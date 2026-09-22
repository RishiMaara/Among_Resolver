"""
Agent 3 (N:M) — joint many-batch subset-sum matching.

Several settlement batches solved SIMULTANEOUSLY against one shared
candidate pool, so a transaction that could plausibly belong to more than
one settlement is assigned by the solver rather than by whichever batch
happens to be processed first. This is the joint sibling of subset_sum.py's
1:N solve, not a replacement for it — see orchestrator.reconcile_many's
docstring for what this reuses from the 1:N path (linkage narrowing per
batch, anchored-refund forcing, ambiguity probing, evidence-based
withholding, confidence gating — all real) and what it does not (the
anchor/strong_link/all_linked tiering and the cross-feed substitutability
guard, both genuine refinements that have not been generalised to a joint
multi-target assignment).

IDENTITY: every set here is keyed by txn_key (linkage.txn_key), never the
bare source_txn_id. N:M is exactly the shape where a bare-id collision is
most likely to bite — the whole point is a pool merging several settlements'
worth of gateway, bank and ERP records at once, and source_txn_id is unique
only within one feed. See subset_sum._solve_cpsat's docstring for the
concrete failure this avoids.
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
    Merge each target's OWN linkage-narrowed candidate pool into one
    (candidate, eligible-targets) structure for the joint solver.

    Every target's pool has already been through build_candidate_links
    independently before this is called — this function narrows nothing
    further, it only merges. A transaction eligible for more than one
    target (a weak cluster signal pointing at two settlements closing in
    the same window, say) appears once in the union, eligible for every
    target whose own narrowing admitted it: that shared claim is exactly
    the case a joint solve exists to resolve. A transaction outside every
    target's narrowed pool never reaches the union at all, same principle
    as the 1:N path's "narrow before the solver runs" — applied per target
    before the merge rather than once for a single batch.

    ANCHORING BINDS A CANDIDATE TO THE TARGET IT NAMES
    ---------------------------------------------------
    `anchor_keys_per_target[t]` is the set of txn_keys that reference
    target t directly. A candidate carrying one is not a shared claim at
    all — the evidence already says whose it is — so its eligibility is
    restricted to the target(s) it names rather than left open to every
    target whose narrowing happened to admit it.

    Without this the joint solve resolved contention on ARITHMETIC ALONE,
    and arithmetic cannot see a reference. Measured on 88 joint batches
    with two settlements each holding an equal-valued leg: 11 auto-cleared
    a set containing the OTHER settlement's anchored leg, at 0.91
    confidence. Swapping two equal amounts leaves both targets satisfied,
    so nothing downstream could detect it — every gate after the solve
    sees a set that sums correctly and contains an anchored member, and
    the anchored member it contains belongs to a different settlement.
    That is a false clear, and it is the failure this engine exists to
    prevent.

    This is narrower than generalising the 1:N substitutability guard,
    which is still not built: it constrains only candidates that carry
    direct reference evidence, and says nothing about twins that are
    merely plausible. A candidate anchored to two targets at once stays
    eligible for both and is left to the solver, which is the genuine
    shared claim the joint path is for.
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

    include: dict[tuple[int, int], object] = {}
    for c in range(n):
        for t in eligible[c]:
            if 0 <= t < num_t:
                include[(c, t)] = model.NewBoolVar(f"include_c{c}_t{t}")

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
            model.AddAtMostOne(vars_for_c)

    for t in range(num_t):
        terms = [
            union[c].amount_cents * include[(c, t)]
            for c in range(n) if t in eligible[c] and (c, t) in include
        ]
        total_t = sum(terms) if terms else 0
        target = target_cents_list[t]
        model.Add(total_t >= target - tolerance_cents)
        model.Add(total_t <= target + tolerance_cents)

    # Forced members — see subset_sum._solve_cpsat's docstring for why this
    # exists: an anchored refund is not the solver's to decline.
    if forced_per_target:
        for t, keys in enumerate(forced_per_target):
            if not keys or t >= num_t:
                continue
            for k in keys:
                c = key_to_idx.get(k)
                if c is not None and (c, t) in include:
                    model.Add(include[(c, t)] == 1)
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
                    c = key_to_idx.get(k)
                    if c is not None:
                        forbidden_pairs.add((c, t))
            # Forbid the exact same joint assignment: at least one (c, t)
            # pair must flip relative to it, over every pair the model
            # actually has a variable for.
            diff_terms = [
                var.Not() if (c, t) in forbidden_pairs else var
                for (c, t), var in include.items()
            ]
            if diff_terms:
                model.AddBoolOr(diff_terms)

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
    Bounded PER-TARGET ambiguity check for the joint solve. Returns one
    bool per target: True if that target's own matched set varied across
    any alternate FULL joint assignment found within the probe budget,
    False if it stayed identical every time (or no alternate was found).

    Per target, not one flag for the whole group: forbidding the baseline
    only guarantees the GLOBAL assignment differs somewhere, not that every
    target is contested. A settlement whose matched set never moved across
    every alternate CP-SAT found within budget is real evidence that ITS
    assignment is stable, even while a different target in the same solve
    keeps reshuffling — punishing it for a neighbour's ambiguity would
    withhold matches this engine can actually stand behind.

    Same caveat as subset_sum._probe_for_alternate_subset: bounded, not
    exhaustive. "No alternate found in probe_limit tries" is evidence of
    stability, never a uniqueness proof — exact subset-sum counting is
    #P-complete regardless of solver choice.
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
