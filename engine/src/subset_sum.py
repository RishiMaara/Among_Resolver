"""
Exact subset-sum matching with CP-SAT (OR-tools). Deterministic, no model.

Given a gross target and candidates, find the subset that sums to it within
a small tolerance. Naive DP is pseudo-polynomial in the paise value (108 s
for 62 candidates at Rs 60,000); CP-SAT's branch-and-bound is not tied to
magnitude (0.1 s there; 2.2 s for 412 of 10,000). Pre-filter to the
settlement window anyway.
"""

from __future__ import annotations
import os
from dataclasses import dataclass, field

from schema import NormalizedTxn, MatchResult, MatchMethod
from linkage import txn_key, canonical_key, _contains_identifier, MIN_CANONICAL_ANCHOR_LEN


def _default_workers() -> int:
    """
    Default CP-SAT parallelism: all logical CPUs, capped at 8. Override with
    SubsetSumConfig.num_search_workers.
    """
    return min(os.cpu_count() or 1, 8)


@dataclass
class SubsetSumConfig:
    tolerance_cents: int = 5        # rounding slack, e.g. +/-5 cents
    max_candidates: int = 20000     # CP-SAT guardrail; beyond this, shrink the
                                     # window or pre-bucket by amount range first
    solver_time_limit_s: float = 5.0
    ambiguity_probe_limit: int = 3  # extra CP-SAT solves to check for alternates
    probe_time_limit_s: float = 8.0  # shorter budget for ambiguity probe solves
                                      # vs primary solve — keeps demo time predictable
    num_search_workers: int = field(default_factory=_default_workers)
    """CP-SAT parallel search workers.

    Defaults to min(os.cpu_count(), 8) so the solver uses all available
    cores on small machines without over-subscribing large ones. Set
    explicitly in resource-constrained environments (containers, CI).
    """


def filter_candidates_by_settlement_window(
    candidates: list[NormalizedTxn],
    settled_at_utc,
    window_days: int = 5,
) -> list[NormalizedTxn]:
    """
    Pre-filter the candidate pool to a realistic settlement window before
    solving. Smaller search space = faster solve and fewer coincidental
    ambiguous matches — searching all history instead of the plausible
    T+2/T+3 window is what lets unrelated transactions coincidentally
    sum to the same target.
    """
    from datetime import timedelta

    window_start = settled_at_utc - timedelta(days=window_days)
    return [
        t for t in candidates
        if window_start <= t.timestamp_utc <= settled_at_utc
    ]


def _solve_cpsat(
    candidates: list[NormalizedTxn],
    target_cents: int,
    tolerance_cents: int,
    time_limit_s: float,
    forbidden_solutions: list[set[str]] | None = None,
    num_search_workers: int = 1,
    forced_ids: set[str] | None = None,
    status_out: list | None = None,
) -> tuple[list[NormalizedTxn], int] | None:
    """
    One CP-SAT solve for a subset within tolerance of the target.
    `forbidden_solutions` asks for a set different from each given one;
    `forced_ids` must be included. Both are txn_key sets: ids repeat across
    feeds, and a bare id once forced an unrelated record into the set.
    `status_out` receives the solver status when given.
    """
    from ortools.sat.python import cp_model

    if not candidates:
        return None

    model = cp_model.CpModel()
    n = len(candidates)
    include = [model.new_bool_var(f"include_{i}") for i in range(n)]
    keys = [txn_key(c) for c in candidates]

    total = sum(candidates[i].amount_cents * include[i] for i in range(n))
    model.add(total >= target_cents - tolerance_cents)
    model.add(total <= target_cents + tolerance_cents)

    # Forced members are not the solver's to choose.
    #
    # A refund anchored to this settlement is not an optional candidate — if
    # it happened and it belongs here, it IS in the set. Leaving it optional
    # is what made refunds break otherwise-perfect reconciliations: negatives
    # create combinatorial slack, so 17 sales alone, 18 sales minus one
    # refund, and 20 sales minus three refunds all hit the same total, and
    # the batch was correctly-but-uselessly reported as ambiguous.
    #
    # Pinning them collapses that slack and leaves the solver choosing only
    # among the payments where a choice actually exists.
    if forced_ids:
        for i in range(n):
            if keys[i] in forced_ids:
                model.add(include[i] == 1)

    if forbidden_solutions:
        for forbidden_keys in forbidden_solutions:
            # forbid the exact same set: at least one variable must flip
            # relative to this forbidden solution (in or out)
            diff_terms = []
            for i in range(n):
                was_included = keys[i] in forbidden_keys
                if was_included:
                    diff_terms.append(include[i].Not())
                else:
                    diff_terms.append(include[i])
            model.add_bool_or(diff_terms)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = num_search_workers
    status = solver.Solve(model)
    # INFEASIBLE proves no such subset exists; UNKNOWN only says the time ran
    # out first. Callers that read "none" as "none exists" need to tell them
    # apart, so the status is handed back when asked for.
    if status_out is not None:
        status_out.append(solver.StatusName(status))

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None

    matched = [candidates[i] for i in range(n) if solver.Value(include[i])]
    achieved_sum = sum(t.amount_cents for t in matched)
    return matched, achieved_sum


def find_exact_subset(
    candidates: list[NormalizedTxn],
    target_cents: int,
    tolerance_cents: int = 5,
    time_limit_s: float = 15.0,
    num_search_workers: int = 1,
    forced_ids: set[str] | None = None,
) -> tuple[list[NormalizedTxn], int] | None:
    """Public entry point — CP-SAT solve, no forbidden solutions."""
    return _solve_cpsat(
        candidates, target_cents, tolerance_cents, time_limit_s,
        num_search_workers=num_search_workers,
        forced_ids=forced_ids,
    )


def _probe_for_alternate_subset(
    matched_txns: list[NormalizedTxn],
    candidates: list[NormalizedTxn],
    target_cents: int,
    tolerance_cents: int,
    time_limit_s: float,
    probe_limit: int,
    num_search_workers: int = 1,
    forced_ids: set[str] | None = None,
    outcome: dict | None = None,
) -> str | None:
    """
    Bounded ambiguity check: ask for up to `probe_limit` different subsets.
    Finding one proves ambiguity; finding none within budget is evidence, never
    a uniqueness proof (counting subsets is #P-complete). A probe that ran out
    of time is recorded in `outcome` as timed_out.
    """
    # txn_key, not the bare source_txn_id: see _solve_cpsat's docstring for
    # why a bare id is not a safe identity across a merged multi-feed pool.
    matched_keys = {txn_key(t) for t in matched_txns}
    forbidden = [matched_keys]
    found: list[tuple[set[str], int]] = []
    statuses: list = []

    for _ in range(probe_limit):
        # The probe has to solve under the SAME constraints as the real
        # search. Without the forced members it happily "finds" alternates
        # that simply drop the refunds — sets that are arithmetically valid
        # and factually impossible — and reports ambiguity that does not
        # exist.
        alt = _solve_cpsat(
            candidates, target_cents, tolerance_cents, time_limit_s,
            forbidden_solutions=forbidden,
            num_search_workers=num_search_workers,
            forced_ids=forced_ids,
            status_out=statuses,
        )
        if alt is None:
            # Out of time is not the same as out of alternates: say which.
            if statuses and statuses[-1] == "UNKNOWN" and outcome is not None:
                outcome["timed_out"] = True
            break  # no further alternates found within budget
        alt_txns, alt_sum = alt
        alt_keys = {txn_key(t) for t in alt_txns}
        found.append((alt_keys, alt_sum))
        forbidden.append(alt_keys)

    if not found:
        return None

    _, first_sum = found[0]
    return (
        f"found {len(found)} of up to {probe_limit} probed alternate "
        f"subset(s), each also summing to within tolerance of "
        f"{target_cents} cents (first alternate: {len(found[0][0])} "
        f"transactions, {first_sum} cents)"
    )



def _anchored_negatives(batch_id: str, candidates: list[NormalizedTxn]) -> set[str]:
    """
    Refunds this settlement cannot decline: a negative amount carrying THIS
    settlement's reference was netted off this payout, so any set without it is
    wrong. Forcing them removes the slack refunds create (17 sales, 18 minus one
    refund, 20 minus three all net the same). Only anchored refunds are forced.
    Returns txn_keys.
    """
    if not batch_id:
        return set()
    # Same canonical form and whole-identifier rule as linkage: plain substring
    # containment forced SETTLE-10's refunds into SETTLE-1, and a false FORCED
    # member can tie out to zero and clear.
    anchor = canonical_key(batch_id)
    if len(anchor) < MIN_CANONICAL_ANCHOR_LEN:
        return set()
    forced = set()
    for t in candidates:
        if t.amount_cents >= 0:
            continue
        ref = canonical_key(t.ref_id_canonical or "")
        if ref and _contains_identifier(ref, anchor):
            forced.add(txn_key(t))
    return forced


# What the arithmetic is worth when the check for a second subset ran out of
# time before it could prove there is none. 1.0 means "the probe found no
# alternate"; a probe that never finished has not shown that, and reporting
# 1.0 for it claimed a uniqueness nobody established (FAILURE_LOG 39). Set
# above the auto-clear gate on purpose: evidence that names the settlement
# can still carry a clear, the report just stops claiming the arithmetic
# alone settled it.
UNPROVEN_UNIQUE_CONFIDENCE = 0.90
UNPROVEN_UNIQUE_NOTE = (
    " Uniqueness not established: the check for a second subset ran out of "
    "time before it could prove none exists, so the arithmetic is reported "
    f"at {UNPROVEN_UNIQUE_CONFIDENCE:.2f} rather than 1.0."
)


def match_batch(
    batch_id: str,
    candidates: list[NormalizedTxn],
    target_cents: int,
    config: SubsetSumConfig | None = None,
) -> MatchResult:
    """Top-level entry point for Agent 3. Always returns a MatchResult —
    cleared=False if no exact subset was found, so the caller routes to
    Agent 4 (fuzzy) or Agent 5 (exceptions) next."""
    config = config or SubsetSumConfig()

    forced_ids = _anchored_negatives(batch_id, candidates)

    result = find_exact_subset(
        candidates, target_cents, config.tolerance_cents,
        config.solver_time_limit_s, config.num_search_workers,
        forced_ids=forced_ids,
    )

    if result is None:
        from fuzzy_match import approximate_subset_sum_greedy
        approx_txns, approx_sum = approximate_subset_sum_greedy(candidates, target_cents)
        
        if approx_txns:
            return MatchResult(
                batch_id=batch_id,
                matched_txn_ids=[t.source_txn_id for t in approx_txns],
                matched_keys=[txn_key(t) for t in approx_txns],
                method=MatchMethod.FUZZY_SEMANTIC,
                confidence=0.4,
                matched_sum_cents=approx_sum,
                target_cents=target_cents,
                cleared=False,  # ALWAYS False for approximation
                reasoning=f"CP-SAT failed/timed out. Fallback greedy approximation found {len(approx_txns)} txns summing to {approx_sum}c (target {target_cents}c). Requires review.",
            )
            
        return MatchResult(
            batch_id=batch_id,
            matched_txn_ids=[],
            method=MatchMethod.EXACT_SUBSET_SUM,
            confidence=0.0,
            matched_sum_cents=0,
            target_cents=target_cents,
            cleared=False,
            reasoning="No exact subset found within tolerance. Routing to fuzzy pass.",
        )

    matched_txns, achieved_sum = result
    diff = abs(achieved_sum - target_cents)

    ambiguous = False
    withheld_reason: str | None = None
    ambiguity_note = ""
    probe: dict = {}
    alt_description = _probe_for_alternate_subset(
        matched_txns, candidates, target_cents, config.tolerance_cents,
        config.probe_time_limit_s, config.ambiguity_probe_limit,
        num_search_workers=config.num_search_workers,
        forced_ids=forced_ids,
        outcome=probe,
    )
    if alt_description is not None:
        ambiguous = True
        withheld_reason = "alternate_subset"
        ambiguity_note = (
            f" WARNING (bounded probe, not exhaustive): {alt_description} — "
            f"this match is not uniquely determined by arithmetic alone within "
            f"the probed alternatives. Narrow the candidate window or resolve "
            f"via ref_id/memo evidence before auto-clearing."
        )

    # 0.36 for an ambiguous set: that band was re-measured twice and came back
    # lower each time (0.65 was right 54.5%, then 0.54 right 36.4%), so it sits
    # at the observed rate. It is below the gate either way. 1.0 means no
    # alternative was found; linkage confidence is applied on top downstream.
    confidence = 0.36 if ambiguous else 1.0
    if not ambiguous and probe.get("timed_out"):
        confidence = UNPROVEN_UNIQUE_CONFIDENCE
        ambiguity_note = UNPROVEN_UNIQUE_NOTE

    return MatchResult(
        batch_id=batch_id,
        matched_txn_ids=[t.source_txn_id for t in matched_txns],
        matched_keys=[txn_key(t) for t in matched_txns],
        method=MatchMethod.EXACT_SUBSET_SUM,
        confidence=confidence,
        matched_sum_cents=achieved_sum,
        target_cents=target_cents,
        cleared=not ambiguous,   # ambiguous matches do NOT auto-clear
        ambiguous=ambiguous,
        withheld_reason=withheld_reason,
        reasoning=(
            f"Exact subset-sum match (CP-SAT): {len(matched_txns)} transactions "
            f"sum to {achieved_sum} cents (target {target_cents} cents, diff {diff} cents)."
            f"{ambiguity_note}"
        ),
    )
