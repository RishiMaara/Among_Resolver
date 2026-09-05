"""
Agent 3 — Subset-Sum Matching Engine.

The core differentiator. Given a settlement batch's gross target amount
and a pool of candidate transactions, find the exact subset that sums
to the target (within a small tolerance band for rounding).

This is deterministic. No LLM. No probabilistic guessing. Every match
this engine produces must be exactly reproducible and auditable.

ALGORITHM — CP-SAT (OR-tools), not naive DP:

  Naive DP subset-sum is O(n * target_value). That looks fine on paper
  but is a trap at real settlement scale: a Rs 60,000 batch has a target
  of 6,300,000+ cents, so even 62 candidates means ~390M operations —
  measured at 108 SECONDS in this project's first pass. That's not a
  tuning problem, it's the wrong algorithm: naive DP is pseudo-polynomial
  in the rupee amount, and cents-level precision at real amounts makes
  that polynomial term huge regardless of candidate count.

  CP-SAT is a constraint solver with branch-and-bound + pruning, not
  brute enumeration — same worst-case complexity class, but its
  practical performance is unrelated to target magnitude. Measured on
  this project: 0.1s for a 62-candidate / Rs 60,000 batch, 2.2s for a
  10,000-candidate / 412-transaction batch (the brief's own "412 out of
  10,000" scenario). That's the difference between a real-time demo and
  a coffee-break wait.

  Candidate pool should still be pre-filtered to a realistic settlement
  window before this runs (filter_candidates_by_settlement_window below)
  — smaller search space is always better, CP-SAT's speed isn't a reason
  to skip that discipline.
"""

from __future__ import annotations
import os
from dataclasses import dataclass, field

from schema import NormalizedTxn, MatchResult, MatchMethod


def _default_workers() -> int:
    """
    Default CP-SAT parallelism: all logical CPUs, capped at 8.

    The cap prevents over-subscription on large machines where handing
    the full core count to OR-tools would starve the rest of the application
    (uvicorn, Redis, other workers). On a 2-vCPU container os.cpu_count()
    returns 2; on a 32-core server it returns 8; on a laptop it returns
    whatever the machine has. All three are better than hardcoding 4 and
    hoping for the best.

    Override via SubsetSumConfig.num_search_workers when running benchmarks
    or in environments with strict resource quotas.
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
) -> tuple[list[NormalizedTxn], int] | None:
    """
    Core CP-SAT solve: find a subset of `candidates` summing to
    target_cents within tolerance_cents. `forbidden_solutions` lets the
    ambiguity check ask "find a DIFFERENT valid subset than these" by
    adding a constraint that at least one included/excluded flag must
    differ from each forbidden solution.
    """
    from ortools.sat.python import cp_model

    if not candidates:
        return None

    model = cp_model.CpModel()
    n = len(candidates)
    include = [model.NewBoolVar(f"include_{i}") for i in range(n)]

    total = sum(candidates[i].amount_cents * include[i] for i in range(n))
    model.Add(total >= target_cents - tolerance_cents)
    model.Add(total <= target_cents + tolerance_cents)

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
            if candidates[i].source_txn_id in forced_ids:
                model.Add(include[i] == 1)

    if forbidden_solutions:
        for forbidden_ids in forbidden_solutions:
            # forbid the exact same set: at least one variable must flip
            # relative to this forbidden solution (in or out)
            diff_terms = []
            for i in range(n):
                txn_id = candidates[i].source_txn_id
                was_included = txn_id in forbidden_ids
                if was_included:
                    diff_terms.append(include[i].Not())
                else:
                    diff_terms.append(include[i])
            model.AddBoolOr(diff_terms)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = num_search_workers
    status = solver.Solve(model)

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
) -> str | None:
    """
    Bounded ambiguity check: ask CP-SAT to find a DIFFERENT valid subset
    than the one already found, using a forbidding constraint. Cheap now
    that solves are ~0.1-2s instead of 100+s, but still bounded
    (probe_limit) and still NOT an exhaustive uniqueness proof — exact
    subset-sum counting is #P-complete and intractable at real scale
    regardless of solver choice. This proves "at least one alternate
    exists" or "none found within the probe budget," never "provably
    unique."
    """
    matched_ids = {t.source_txn_id for t in matched_txns}
    forbidden = [matched_ids]
    _num_search_workers = num_search_workers

    for _ in range(probe_limit):
        # The probe has to solve under the SAME constraints as the real
        # search. Without the forced members it happily "finds" alternates
        # that simply drop the refunds — sets that are arithmetically valid
        # and factually impossible — and reports ambiguity that does not
        # exist.
        alt = _solve_cpsat(
            candidates, target_cents, tolerance_cents, time_limit_s,
            forbidden_solutions=forbidden,
            num_search_workers=_num_search_workers,
            forced_ids=forced_ids,
        )
        if alt is None:
            return None  # no further alternates found within budget
        alt_txns, alt_sum = alt
        alt_ids = {t.source_txn_id for t in alt_txns}
        if alt_ids != matched_ids:
            return (
                f"found an alternate {len(alt_txns)}-transaction subset "
                f"also summing to {alt_sum} cents (target {target_cents})"
            )
        forbidden.append(alt_ids)

    return None



def _anchored_negatives(batch_id: str, candidates: list[NormalizedTxn]) -> set[str]:
    """
    Refunds this settlement cannot decline to include.

    A negative amount carrying THIS settlement's reference is a refund that
    was netted off this payout. It is not a candidate the solver may take or
    leave — it happened, it belongs here, and any set excluding it is wrong
    however well it adds up.

    Treating them as optional was quietly costing real reconciliations. Twenty
    sales of Rs 1,000 with three refunds of Rs 1,000 nets to Rs 17,000, and so
    does seventeen sales alone, and so does eighteen sales minus one refund.
    All are arithmetically valid, so the batch was reported ambiguous and
    withheld — correct, and useless, on a shape that occurs in almost every
    real settlement.

    ONLY anchored negatives are forced. A refund with no reference, or one
    referencing a different settlement, stays optional: forcing it would be
    asserting membership the evidence does not support, which is the exact
    error this engine exists to avoid. The anchor has to be there.
    """
    if not batch_id:
        return set()
    # Compare in the SAME canonical form ingestion stored. ref_id_canonical
    # has punctuation stripped, so a raw "REFUND-DAY" never matches the
    # "REFUNDDAY" on the row and every refund silently stayed optional.
    from linkage import canonical_key
    anchor = canonical_key(batch_id)
    if not anchor:
        return set()
    forced = set()
    for t in candidates:
        if t.amount_cents >= 0:
            continue
        ref = canonical_key(t.ref_id_canonical or "")
        if ref and (ref == anchor or anchor in ref or ref in anchor):
            forced.add(t.source_txn_id)
    return forced


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
    alt_description = _probe_for_alternate_subset(
        matched_txns, candidates, target_cents, config.tolerance_cents,
        config.probe_time_limit_s, config.ambiguity_probe_limit,
        num_search_workers=config.num_search_workers,
        forced_ids=forced_ids,
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

    # 0.54, not 0.65. Calibration bucketed every prediction the engine made
    # against whether the set was actually correct: the 0.65 band was right
    # 54.5% of the time (n=11). Small sample, but the error is in the
    # dangerous direction — an overstated confidence tells a reviewer to skip
    # a batch that was wrong — so it is set at the observed rate rather than
    # left flattering. See scripts/calibration.py.
    #
    # 1.0 for the unambiguous case is not a guess: the probe found no
    # alternative subset within tolerance, so the arithmetic really is
    # determined. What that does NOT establish is that the arithmetic
    # identifies the right transactions, which is why linkage confidence is
    # applied on top of this downstream rather than instead of it.
    confidence = 0.54 if ambiguous else 1.0

    return MatchResult(
        batch_id=batch_id,
        matched_txn_ids=[t.source_txn_id for t in matched_txns],
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
