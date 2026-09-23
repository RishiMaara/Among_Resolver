"""
Joint N:M subset-sum: several settlements solved at once against one pool,
so a payment two settlements could claim is assigned by the solver. What
carries over from the 1:N path is listed in orchestrator.reconcile_many.
Every set is keyed by txn_key; bare ids repeat across feeds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

from schema import NormalizedTxn
from linkage import txn_key

logger = logging.getLogger(__name__)


@dataclass
class NMSolveResult:
    """One joint solve's outcome. `matched`/`achieved_sums` are indexed by
    target, in the same order target_cents_list was given."""
    matched: List[List[NormalizedTxn]]
    achieved_sums: List[int]


def build_union_pool(
    candidates_per_target: List[List[NormalizedTxn]],
    anchor_keys_per_target: List[set[str]] | None = None,
) -> tuple[List[NormalizedTxn], List[set[int]]]:
    """
    Merge each target's own linkage-narrowed pool into one union with, per
    candidate, the targets it is eligible for. Nothing is narrowed further.

    A candidate anchored to a target is eligible only for the target(s) it
    names: resolving contention on arithmetic alone once gave a settlement the
    other's equal-valued anchored leg (11 of 88 batches, at 0.91). A candidate
    anchored to several targets stays eligible for each.
    """
    by_key: dict[str, NormalizedTxn] = {}
    order: List[str] = []
    target_sets: dict[str, set[int]] = {}

    for t_idx, pool in enumerate(candidates_per_target):
        for txn in pool:
            k = txn_key(txn)
            if k not in by_key:
                by_key[k] = txn
                order.append(k)
                target_sets[k] = set()
            target_sets[k].add(t_idx)

    if anchor_keys_per_target:
        for k in order:
            anchored_to = {
                t_idx for t_idx, keys in enumerate(anchor_keys_per_target)
                if k in keys
            }
            # Intersect rather than replace: an anchor for a target whose
            # own narrowing dropped the record (out of window, wrong
            # currency) must not smuggle it back into that target's pool.
            bound = anchored_to & target_sets[k]
            if bound:
                target_sets[k] = bound

    union = [by_key[k] for k in order]
    eligible = [target_sets[k] for k in order]
    return union, eligible


def _solve(
    union: List[NormalizedTxn],
    eligible: List[set[int]],
    target_cents_list: List[int],
    tolerance_cents: int,
    time_limit_s: float,
    num_search_workers: int = 1,
    forced_per_target: List[set[str]] | None = None,
    forbidden_solutions: List[List[set[str]]] | None = None,
    status_out: list | None = None,
) -> NMSolveResult | None:
    """
    Core joint CP-SAT solve. include[(c, t)] exists ONLY when candidate c is
    in eligible[c] for target t — a candidate structurally cannot be
    assigned to a target its own linkage narrowing never admitted it to, so
    there is no variable for the solver to consider in the first place.

    `forced_per_target[t]` and each entry of `forbidden_solutions` are sets
    of txn_key values, one set per target, in target order — see the module
    docstring for why txn_key rather than the bare id.
    """
    from ortools.sat.python import cp_model

    if not union or not target_cents_list:
        return None

    model = cp_model.CpModel()
    n = len(union)
    num_t = len(target_cents_list)
    key_to_idx = {txn_key(u): i for i, u in enumerate(union)}

    include: dict[tuple[int, int], cp_model.IntVar] = {}
    for c in range(n):
        for t in eligible[c]:
            if 0 <= t < num_t:
                include[(c, t)] = model.new_bool_var(f"include_c{c}_t{t}")

    if not include:
        return None

    # A candidate may be claimed by at most one of the targets it is
    # eligible for. This is the constraint that makes the solve JOINT
    # rather than num_t independent solves sharing a pool by accident: two
    # targets contesting the same transaction must settle it against each
    # other, not both silently claim it.
    by_candidate: dict[int, list] = {}
    for (c, t), var in include.items():
        by_candidate.setdefault(c, []).append(var)
    for c, vars_for_c in by_candidate.items():
        if len(vars_for_c) > 1:
            model.add_at_most_one(vars_for_c)

    for t in range(num_t):
        terms = [
            union[c].amount_cents * include[(c, t)]
            for c in range(n) if t in eligible[c] and (c, t) in include
        ]
        total_t = sum(terms) if terms else 0
        target = target_cents_list[t]
        model.add(total_t >= target - tolerance_cents)
        model.add(total_t <= target + tolerance_cents)

    # Forced members — see subset_sum._solve_cpsat's docstring for why this
    # exists: an anchored refund is not the solver's to decline.
    if forced_per_target:
        for t, keys in enumerate(forced_per_target):
            if not keys or t >= num_t:
                continue
            for k in keys:
                idx = key_to_idx.get(k)
                if idx is not None and (idx, t) in include:
                    model.add(include[(idx, t)] == 1)
                # A forced key that resolves to no (c, t) variable at all
                # means it was anchored to a target whose own narrowed pool
                # never contained it — cannot happen when forced_per_target
                # is built from the SAME narrowed pools that fed
                # candidates_per_target (which is how orchestrator.py builds
                # it), so this is a defensive no-op, not a silent guess.

    if forbidden_solutions:
        for forbidden in forbidden_solutions:
            forbidden_pairs: set[tuple[int, int]] = set()
            for t, keys in enumerate(forbidden):
                if t >= num_t:
                    continue
                for k in keys:
                    idx = key_to_idx.get(k)
                    if idx is not None:
                        forbidden_pairs.add((idx, t))
            # Forbid the exact same joint assignment: at least one (c, t)
            # pair must flip relative to it, over every pair the model
            # actually has a variable for.
            diff_terms = [
                var.Not() if (c, t) in forbidden_pairs else var
                for (c, t), var in include.items()
            ]
            if diff_terms:
                model.add_bool_or(diff_terms)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = num_search_workers
    status = solver.Solve(model)
    # See subset_sum._solve_cpsat: UNKNOWN is "out of time", not "none".
    if status_out is not None:
        status_out.append(solver.StatusName(status))

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None

    matched: List[List[NormalizedTxn]] = [[] for _ in range(num_t)]
    for (c, t), var in include.items():
        if solver.Value(var):
            matched[t].append(union[c])

    sums = [sum(txn.amount_cents for txn in group) for group in matched]
    return NMSolveResult(matched=matched, achieved_sums=sums)


def exact_subset_sum_nm(
    union: List[NormalizedTxn],
    eligible: List[set[int]],
    target_cents_list: List[int],
    tolerance_cents: int = 5,
    time_limit_s: float = 10.0,
    num_search_workers: int = 1,
    forced_per_target: List[set[str]] | None = None,
) -> NMSolveResult | None:
    """Public entry point — one joint CP-SAT solve, no forbidding constraint.
    Build `union`/`eligible` with build_union_pool first."""
    return _solve(
        union, eligible, target_cents_list, tolerance_cents, time_limit_s,
        num_search_workers=num_search_workers,
        forced_per_target=forced_per_target,
    )


def probe_for_alternate_nm_assignment(
    union: List[NormalizedTxn],
    eligible: List[set[int]],
    baseline: NMSolveResult,
    target_cents_list: List[int],
    tolerance_cents: int,
    time_limit_s: float,
    probe_limit: int,
    num_search_workers: int = 1,
    forced_per_target: List[set[str]] | None = None,
    outcome: dict | None = None,
) -> List[bool]:
    """
    Per-target ambiguity for the joint solve: True where that target's own set
    varied across the alternate joint assignments found within budget. A
    target whose set never moved is not punished for a neighbour's ambiguity.
    Bounded, not exhaustive; a timeout is recorded in `outcome`.
    """
    baseline_keys = [{txn_key(t) for t in group} for group in baseline.matched]
    forbidden = [baseline_keys]
    varied = [False] * len(target_cents_list)
    statuses: list = []

    for _ in range(probe_limit):
        alt = _solve(
            union, eligible, target_cents_list, tolerance_cents, time_limit_s,
            num_search_workers=num_search_workers,
            forced_per_target=forced_per_target,
            forbidden_solutions=forbidden,
            status_out=statuses,
        )
        if alt is None:
            if statuses and statuses[-1] == "UNKNOWN" and outcome is not None:
                outcome["timed_out"] = True
            break  # no further alternates found within budget
        alt_keys = [{txn_key(t) for t in group} for group in alt.matched]
        for i, (b, a) in enumerate(zip(baseline_keys, alt_keys)):
            if a != b:
                varied[i] = True
        forbidden.append(alt_keys)

    return varied
