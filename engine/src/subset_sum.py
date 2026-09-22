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
from linkage import txn_key, canonical_key, _contains_identifier, MIN_CANONICAL_ANCHOR_LEN


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
    status_out: list | None = None,
) -> tuple[list[NormalizedTxn], int] | None:
    """
    Core CP-SAT solve: find a subset of `candidates` summing to
    target_cents within tolerance_cents. `forbidden_solutions` lets the
    ambiguity check ask "find a DIFFERENT valid subset than these" by
    adding a constraint that at least one included/excluded flag must
    differ from each forbidden solution.

    `forced_ids` and `forbidden_solutions` are both sets of txn_key values
    (linkage.txn_key: "{source}:{source_txn_id}"), NOT bare source_txn_id.
    `source_txn_id` is unique per FEED, not globally — a settlement's
    candidate pool routinely merges gateway, bank and ERP records, and their
    id sequences overlap. Matching by bare id here used to mean a forced
    refund's id could ALSO match an unrelated record from a different feed
    that happened to reuse it, silently forcing that unrelated record into
    the matched set alongside the real one (or worse, in a pool that never
    contains the real anchored refund by coincidence of collision, forcing
    the wrong one in its place). A false forced-negative is a confident
    wrong answer on someone else's money, same as a false anchor — see
    _anchored_negatives.
    """
    from ortools.sat.python import cp_model

    if not candidates:
        return None

    model = cp_model.CpModel()
    n = len(candidates)
    include = [model.NewBoolVar(f"include_{i}") for i in range(n)]
    keys = [txn_key(c) for c in candidates]

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
            if keys[i] in forced_ids:
                model.Add(include[i] == 1)

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
            model.AddBoolOr(diff_terms)

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
    Bounded ambiguity check: ask CP-SAT for up to `probe_limit` DIFFERENT
    valid subsets than the one already found, using a forbidding constraint
    that grows with each one found. Cheap now that solves are ~0.1-2s
    instead of 100+s, but still bounded and still NOT an exhaustive
    uniqueness proof — exact subset-sum counting is #P-complete and
    intractable at real scale regardless of solver choice. This proves "N
    alternates exist within the probe budget" or "none found," never
    "provably unique."

    Runs the full probe_limit rather than stopping at the first alternate.
    An earlier version returned as soon as `alt_ids != matched_ids` — but
    `forbidden` already contains `matched_ids` and the solver is constrained
    to differ from every entry in it, so any solution found is ALREADY
    guaranteed to differ from `matched_ids`; that comparison was always
    true and the function always returned on iteration one. `probe_limit`
    and the `forbidden.append(...)` below it were dead code, while the docs
    (README, TEST_REPORT) kept claiming "3 extra solves" — a documentation
    accuracy bug, not a correctness one, but the wrong kind to have in the
    one place the project is being humble about its own thoroughness.
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

    Returns txn_key values (linkage.txn_key), not bare source_txn_id. A bare
    id is not unique across a merged multi-feed pool — the candidates this
    is called with routinely mix gateway, bank and ERP records, and their id
    sequences overlap. A caller that matched candidates back to this set by
    bare id would force-include EVERY candidate sharing that id, anchored or
    not: force the gateway refund that names this settlement, and an
    unrelated ERP line that happens to reuse the same id number comes along
    with it, "included" by nothing but a coincidence in two feeds' counters.
    """
    if not batch_id:
        return set()
    # Compare in the SAME canonical form ingestion stored, and with the SAME
    # false-anchor guards linkage.py uses for the identical containment
    # problem — see linkage._contains_identifier for the full reasoning.
    #
    # This used to be plain substring containment (`anchor in ref or ref in
    # anchor`), which is wrong for unpadded sequential settlement ids:
    # "SETTLE1" is a substring of both "SETTLE10" and "SETTLE100", so batch
    # SETTLE-1 was force-including refunds that belong to SETTLE-10 and
    # SETTLE-100. That is worse than a false anchor: a false anchor only adds
    # a candidate the solver may reject, but a false FORCED member is
    # `model.Add(include[i] == 1)` — the solver must include a refund
    # belonging to a different settlement, then finds other payments to
    # cover the difference, and the set can tie out to zero and clear. Both
    # reference corpora hide this because they use fixed-width zero-padded
    # ids where no id is a prefix of another; unpadded sequential ids are
    # normal in production.
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

    # 0.36, not 0.54, and 0.54 not 0.65 before it. Calibration buckets every
    # prediction against whether the set was actually correct, and this band
    # has now been re-measured twice and come back lower both times: the 0.65
    # band was right 54.5%, and the 0.54 band that replaced it is right 36.4%
    # (n=11 each). Small sample, and the same small sample — but the error is
    # in the dangerous direction every time, and an overstated confidence
    # tells a reviewer to skip a batch that was wrong. Set at the observed
    # rate rather than left flattering. See scripts/calibration.py.
    #
    # This is below the auto-clear gate either way, so the change costs no
    # coverage: it only stops the number lying to whoever reads it.
    #
    # 1.0 for the unambiguous case is not a guess: the probe found no
    # alternative subset within tolerance, so the arithmetic really is
    # determined. What that does NOT establish is that the arithmetic
    # identifies the right transactions, which is why linkage confidence is
    # applied on top of this downstream rather than instead of it.
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
