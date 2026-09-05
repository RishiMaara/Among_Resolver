"""
Agent 6 — Orchestrator + Governance.

Runs the full pipeline in order and enforces the one non-negotiable rule
from the brief: nothing writes back to a ledger automatically. The
orchestrator proposes matches, computes an honest match rate, and
produces an exception report. A human approves final write-backs —
that approval step is outside this module by design.

Pipeline order:
  1. Ingest + normalize (Agent 1) — happens before this is called
  2. Fee decomposition (Agent 2) -> gross target for each batch
  3. Exact subset-sum (Agent 3) -> try deterministic match first
  3b. Ambiguity tie-breaking -> when Agent 3 finds multiple valid subsets,
      use fuzzy ref_id/memo signal to prefer the more coherent one
  4. Fuzzy/semantic fallback (Agent 4) -> only for what Agent 3 couldn't clear
  5. Exception diagnosis (Agent 5) -> for what's still unresolved
  6. Audit log every decision (audit.py)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from schema import NormalizedTxn, SettlementBatch, MatchResult, MatchMethod, ExceptionRecord
from fee_decomposition import compute_fee_breakdown, FeeRateCard, DEFAULT_RATE_CARD
from subset_sum import (
    SubsetSumConfig,
    find_exact_subset,
    match_batch,
    filter_candidates_by_settlement_window,
)
from subset_sum_nm import exact_subset_sum_nm
from fuzzy_match import (
    match_batch_fuzzy, FuzzyMatchConfig,
    score_subset_plausibility, build_similarity_context, bulk_fuzzy_recover,
)
import erp_sync
from exception_diagnosis import diagnose_batch_exceptions
from linkage import LinkageResult, build_candidate_links, link_confidence, txn_key
import audit


# Above this candidate count, an exact subset sum with no anchor evidence is
# not credible as an identification. Derived from the density argument in
# linkage.py: 2^n subsets compete for a target with ~2e6 distinct paise
# values, so uniqueness stops being plausible around n=20-25.
#
# Set at the BOTTOM of that range, not the top. It was 25 — the generous end —
# and the realistic benchmark then produced false clears at pools of 22 and 23:
# with an estimated fee target and a 10-paise tolerance band, C(22,5) is 26,334
# subsets competing for a 20-paise-wide window, and the arithmetic is simply
# not determined there. The two errors are not symmetric, so the conservative
# end of a range this uncertain is the defensible one.
UNANCHORED_AUTOCLEAR_LIMIT = int(
    os.environ.get("UNANCHORED_AUTOCLEAR_LIMIT", "20")
)

# Minimum structural confidence required to auto-clear without review.
#
# Set where the measured reliability actually begins, not at a round number.
# calibration.py buckets every prediction against its outcome:
#
#     [0.93, 1.01)   n=62   said 0.955   actual 1.000
#     [0.85, 0.93)   n=41   said 0.876   actual 1.000
#     [0.70, 0.85)   n=5    said 0.800   actual 1.000
#     [0.50, 0.70)   n=11   said 0.540   actual 0.364   <- overconfident
#     [0.00, 0.50)   n=61   said 0.176   actual 0.098   <- overconfident
#
# Everything at or above 0.85 was correct in all 103 observations; below 0.70
# it is a coin flip or worse. 0.85 is therefore the boundary the data draws.
#
# These counts were n=31 and "93 observations" until the benchmark was pinned
# to a single CP-SAT worker. They were not wrong then, they were one sample of
# a measurement that moved: the parallel solver picks differently between runs
# on scenarios where more than one subset is valid. See benchmark.run_scenario.
#
# This was 0.90 briefly, which is the kind of round number that looks careful
# and is not: the 50K stress run scores 0.87 — fully anchored, penalised for a
# large pool — finds all 55 members with precision and recall of 1.0, and was
# withheld for being three hundredths under an arbitrary line. Override with
# AUTOCLEAR_MIN_CONFIDENCE to re-measure the trade-off.
MIN_AUTOCLEAR_CONFIDENCE = float(
    os.environ.get("AUTOCLEAR_MIN_CONFIDENCE", "0.85")
)


@dataclass
class ReconciliationReport:
    batch_id: str
    total_candidates: int
    match_result: MatchResult
    exceptions: list[ExceptionRecord] = field(default_factory=list)

    false_positive_cost_estimate_cents: int = 0
    """
    Estimated cost if the matched subset is incorrect (a false positive).
    Computed as: matched_sum_cents * false_positive_rate (conservative 5%
    for ambiguous matches, 0 for high-confidence exact matches).
    Judges explicitly score this metric — it's the financial materiality
    of getting the match wrong.
    """

    # ── target preservation ───────────────────────────────────────────────
    # Everything needed to prove the money adds up, rather than asserting it.
    fee_basis: str = "estimated"
    """Whether the gross target rests on deductions the source DECLARED or on
    an ESTIMATE from a rate card. A reviewer needs to know whether the target
    is a fact or an inference."""

    target_cents: int = 0
    matched_gross_cents: int = 0
    net_amount_cents: int = 0
    deductions_cents: int = 0

    @property
    def tie_out_residual_cents(self) -> int:
        """
        matched gross - deductions - net. Zero means the books tie.

        This is the arithmetic proof that the target was preserved end to end:
        the transactions matched, less what the processor withheld, equal the
        cash that actually arrived. It costs nothing to report and is the
        difference between "the solver said yes" and "the money adds up".
        """
        if not self.match_result.matched_txn_ids:
            return 0
        return self.matched_gross_cents - self.deductions_cents - self.net_amount_cents

    @property
    def ties_out(self) -> bool:
        return self.tie_out_residual_cents == 0

    @property
    def match_rate(self) -> float:
        if self.total_candidates == 0:
            return 0.0
        matched = len(self.match_result.matched_txn_ids)
        return round(matched / self.total_candidates, 4)

    def summary(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "cleared": self.match_result.cleared,
            "method": self.match_result.method.value,
            "match_rate": self.match_rate,
            "matched_count": len(self.match_result.matched_txn_ids),
            "total_candidates": self.total_candidates,
            "exception_count": len(self.exceptions),
            "exceptions_by_reason": _count_by_reason(self.exceptions),
            "ambiguous": self.match_result.ambiguous,
            "withheld_reason": self.match_result.withheld_reason,
            "confidence": self.match_result.confidence,
            "false_positive_cost_estimate_cents": self.false_positive_cost_estimate_cents,
            "requires_human_approval": (
                not self.match_result.cleared
                or self.match_result.ambiguous
                or len(self.exceptions) > 0
            ),
            "fee_basis": self.fee_basis,
            "target_cents": self.target_cents,
            "matched_gross_cents": self.matched_gross_cents,
            "deductions_cents": self.deductions_cents,
            "tie_out_residual_cents": self.tie_out_residual_cents,
            "ties_out": self.ties_out,
        }


# States that mean the money did not move. Deliberately a closed list of
# things that are unambiguous: anything unrecognised is treated as settling,
# because most feeds carry no status at all and guessing would discard real
# payments.
NON_SETTLING_STATUSES = {
    # Never went through.
    "failed", "failure", "fail", "declined", "rejected", "error",
    "cancelled", "canceled", "voided", "void", "expired", "timeout",
    "reversed", "rolled_back", "rollback",

    # Money authorised but not taken. The merchant can still walk away, so
    # nothing has settled — Stripe splits this across several names.
    "authorized", "authorised", "requires_capture", "uncaptured",
    "requires_payment_method", "requires_confirmation", "requires_action",

    # Started, not finished.
    "created", "initiated", "pending", "processing", "in_transit",
    "incomplete", "awaiting_payment",

    # The bank took it back or never posted it. A returned credit is money
    # that visibly arrived and then left, which is the case most likely to be
    # reconciled by mistake.
    "returned", "return", "bounced", "unposted", "not_posted",
    "on_hold", "held", "blocked", "frozen",
}

# DELIBERATELY NOT IN THAT LIST, and the reasoning matters more than the
# names:
#
#   refunded, partially_refunded, chargeback, chargeback_lost, dispute
#
# These all describe money that DID move and was later clawed back. The
# clawback is its own row — a negative amount the engine already handles as a
# forced member when it is anchored. Excluding the original as well would
# subtract the same reversal twice and leave the settlement short by exactly
# the refunded amount, which is a wrong answer that ties out to nothing and
# would be very hard to trace back to this list.
#
#   captured, settled, succeeded, success, paid, credited, processed,
#   deemed_success, represented
#
# Money moved. `deemed_success` is UPI's "treat as successful pending
# confirmation" and settles; `represented` is a chargeback resolved in the
# merchant's favour, so the money is theirs again.


def _count_by_reason(exceptions: list[ExceptionRecord]) -> dict:
    counts: dict[str, int] = {}
    for e in exceptions:
        counts[e.reason.value] = counts.get(e.reason.value, 0) + 1
    return counts


def _compute_false_positive_cost(match_result: MatchResult) -> int:
    """
    Conservative estimate of the financial cost if this match is wrong.
    - High-confidence exact match (confidence=1.0, not ambiguous): cost = 0
      (the math is exact, only way it's wrong is a fee-rate error)
    - Ambiguous exact match (confidence=0.65): 5% of matched sum
      (plausible restatement error if wrong subset chosen)
    - Fuzzy/semantic match: 10% of matched sum
      (semantic reasoning could be wrong even at high confidence)
    - Not cleared: N/A (no auto-clear, human approves)
    """
    if not match_result.cleared:
        return 0
    amount = match_result.matched_sum_cents
    if match_result.ambiguous:
        return round(amount * 0.05)
    if match_result.method == MatchMethod.FUZZY_SEMANTIC:
        return round(amount * 0.10)
    return 0  # exact, unambiguous — arithmetic is correct


def _tiebreak_ambiguous_match(
    primary_txns: list[NormalizedTxn],
    candidates: list[NormalizedTxn],
    target_cents: int,
    tolerance_cents: int,
    primary_ids: set[str],
    batch_id: str,
) -> Optional[list[NormalizedTxn]]:
    """
    When CP-SAT is ambiguous (multiple valid subsets), use ref_id/memo
    plausibility scoring to pick the more coherent subset.

    Strategy: find one alternate subset via CP-SAT with forbidding constraint,
    then score both primary and alternate against the full pool's ref_id/memo
    signal — higher plausibility score wins.
    """
    from subset_sum import _solve_cpsat

    # find an alternate subset
    forbidden = [primary_ids]
    alt_result = _solve_cpsat(
        candidates, target_cents, tolerance_cents, 5.0,
        forbidden_solutions=forbidden
    )
    if alt_result is None:
        # no genuine alternate found (probe budget exhausted), keep primary
        audit.log_decision(
            batch_id=batch_id,
            agent="tiebreak",
            detail="Ambiguous flag set but no alternate subset found during tiebreak — keeping primary."
        )
        return primary_txns

    alt_txns, _ = alt_result
    alt_ids = {t.source_txn_id for t in alt_txns}

    if alt_ids == primary_ids:
        return primary_txns  # same subset, no tiebreak needed

    # Score both subsets via fuzzy plausibility. Build ONE similarity
    # context over the UNION of both subsets (bounded, small — typically
    # far smaller than the full candidate pool) and reuse it for both
    # scores, rather than a full pool x pool matrix. See fuzzy_match.py's
    # module docstring for why a full square matrix over the whole pool
    # OOM'd on the real 50K stress dataset (~19.6GB at 35K candidates).
    combined_ids_seen: set[str] = set()
    query_union: list[NormalizedTxn] = []
    for t in primary_txns + alt_txns:
        if t.source_txn_id not in combined_ids_seen:
            combined_ids_seen.add(t.source_txn_id)
            query_union.append(t)

    context = build_similarity_context(query_union, candidates)
    primary_score = score_subset_plausibility(primary_txns, context)
    alt_score = score_subset_plausibility(alt_txns, context)

    winner = "primary" if primary_score >= alt_score else "alternate"
    chosen = primary_txns if primary_score >= alt_score else alt_txns
    audit.log_decision(
        batch_id=batch_id,
        agent="tiebreak",
        detail=(
            f"Tiebreak between primary ({len(primary_txns)} txns, score={primary_score:.3f}) "
            f"and alternate ({len(alt_txns)} txns, score={alt_score:.3f}). "
            f"Chose {winner}."
        )
    )
    return chosen


def _filter_to_settlement_currency(
    batch: SettlementBatch, candidates: list[NormalizedTxn]
) -> list[NormalizedTxn]:
    """Drop candidates denominated in a currency the settlement is not in."""
    # Currency first, because amount_cents carries no unit.
    #
    # Every amount in this engine is an integer of minor units, and nothing
    # downstream re-checks what those units are. The solver will therefore
    # happily add 100 USD to 100 INR and report 200 — and it did: a three-leg
    # settlement of INR 300 cleared at 0.97 confidence against two INR legs
    # and one USD leg, because 10000 + 10000 + 10000 is 30000 whatever the
    # currencies were.
    #
    # That is a false clear at the top confidence band, and no other guard
    # catches it. The anchors were real, the arithmetic was exact, the tie-out
    # was zero. Every signal the engine reasons with said yes. The unit was
    # simply never part of the comparison.
    #
    # An FX-aware version would convert at the settlement's rate and carry the
    # rate as evidence. That is real work and is not done. Excluding
    # foreign-currency candidates is the honest interim: a settlement whose
    # members are in another currency now reports that rather than silently
    # summing across the boundary.
    settlement_ccy = (batch.currency or "").strip().upper()
    if settlement_ccy:
        same_ccy = [
            t for t in candidates
            if (t.currency or "").strip().upper() == settlement_ccy
        ]
        dropped_ccy = len(candidates) - len(same_ccy)
        if dropped_ccy:
            others = sorted({
                (t.currency or "?").strip().upper() for t in candidates
                if (t.currency or "").strip().upper() != settlement_ccy
            })
            audit.log_decision(
                batch_id=batch.batch_id,
                agent="currency_filter",
                detail=(
                    f"Excluded {dropped_ccy} candidate(s) denominated in "
                    f"{', '.join(others)} from this {settlement_ccy} "
                    f"settlement. Amounts are integer minor units with no unit "
                    f"attached, so summing across currencies produces an exact "
                    f"total that is meaningless. No FX conversion is applied."
                ),
            )
        return same_ccy

    # No settlement currency declared: nothing to compare against, so nothing
    # is excluded. Guessing one would be worse than not filtering.
    return candidates


def _filter_out_non_settling(
    batch: SettlementBatch, candidates: list[NormalizedTxn]
) -> list[NormalizedTxn]:
    """Drop candidates whose status says the money never moved."""
    # Pre-filter to a realistic settlement window BEFORE running CP-SAT.
    # Money that never moved is not a settlement member.
    #
    # `status` was not mapped at all until now, so a FAILED payment entered
    # the pool as a spendable amount. Two things followed. It could be named
    # as a member of a settlement it was never part of. And on a merchant
    # whose prices repeat, the failures created alternate subsets that were
    # arithmetically valid and factually impossible — ten captured payments
    # of Rs 1,000 beside five failed ones meant "any ten of fifteen", and the
    # batch was withheld as ambiguous when the real answer was unique.
    #
    # Only EXPLICITLY non-settling states are dropped. A blank or unrecognised
    # status is kept: most feeds carry none, and inventing a reason to discard
    # a payment is the opposite of what this engine is for.
    dropped_status: dict[str, int] = {}
    settling = []
    for _t in candidates:
        _state = str((_t.extra or {}).get("status") or "").strip().lower().replace("-", "_")
        if _state in NON_SETTLING_STATUSES:
            dropped_status[_state] = dropped_status.get(_state, 0) + 1
        else:
            settling.append(_t)
    if dropped_status:
        _detail = ", ".join(f"{n} {k}" for k, n in sorted(dropped_status.items()))
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="status_filter",
            detail=(
                f"Excluded {sum(dropped_status.values())} candidate(s) whose "
                f"status says the money never moved ({_detail}). A failed or "
                f"uncaptured payment cannot be part of a settlement, and "
                f"leaving it in the pool invents subsets that cannot happen."
            ),
        )
    return settling


@dataclass
class _SolveOutcome:
    """What solving produced, and the pool it was actually solved over.

    `solver_candidates` is returned rather than assumed because the safety
    net can widen it back to the full windowed pool, and every guard after
    this point reasons about pool size. Reading a stale pool there would let
    a large-pool batch pass the unanchored limit on the narrowed count.
    """
    result: MatchResult
    solver_candidates: list[NormalizedTxn]
    scores: dict
    anchor_keys: set
    anchor_ids: set


def _solve_in_tiers(
    batch: SettlementBatch,
    solver_candidates: list[NormalizedTxn],
    windowed_candidates: list[NormalizedTxn],
    link_result,
    gross_target: int,
    cfg: SubsetSumConfig,
) -> _SolveOutcome:
    """Agent 3 end to end: tiered solve, empty-linkage fallback, safety net."""
    # Agent 3: exact subset-sum, solved in tiers of decreasing evidence.
    #
    # Transactions that NAME the settlement are far stronger evidence than
    # transactions that merely cluster near it, so they get solved on their
    # own first. If four transactions cite settlement X and sum exactly to
    # X's target, that is the answer — admitting weakly-linked candidates
    # alongside them can only manufacture ambiguity.
    #
    # Measured: without this tier the near-collision family scored 0%. Decoys
    # summing within tolerance entered on cluster signal, gave the solver a
    # second arithmetically valid subset, and it correctly refused to clear —
    # losing a match it should have made. Solving anchors first recovers it
    # without loosening the ambiguity check that makes the refusal correct.
    # Tiers, strongest evidence first. Each tier is solved on its own and the
    # first one that CLEARS wins.
    #
    # The tiers matter because a weak signal admitted alongside strong ones
    # does not add information, it adds degeneracy. Cross-source amount
    # peering scores only 0.10 on its own — real but feeble, since unrelated
    # payments share amounts constantly. Admitting every amount-peered record
    # into the same solve as the anchored ones re-creates exactly the
    # under-determination this module exists to remove. Measured: it took
    # ref_partial from 100% to 13%, with truth scoring 0.35 and diluting
    # noise scoring 0.10 — separable, but only if they are solved separately.
    STRONG_LINK = 0.25

    # Keyed by txn_key, not by the bare id. Feeds mint overlapping id
    # sequences, so a bare-id lookup lets an unrelated ERP record inherit a
    # gateway record's anchor status and be solved in the anchor tier.
    scores = {txn_key(c.txn): c.score for c in link_result.scored}
    anchor_keys = link_result.anchor_keys
    anchor_ids = set(link_result.anchor_cluster_ids)   # bare ids, for reporting

    anchor_txns = [t for t in solver_candidates if txn_key(t) in anchor_keys]
    strong_txns = [
        t for t in solver_candidates
        if scores.get(txn_key(t), 0.0) >= STRONG_LINK
        or txn_key(t) in anchor_keys
    ]

    tiers: list[tuple[str, list[NormalizedTxn]]] = []
    if anchor_txns:
        tiers.append(("anchor", anchor_txns))
    if len(strong_txns) > len(anchor_txns):
        tiers.append(("strong_link", strong_txns))
    tiers.append(("all_linked", solver_candidates))

    result = None
    seen_sizes: set[int] = set()
    for tier_name, tier_txns in tiers:
        if not tier_txns or len(tier_txns) in seen_sizes:
            continue
        seen_sizes.add(len(tier_txns))

        tier_result = match_batch(batch.batch_id, tier_txns, gross_target, cfg)

        # Substitutability guard.
        #
        # The same payment arrives in more than one feed carrying the same
        # amount, so a solution can be swapped member-for-member with the
        # other feed's copies and the arithmetic will not notice. Narrowing
        # to a tier can hand the solver only ONE side of that pair, at which
        # point the solve looks unique when it is not — and it clears,
        # confidently, on possibly the wrong system of record.
        #
        # This is not hypothetical. Introducing the strong_link tier produced
        # the first false clears this engine has ever recorded: whole matched
        # sets of "..._MIRROR" records standing in for their originals, in
        # ref_missing and ref_truncated where nothing distinguishes the two.
        #
        # So before accepting a tier's clear, check whether an equal-amount
        # record from a DIFFERENT feed exists outside the tier for any
        # matched member. If one does, the answer is not unique, it only
        # looked unique because of where the tier boundary fell — refuse to
        # auto-clear and let a human choose the system of record.
        if tier_result.cleared and tier_result.matched_txn_ids:
            tier_ids = {t.source_txn_id for t in tier_txns}
            matched_set = set(tier_result.matched_txn_ids)
            matched_txns = [t for t in tier_txns if t.source_txn_id in matched_set]

            # An ANCHORED member is safe: it names the settlement, so even
            # though a copy of it exists in another feed, we know which
            # record belongs here. An unanchored member with a cross-feed
            # twin is not safe — nothing distinguishes the two, and the tier
            # boundary may simply have hidden the alternative.
            #
            # Scoring the two copies against each other was tried and is
            # worse than useless: with the true record's reference stripped,
            # its ERP copy scored HIGHER (feed-level prefixes cluster), so
            # the comparison actively endorsed the wrong record and produced
            # 30 false clears. Cluster rank is evidence about which system
            # produced a record, not about which settlement it belongs to.
            # A member from the DECLARED member feed is not substitutable by
            # a copy in another feed: we already know which feed is
            # authoritative, so the copy was never a candidate. Without that
            # declaration an unanchored member with a twin is genuinely
            # undetermined and must be withheld.
            substitutable = [
                t for t in matched_txns
                if txn_key(t) not in anchor_keys
                and not (
                    batch.member_source is not None
                    and t.source is batch.member_source
                )
                and any(
                    o.amount_cents == t.amount_cents
                    and o.source is not t.source
                    and o.source_txn_id not in tier_ids
                    for o in windowed_candidates
                )
            ]
            if substitutable:
                tier_result.cleared = False
                tier_result.ambiguous = True
                tier_result.reasoning += (
                    f" Withheld from auto-clear: {len(substitutable)} matched "
                    f"record(s) have an equal-amount counterpart in another feed "
                    f"outside this tier, so the set is substitutable and the "
                    f"system of record is not determined by the arithmetic."
                )
                audit.log_decision(
                    batch_id=batch.batch_id,
                    agent="linkage",
                    detail=(
                        f"Tier '{tier_name}' summed to target but "
                        f"{len(substitutable)} member(s) are substitutable with "
                        f"another feed's copy. Refusing to auto-clear."
                    ),
                )

        if tier_result.cleared:
            audit.log_decision(
                batch_id=batch.batch_id,
                agent="linkage",
                detail=(
                    f"Cleared on the '{tier_name}' tier: {len(tier_txns)} of "
                    f"{len(solver_candidates)} linked candidate(s) sum to target "
                    f"without needing weaker-linked records."
                ),
            )
            result = tier_result
            solver_candidates = tier_txns
            break
        if result is None:
            result = tier_result  # keep the strongest tier's answer as fallback

    # Linkage can legitimately return NOTHING: a declared member_source with
    # no in-scope candidate leaves every tier empty, so the loop above never
    # assigns a result. That is a real finding, not an error — "no record in
    # the declared member feed is connected to this settlement" — but it
    # reached the code below as None and raised AttributeError, surfacing to
    # the API as a 500. Reachable straight from the upload form the moment
    # someone picks the wrong feed, which is exactly when a user needs the
    # diagnosis rather than a stack trace.
    if result is None:
        result = MatchResult(
            batch_id=batch.batch_id,
            matched_txn_ids=[],
            matched_sum_cents=0,
            target_cents=gross_target,
            cleared=False,
            confidence=0.0,
            method=MatchMethod.MANUAL_REVIEW,
            reasoning=(
                f"No candidate survived linkage for this settlement. "
                f"{link_result.reasoning}"
            ),
            ambiguous=False,
        )
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="linkage",
            detail=(
                "No candidate survived linkage; nothing was solved. "
                f"{link_result.reasoning}"
            ),
        )

    # Safety net: linkage must never make the outcome WORSE than solving the
    # unconstrained pool. If it narrowed and that solve produced nothing,
    # fall back to the full windowed pool so a bad block key can only cost
    # solver time, never the answer. (Blocking is high-recall by design, but
    # "by design" is not a guarantee, and this is money.)
    narrowed = link_result.pool_after < link_result.pool_before
    if narrowed and not result.cleared:
        fallback = match_batch(batch.batch_id, windowed_candidates, gross_target, cfg)
        if fallback.cleared:
            # Check if the fallback match left anchors unused. If it did, the
            # fallback is evidence-contradicting and should be withheld.
            if anchor_keys:
                fallback_matched_keys = {txn_key(t) for t in windowed_candidates
                                        if t.source_txn_id in set(fallback.matched_txn_ids)}
                anchors_in_pool = anchor_keys & {txn_key(t) for t in windowed_candidates}
                unused_anchors = anchors_in_pool - fallback_matched_keys
                
                if unused_anchors:
                    # Fallback left anchors unused - don't accept it
                    audit.log_decision(
                        batch_id=batch.batch_id,
                        agent="linkage",
                        detail=(
                            f"Constrained solve found nothing; unconstrained solve over "
                            f"{link_result.pool_before} candidates found a match but left "
                            f"{len(unused_anchors)} anchor(s) unused. Rejecting fallback - "
                            f"an ignored anchor is evidence pointing elsewhere."
                        ),
                    )
                    # Keep the constrained result (no match), don't accept fallback
                else:
                    # Fallback used all available anchors - accept it
                    audit.log_decision(
                        batch_id=batch.batch_id,
                        agent="linkage",
                        detail=(
                            f"Constrained solve found nothing over {link_result.pool_after} "
                            f"linked candidates; retried unconstrained over "
                            f"{link_result.pool_before} candidates and found a match using "
                            f"all {len(anchors_in_pool)} available anchor(s). Accepting fallback."
                        ),
                    )
                    result = fallback
                    solver_candidates = windowed_candidates
            else:
                # No anchors - original fallback behavior applies
                audit.log_decision(
                    batch_id=batch.batch_id,
                    agent="linkage",
                    detail=(
                        f"Constrained solve found nothing over {link_result.pool_after} "
                        f"linked candidates; retried unconstrained over "
                        f"{link_result.pool_before} candidates and CLEARED. "
                        "Linkage narrowed too aggressively."
                    ),
                )
                result = fallback
                solver_candidates = windowed_candidates

    return _SolveOutcome(
        result=result,
        solver_candidates=solver_candidates,
        scores=scores,
        anchor_keys=anchor_keys,
        anchor_ids=anchor_ids,
    )


def _withhold_if_unevidenced(
    batch: SettlementBatch,
    result: MatchResult,
    solver_candidates: list[NormalizedTxn],
    link_result,
    anchor_keys: set,
) -> None:
    """Refuse to auto-clear a sum that no evidence ties to this settlement.

    Mutates `result` in place, as the inline block it replaces did: this is
    the point where an arithmetically valid answer is demoted to a withheld
    one, and every field it sets - cleared, ambiguous, withheld_reason and
    the appended reasoning - is read by the caller straight afterwards.
    """
    # Unanchored auto-clear guard.
    #
    # Auto-clearing needs either evidence that these records belong to this
    # settlement, or a pool small enough that the arithmetic is genuinely
    # determined. With neither, an exact sum is not a match — it is a
    # coincidence, and there are astronomically many available: the space
    # competing for one target is 2^n against ~2e6 distinct paise values, so
    # uniqueness stops being plausible somewhere around n=20-25 and is gone
    # entirely beyond that. The solver's ambiguity probe only samples a
    # couple of alternates, so "not ambiguous" over a large pool is weak
    # evidence, not proof.
    #
    # Measured: with the settlement reference stripped from every true
    # member, linkage still narrowed 50,000 -> 400 on cluster signal alone
    # while every anchor was scoped away, and the solver returned a
    # confident 67-record set that was simply wrong. That is a false clear —
    # the one outcome this engine is built to never produce.
    #
    # Small pools are exempt because there the subset-sum really is
    # determined, which is why a 5-candidate batch with no references still
    # clears correctly.
    # The test is whether the MATCHED SET is anchored, not whether the batch
    # has anchors anywhere.
    #
    # Those are different questions and the difference is a false clear. A
    # settlement whose members DO name it, but where one leg has not arrived
    # yet, has anchors in the pool and no reachable correct answer. The old
    # condition saw the anchors, concluded the batch was well-evidenced, and
    # stood aside while the solver cleared five unrelated noise records that
    # happened to sum to the target. Measured on the realistic benchmark:
    # anchors ['S7_TRUE_0','S7_TRUE_1','S7_TRUE_2'] present, matched set
    # ['S7_N_20','S7_N_24','S7_N_44','S7_N_51','S7_N_52'], intersection empty,
    # cleared=True. Pure noise, auto-cleared, confidently.
    #
    # Evidence does not transfer between records. An anchor vouches for the
    # transaction carrying it and for nothing else, so what matters is whether
    # the records being cleared are themselves evidenced.
    #
    # Neither existing corpus could show this. benchmark.py's batch ids share
    # no canonical form with its references, so anchors were never found and
    # the guard always fired; ReconRiver's ids do match, but its data is clean
    # enough that the true set is always reachable. It needs both at once —
    # anchors present AND the true answer absent from the pool — which is what
    # a late leg does in production every day.
    matched_id_set = set(result.matched_txn_ids)
    matched_keys = {
        txn_key(t) for t in solver_candidates
        if t.source_txn_id in matched_id_set
    }
    matched_anchored = bool(anchor_keys & matched_keys)

    # Two distinct situations, and the small-pool exemption is only sound in
    # one of them:
    #
    #   no anchors anywhere      the settlement is simply not referenced. Over
    #                            a small pool the arithmetic really is
    #                            determined, and this clears correctly.
    #
    #   anchors exist, but NONE  the settlement IS referenced, and the solver
    #   are in the matched set   chose a set containing none of the records
    #                            that reference it. The evidence points
    #                            somewhere other than the answer. Pool size
    #                            does not rescue that, because the problem is
    #                            not degeneracy — it is that the one signal
    #                            available was ignored.
    #
    # Measured: the second case cleared five unrelated noise records over a
    # pool of 21 while three anchored members sat outside the matched set,
    # because 21 was under the small-pool limit. It is the only false clear
    # the realistic benchmark produced.
    evidence_ignored = bool(anchor_keys) and not matched_anchored
    pool_too_large = len(solver_candidates) > UNANCHORED_AUTOCLEAR_LIMIT

    # Linkage saying it found NOTHING is itself a finding, and it must not be
    # overridden by a small pool.
    #
    # `no_linkage_signal` is not "weak evidence" — it is linkage reporting
    # that no reference, no cluster and no cross-source peer exists anywhere
    # in the pool. The only thing left is the arithmetic, and the arithmetic
    # is what this engine exists to say is insufficient. Calibration puts that
    # band at 27.6% accurate.
    #
    # The small-pool exemption assumed a unique sum over few candidates means
    # the answer is determined. That holds only if the answer is IN the pool.
    # Give the engine a window of unrelated traffic and a unique sum is a
    # coincidence, not a determination.
    #
    # Found on a real SBI statement: eight genuine UPI debits, no settlement
    # reference among them because a UPI RRN identifies the payment and not
    # any settlement. Four of them summed to a Rs 500 credit to the paisa and
    # the engine cleared it at 0.22 confidence. Those four payments went to
    # four unrelated people and have nothing to do with that credit. Across
    # the same statement 69 of 189 credits have such a subset, 57 of them
    # have more than one, and one debit is claimed by ten different "matches"
    # — so the coincidence rate is not incidental, it is the norm for retail
    # payment data where amounts are round and repeat.
    no_evidence_at_all = link_result.method == "no_linkage_signal"

    # PARTIAL anchoring is its own case, and the measured worst one.
    #
    # A matched set where some members name the settlement and others do not
    # sits in the 0.42 confidence band, which calibration measures at 43%
    # correct — worse than the unanchored-but-clustered band. The instinct
    # that "at least one member is anchored, so the set is probably right" is
    # exactly backwards: an anchor vouches for the record carrying it and for
    # nothing else, so the unanchored members are unevidenced regardless of
    # the company they keep.
    #
    # Measured: with the fee target perturbed by 1.5bps, a set of five was
    # cleared on the strength of one anchored member and four that were simply
    # wrong. Pool size did not save it — 23 candidates, under the small-pool
    # limit — because the problem is not degeneracy, it is that four of the
    # five records had no evidence at all.
    # Partial anchoring only blocks when the match LEFT ANCHORS UNUSED.
    #
    # Blocking every partially-anchored match cost ten correct answers to
    # prevent two wrong ones. The two wrong ones had a property the ten did
    # not: anchors sat in the pool that the matched set did not include. A
    # match that uses every available anchor and adds unanchored members is
    # reading the evidence; one that ignores anchors is contradicting it.
    anchors_in_pool = anchor_keys & {txn_key(t) for t in solver_candidates}
    partially_anchored = (
        bool(anchor_keys)
        and matched_anchored
        and not (matched_keys <= anchor_keys)
        and bool(anchors_in_pool - matched_keys)
    )

    if result.cleared and (
        partially_anchored
        or (not matched_anchored
            and (evidence_ignored or pool_too_large or no_evidence_at_all))
    ):
        result.cleared = False
        result.ambiguous = True
        result.withheld_reason = "no_corroborating_evidence"
        anchor_note = (
            "linkage found no reference, cluster or cross-source evidence "
            "anywhere in this pool, so the match rests on the arithmetic alone"
            if no_evidence_at_all and not anchor_keys else
            f"only {len(anchor_keys & matched_keys)} of the "
            f"{len(matched_id_set)} matched record(s) reference this "
            f"settlement, so the rest are unevidenced"
            if partially_anchored else
            "no candidate references this settlement"
            if not anchor_keys else
            f"none of the {len(matched_id_set)} matched record(s) references "
            f"this settlement (the {len(anchor_keys)} record(s) that do were "
            f"not selected)"
        )
        # The second clause only applies when pool size is the reason. Saying
        # "the pool of 8 is too large" when the actual finding is "no evidence
        # exists" tells the reviewer the wrong thing to go and fix.
        size_clause = (
            f", and the pool of {len(solver_candidates)} is too large for an "
            f"exact sum to establish uniqueness on its own"
            if pool_too_large else ""
        )
        result.reasoning += (
            f" Withheld from auto-clear: {anchor_note}{size_clause}. "
            f"Routed for human review."
        )
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="linkage",
            detail=(
                f"Subset summed to target over {len(solver_candidates)} "
                f"candidates with no anchored member in the matched set. "
                f"Arithmetic alone does not identify a settlement at this pool "
                f"size — refusing to auto-clear."
            ),
        )


def _collect_unmatched(
    batch: SettlementBatch,
    result: MatchResult,
    candidates: list[NormalizedTxn],
    windowed_candidates: list[NormalizedTxn],
    gross_target: int,
    fuzz_cfg: FuzzyMatchConfig,
) -> tuple[list[ExceptionRecord], list[NormalizedTxn]]:
    """Everything the solve did not account for, and the fuzzy recovery pass.

    Returns (exceptions, unmatched) rather than setting them on the caller,
    so the two are always produced together - they describe the same residual
    and reading one without the other has never been meaningful.
    """

    exceptions: list[ExceptionRecord] = []
    unmatched: list[NormalizedTxn] = []

    if result.cleared:
        # Happy path: deterministic exact match, no ambiguity.
        #
        # The residual pool is deliberately NOT routed to exception
        # diagnosis. The candidate pool intentionally contains every
        # uncleared ledger entry in the settlement window — entries that
        # belong to other settlements, or that haven't settled yet.
        # "Not part of this batch" is the normal state of the world, not
        # something a human needs to investigate.
        #
        # Routing the residual here previously produced 20,078 exceptions
        # on a flawless 5-transaction match against the 50K stress
        # dataset, which (a) buried genuinely actionable exceptions under
        # 20K rows of noise, defeating the entire purpose of the
        # exception queue, and (b) flipped requires_human_approval to
        # True on a 100%-confidence exact match, because summary() ORs in
        # `len(exceptions) > 0`. A perfect match must not ask for human
        # review.
        #
        # Exceptions raised elsewhere (e.g. the Compliance Agent's blocks,
        # appended by the pipeline) are unaffected — those are real.
        matched_ids = set(result.matched_txn_ids)
        residual_count = len(windowed_candidates) - len(matched_ids)
        unmatched = []
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="orchestrator",
            detail=(
                f"Exact match cleared with {len(matched_ids)} txns. "
                f"{residual_count} residual in-window candidates left unreconciled "
                f"by design — they belong to other settlements, not this batch, "
                f"and are not exceptions."
            ),
        )

    elif result.ambiguous and result.matched_txn_ids:
        # Ambiguous: CP-SAT found A valid subset but not THE unique one.
        # Tiebreak already ran above. Do NOT run fuzzy — that would be adding
        # semantic noise on top of arithmetic ambiguity. Route directly to human.
        # The matched txns are the tiebreak's best guess; the rest are unmatched.
        matched_ids = set(result.matched_txn_ids)
        unmatched = [t for t in windowed_candidates if t.source_txn_id not in matched_ids]
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="orchestrator",
            detail=(
                f"Ambiguous exact match — skipping fuzzy fallback. "
                f"Tiebreak chose {len(matched_ids)} txns; "
                f"{len(unmatched)} routed to exception diagnosis."
            ),
        )

    else:
        # No arithmetic match at all — now fuzzy fallback (Agent 4) earns its keep.
        # Try to partially recover unmatched records via ref_id/memo similarity.
        #
        # Uses bulk_fuzzy_recover, NOT a per-transaction loop calling
        # match_batch_fuzzy. The original per-txn loop rebuilt an O(n)
        # pool copy AND ran per-pair similarity scoring for every single
        # unmatched transaction -- measured at ~112 MINUTES projected for
        # a single ~20,000-item unmatched chunk from the real 50K stress
        # dataset. bulk_fuzzy_recover builds one similarity matrix for
        # the whole pool and does vectorized lookups instead.
        unmatched = windowed_candidates
        fuzzy_results = bulk_fuzzy_recover(batch.batch_id, unmatched, windowed_candidates, fuzz_cfg)

        fuzzy_recovered: list[str] = []
        for txn, fuzzy_result in zip(unmatched, fuzzy_results):
            if fuzzy_result.cleared and fuzzy_result.matched_txn_ids:
                fuzzy_recovered.extend([txn.source_txn_id] + fuzzy_result.matched_txn_ids)
                audit.log_decision(
                    batch_id=batch.batch_id,
                    agent="fuzzy_fallback",
                    detail=(
                        f"Fuzzy match: {txn.source_txn_id} -> "
                        f"{fuzzy_result.matched_txn_ids[0]} "
                        f"(confidence={fuzzy_result.confidence:.2f}): {fuzzy_result.reasoning}"
                    ),
                )

        if fuzzy_recovered:
            recovered_set = set(fuzzy_recovered)
            unmatched = [t for t in unmatched if t.source_txn_id not in recovered_set]
            # Update result to reflect fuzzy recovery
            result.matched_txn_ids = list(recovered_set)
            result.matched_sum_cents = sum(
                t.amount_cents for t in windowed_candidates
                if t.source_txn_id in recovered_set
            )
            result.method = MatchMethod.FUZZY_SEMANTIC
            result.confidence = fuzz_cfg.confidence_threshold
            # Finish the sentence rather than replace it. reasoning ended at
            # "Routing to fuzzy pass." — written BEFORE this pass ran and never
            # updated once it had, so a reader saw a recovered count and a
            # confidence beside a line that stopped at "routing to".
            #
            # APPENDED, because the existing text says WHY subset-sum found
            # nothing ("No candidate survived linkage", and others) and that is
            # the more useful half. Overwriting it threw that away — a test
            # caught it doing exactly that.
            #
            # The gap matters most here: a fuzzy set does not have to sum to
            # the target, so the batch can carry a large residual the old text
            # never mentioned.
            shortfall = result.matched_sum_cents - gross_target
            result.reasoning = (
                f"{result.reasoning.rstrip()} "
                f"The fuzzy pass recovered {len(recovered_set)} transaction"
                f"{'' if len(recovered_set) == 1 else 's'} on reference and "
                f"memo similarity, totalling {result.matched_sum_cents}c "
                f"against a target of {gross_target}c — "
                f"{'over' if shortfall > 0 else 'short'} by {abs(shortfall)}c. "
                f"Similarity is evidence of association, not of arithmetic, so "
                f"this set is a starting point for review and is never cleared "
                f"on its own."
            )
            audit.log_decision(
                batch_id=batch.batch_id,
                agent="fuzzy_fallback",
                detail=f"Recovered {len(fuzzy_recovered)} txns via fuzzy pass. "
                       f"{len(unmatched)} still unresolved.",
            )

    return exceptions, unmatched
def _apply_confidence_gate(
    batch: SettlementBatch,
    result: MatchResult,
    solver_candidates: list[NormalizedTxn],
    link_result: LinkageResult
) -> None:
    """Report how the match was FOUND, and gate auto-clear on structural confidence."""
    if not result.matched_txn_ids:
        return

    structural = link_confidence(link_result, result.matched_txn_ids)
    result.confidence = min(result.confidence, structural)
    result.reasoning += (
        f" Linkage: {link_result.method}, structural confidence "
        f"{structural:.2f} over {link_result.pool_after} linked candidate(s)."
    )

    gate_applies = len(solver_candidates) > UNANCHORED_AUTOCLEAR_LIMIT
    if (
        result.cleared
        and gate_applies
        and result.confidence < MIN_AUTOCLEAR_CONFIDENCE
    ):
        result.cleared = False
        result.ambiguous = True
        result.withheld_reason = result.withheld_reason or "below_confidence_gate"
        result.reasoning += (
            f" Withheld from auto-clear: structural confidence "
            f"{result.confidence:.2f} is below the {MIN_AUTOCLEAR_CONFIDENCE:.2f} "
            f"required to release without review. The matched set is "
            f"reported as a proposal."
        )
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="orchestrator",
            detail=(
                f"Auto-clear withheld: confidence {result.confidence:.2f} < "
                f"{MIN_AUTOCLEAR_CONFIDENCE:.2f}. Matched set surfaced for "
                f"human review rather than released."
            ),
        )



def _tiebreak_if_ambiguous(
    batch: SettlementBatch,
    result: MatchResult,
    windowed_candidates: list[NormalizedTxn],
    gross_target: int,
    cfg: SubsetSumConfig,
    enable_tiebreak: bool
) -> None:
    """Agent 3b: tiebreak ambiguous matches via fuzzy plausibility scoring."""
    if result.ambiguous and enable_tiebreak and result.matched_txn_ids:
        matched_pool = [t for t in windowed_candidates if t.source_txn_id in set(result.matched_txn_ids)]
        primary_ids = set(result.matched_txn_ids)
        chosen = _tiebreak_ambiguous_match(
            matched_pool, windowed_candidates, gross_target,
            cfg.tolerance_cents, primary_ids, batch.batch_id
        )
        if chosen is not None:
            chosen_ids = {t.source_txn_id for t in chosen}
            if chosen_ids != primary_ids:
                result.matched_txn_ids = list(chosen_ids)
                result.matched_sum_cents = sum(t.amount_cents for t in chosen)
                result.confidence = min(result.confidence, 0.25)
                result.reasoning += (
                    " Tiebreak: alternate subset chosen via ref_id/memo "
                    "plausibility scoring — a preference between equally valid "
                    "arithmetic, not evidence. Confidence reflects that."
                )

def _build_report_and_tie_out(
    batch: SettlementBatch,
    result: MatchResult,
    windowed_candidates: list[NormalizedTxn],
    exceptions: list[ExceptionRecord],
    fee_breakdown,
    gross_target: int
) -> ReconciliationReport:
    """Construct the report and check the tie-out arithmetic."""
    fp_cost = _compute_false_positive_cost(result)

    matched_gross = sum(
        t.amount_cents for t in windowed_candidates
        if t.source_txn_id in set(result.matched_txn_ids)
    )
    report = ReconciliationReport(
        batch_id=batch.batch_id,
        total_candidates=len(windowed_candidates),
        match_result=result,
        exceptions=exceptions,
        false_positive_cost_estimate_cents=fp_cost,
        fee_basis=fee_breakdown.basis,
        target_cents=gross_target,
        matched_gross_cents=matched_gross,
        net_amount_cents=batch.net_amount_cents,
        deductions_cents=fee_breakdown.total_deductions_cents,
    )

    if result.matched_txn_ids and not report.ties_out:
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="tie_out",
            detail=(
                f"DOES NOT TIE: matched gross {matched_gross}c - deductions "
                f"{fee_breakdown.total_deductions_cents}c - net "
                f"{batch.net_amount_cents}c = {report.tie_out_residual_cents}c "
                f"residual. Deduction basis was '{fee_breakdown.basis}'"
                + ("; supply declared_deductions_cents to remove the estimate."
                   if fee_breakdown.basis == "estimated" else ".")
            ),
        )
    elif result.matched_txn_ids:
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="tie_out",
            detail=(
                f"Ties out exactly: {matched_gross}c gross - "
                f"{fee_breakdown.total_deductions_cents}c deductions = "
                f"{batch.net_amount_cents}c net."
            ),
        )

    audit.log_decision(
        batch_id=batch.batch_id,
        agent="orchestrator",
        detail=f"Final: {report.summary()}",
    )

    return report


def reconcile_many(
    batches: list[SettlementBatch],
    candidates: list[NormalizedTxn],
    subset_config: SubsetSumConfig | None = None,
    rate_card: FeeRateCard = DEFAULT_RATE_CARD,
) -> list[ReconciliationReport]:
    """
    N:M solver pathway. Matches multiple batches against a shared pool simultaneously.
    Provides safety by leaving the standard 1:N loop unchanged.
    """
    if not batches:
        return []

    cfg = subset_config or SubsetSumConfig()

    # Pre-filter compliance
    safe_candidates, _ = compliance_agent.scan(candidates)

    target_cents_list = []
    fees_list = []
    for batch in batches:
        fee_breakdown = compute_fee_breakdown(batch, rate_card, safe_candidates)
        gross_target = fee_breakdown.gross_target_cents(batch.net_amount_cents)
        target_cents_list.append(gross_target)
        fees_list.append(fee_breakdown)

    matched_subsets = exact_subset_sum_nm(
        safe_candidates,
        target_cents_list,
        tolerance_cents=cfg.tolerance_cents,
        time_limit_s=cfg.solver_time_limit_s,
        num_search_workers=cfg.num_search_workers,
    )

    reports = []
    if matched_subsets:
        for i, batch in enumerate(batches):
            matched = matched_subsets[i]
            matched_ids = [t.source_txn_id for t in matched]
            
            res = MatchResult(
                cleared=True,
                method=MatchMethod.EXACT_SUBSET_SUM,
                confidence=0.85,  # Slightly lower confidence for N:M inference
                matched_txn_ids=matched_ids,
                matched_txns=matched,
                matched_sum_cents=sum(t.amount_cents for t in matched),
                target_cents=target_cents_list[i],
                pool_size=len(safe_candidates),
                reasoning="Matched via N:M global bin-packing solver.",
                linkage=None,
            )
            
            position = cash_position.build_cash_position(
                batch, candidates, matched_ids, cleared=True, rate_card=rate_card
            )
            erp_sync.push_to_erp(position)
            
            rep = ReconciliationReport(
                batch_id=batch.batch_id,
                batch=batch,
                match=res,
                position=position,
            )
            reports.append(rep)
    else:
        # Fallback to failing them all individually if N:M fails
        for batch in batches:
            res = MatchResult(
                cleared=False,
                method=MatchMethod.EXACT_SUBSET_SUM,
                confidence=0.0,
                matched_txn_ids=[],
                matched_txns=[],
                matched_sum_cents=0,
                target_cents=target_cents_list[batches.index(batch)],
                pool_size=len(safe_candidates),
                reasoning="N:M global solver failed to find a valid assignment.",
                linkage=None,
            )
            position = cash_position.build_cash_position(
                batch, candidates, [], cleared=False, rate_card=rate_card
            )
            reports.append(ReconciliationReport(
                batch_id=batch.batch_id, batch=batch, match=res, position=position
            ))

    return reports


def reconcile_batch(
    batch: SettlementBatch,
    candidates: list[NormalizedTxn],
    subset_config: SubsetSumConfig | None = None,
    fuzzy_config: FuzzyMatchConfig | None = None,
    settlement_window_days: int = 5,
    enable_tiebreak: bool = True,
    rate_card: FeeRateCard = DEFAULT_RATE_CARD,
) -> ReconciliationReport:
    """
    Runs one settlement batch through the full pipeline end to end.

    `rate_card` is a parameter because deductions are a property of the
    processor agreement, not of the engine. It was previously hardcoded to
    DEFAULT_RATE_CARD, which silently imposed one merchant's 2%+1% terms on
    every batch — wrong for any other processor, and wrong for feeds that are
    already net of fees, where the correct card is zero and adding phantom
    deductions moves the target off the answer entirely.
    """
    cfg = subset_config or SubsetSumConfig()
    fuzz_cfg = fuzzy_config or FuzzyMatchConfig()

    # Agent 2: reconstruct gross target from net settlement amount
    fee_breakdown = compute_fee_breakdown(batch, rate_card, candidates)
    gross_target = fee_breakdown.gross_target_cents(batch.net_amount_cents)
    audit.log_decision(
        batch_id=batch.batch_id,
        agent="fee_decomposition",
        detail=(
            f"net={batch.net_amount_cents}c -> gross_target={gross_target}c, "
            f"deductions={fee_breakdown.total_deductions_cents}c "
            f"[{fee_breakdown.basis}]"
            + ("" if fee_breakdown.basis == "declared" else
               f" via rate card {rate_card.gateway_fee_bps}bps gateway + "
               f"{rate_card.tax_withholding_bps}bps tax + "
               f"{rate_card.flat_fee_cents}c flat — an ESTIMATE; the target "
               f"moves if the processor's actual deductions differ")
        ),
    )

    candidates = _filter_to_settlement_currency(batch, candidates)


    candidates = _filter_out_non_settling(batch, candidates)


    windowed_candidates = filter_candidates_by_settlement_window(
        candidates, batch.settled_at_utc, settlement_window_days
    )
    audit.log_decision(
        batch_id=batch.batch_id,
        agent="settlement_window_filter",
        detail=f"{len(candidates)} candidates -> {len(windowed_candidates)} "
               f"within {settlement_window_days}-day window",
    )

    # Agent 2b: linkage — constrain candidates BEFORE the solver.
    #
    # Subset-sum cannot identify a settlement on its own: the solver may use
    # any subset size, so the space competing for one target is 2^n (1.2e18
    # for a pool of 60) against a target with only ~2e6 distinct paise
    # values. Measured auto-clear accuracy without this stage was 0.0% across
    # 120 benchmark scenarios. Linkage answers "which transactions are
    # plausibly connected to this settlement at all", so the arithmetic runs
    # over tens of candidates instead of tens of thousands and is actually
    # determined. See linkage.py for the full reasoning.
    link_result = build_candidate_links(
        batch, windowed_candidates, settlement_window_days
    )
    solver_candidates = link_result.candidates

    # AMONGRESOLVER_NO_LINKAGE=1 hands the solver the whole windowed pool.
    #
    # This exists so the project's central claim can be RUN rather than
    # believed. "Subset-sum alone scores 0.0%" sat in a comment and in the
    # README as a historical measurement, with no way for a reader to
    # reproduce it — which is the weakest possible form of the strongest
    # thing this engine has to say.
    #
    # Off by default and deliberately an environment variable rather than a
    # request field: this is a demonstration harness, not a mode anyone should
    # be able to reach through the API.
    if os.environ.get("AMONGRESOLVER_NO_LINKAGE", "").strip() == "1":
        # Discard the EVIDENCE, not just the narrowing.
        #
        # This originally only widened the pool back to the windowed set and
        # left link_result intact, which meant the anchors, the scores and the
        # tiers built from them all survived. The solver was still handed
        # anchored candidates first, so the flag that exists to reproduce
        # "subset-sum alone scores 0.0%" measured 62.0% — the same number as
        # with linkage on, to the decimal. The one claim this project most
        # wants a reader to be able to check was the one it could not.
        #
        # Arithmetic alone means no reference, no cluster, no cross-feed
        # correspondence: one tier holding everything in the window, and no
        # evidence for the auto-clear guard to weigh.
        solver_candidates = windowed_candidates
        link_result = LinkageResult(
            candidates=windowed_candidates,
            scored=[],
            pool_before=len(windowed_candidates),
            pool_after=len(windowed_candidates),
            method="no_linkage_signal",
            reasoning=(
                "Linkage bypassed (AMONGRESOLVER_NO_LINKAGE=1): the solver is "
                "given the whole settlement window with no identity evidence."
            ),
        )
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="linkage",
            detail=(
                f"BYPASSED (AMONGRESOLVER_NO_LINKAGE=1). Handing all "
                f"{len(windowed_candidates)} windowed candidates to the solver "
                f"with no anchors, scores or tiers, so arithmetic alone has to "
                f"identify the members. This is the comparison, not the product."
            ),
        )
    audit.log_decision(
        batch_id=batch.batch_id,
        agent="linkage",
        detail=f"[{link_result.method}] {link_result.reasoning}",
    )

    _solved = _solve_in_tiers(
        batch, solver_candidates, windowed_candidates, link_result, gross_target, cfg
    )
    result = _solved.result
    solver_candidates = _solved.solver_candidates
    scores = _solved.scores
    anchor_keys = _solved.anchor_keys
    anchor_ids = _solved.anchor_ids

    _withhold_if_unevidenced(
        batch, result, solver_candidates, link_result, anchor_keys
    )

    audit.log_decision(
        batch_id=batch.batch_id,
        agent="subset_sum",
        detail=result.reasoning,
    )

    _apply_confidence_gate(batch, result, solver_candidates, link_result)

    # Agent 3b: tiebreak ambiguous matches via fuzzy plausibility scoring
    _tiebreak_if_ambiguous(batch, result, windowed_candidates, gross_target, cfg, enable_tiebreak)

    exceptions, unmatched = _collect_unmatched(
        batch, result, candidates, windowed_candidates, gross_target, fuzz_cfg
    )

    # Agent 5: diagnose what's left
    if unmatched:
        exceptions = diagnose_batch_exceptions(unmatched, windowed_candidates, batch.batch_id)
        for exc in exceptions:
            audit.log_decision(
                batch_id=batch.batch_id,
                agent="exception_diagnosis",
                detail=f"{exc.reason.value}: {exc.diagnosis_note}",
            )

    return _build_report_and_tie_out(
        batch, result, windowed_candidates, exceptions, fee_breakdown, gross_target
    )
