"""
The reconciliation pipeline for one settlement, and the N:M variant.

Order: fee decomposition (gross target), currency and status filters, the
settlement window, linkage, exact subset-sum in evidence tiers, the refusal
gates (recon_gates.py), tiebreak for ambiguous sets, fuzzy recovery when
nothing sums, exception diagnosis, report and tie-out. Every decision is
written to the audit trail. Nothing here writes to a ledger.
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
    _anchored_negatives,
    UNPROVEN_UNIQUE_CONFIDENCE,
    UNPROVEN_UNIQUE_NOTE,
)
from subset_sum_nm import build_union_pool, exact_subset_sum_nm, probe_for_alternate_nm_assignment
from fuzzy_match import (
    match_batch_fuzzy, FuzzyMatchConfig,
    score_subset_plausibility, build_similarity_context, bulk_fuzzy_recover,
)
import compliance_agent
from exception_diagnosis import diagnose_batch_exceptions
from linkage import (
    LinkageResult, build_candidate_links, link_confidence, txn_key, members_of,
)
import audit
from fee_audit import run_fee_audit, MethodRateCard, FeeAuditFinding
from india_tax import ist_date
import calibration_map

# The refusal gates moved to recon_gates.py — one concern, and this file
# was not it. Re-exported because callers and tests reach for them here.
from recon_gates import (  # noqa: E402,F401
    MIN_AUTOCLEAR_CONFIDENCE, FUZZY_RECOVERY_CONFIDENCE,
    UNANCHORED_AUTOCLEAR_LIMIT,
    _withhold_if_unevidenced, _apply_confidence_gate,
    _withhold_cross_batch_double_claims,
)


@dataclass
class ReconciliationReport:
    batch_id: str
    total_candidates: int
    match_result: MatchResult
    exceptions: list[ExceptionRecord] = field(default_factory=list)
    fee_audit_findings: list[FeeAuditFinding] = field(default_factory=list)
    fee_audit_summary: dict | None = None
    # What the Fellegi-Sunter model learned for this settlement, if it fitted.
    learned_linkage: dict | None = None

    false_positive_cost_estimate_cents: int = 0
    """
    Estimated cost if the matched subset is incorrect (a false positive).
    Computed as: matched_sum_cents * false_positive_rate (conservative 5%
    for ambiguous matches, 0 for high-confidence exact matches).
    It is the financial materiality of getting the match wrong.
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
            # What claims at this confidence have actually been worth
            # (calibration_map.py). Shown beside the raw figure; the auto-clear
            # gate still reads the raw one, which is the figure with a record.
            "calibrated_confidence": calibration_map.calibrated(self.match_result.confidence),
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
            "learned_linkage": self.learned_linkage,
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

# Deliberately settling: refunded, chargeback, dispute and similar describe
# money that moved and was clawed back by its own negative row; excluding the
# original too would count the reversal twice. captured, settled, success,
# deemed_success (UPI) and represented all settled.


def _count_by_reason(exceptions: list[ExceptionRecord]) -> dict:
    counts: dict[str, int] = {}
    for e in exceptions:
        counts[e.reason.value] = counts.get(e.reason.value, 0) + 1
    return counts


def _compute_false_positive_cost(match_result: MatchResult) -> int:
    """
    Estimated cost if this match is wrong: 0 for an exact, unambiguous clear;
    5% of the matched sum for an ambiguous one; 10% for fuzzy; 0 when not
    cleared.
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
    primary_keys: set[str],
    batch_id: str,
    forced_ids: set[str] | None = None,
    num_search_workers: int = 1,
    time_limit_s: float = 5.0,
) -> Optional[list[NormalizedTxn]]:
    """
    Break an ambiguous tie: find one alternate subset (forbidding the primary)
    and keep whichever scores higher on reference/memo plausibility.
    `forced_ids` are passed through so the alternate cannot simply drop an
    anchored refund; keys are txn_keys, since the pool mixes feeds.
    """
    from subset_sum import _solve_cpsat

    # find an alternate subset
    forbidden = [primary_keys]
    alt_result = _solve_cpsat(
        candidates, target_cents, tolerance_cents, time_limit_s,
        forbidden_solutions=forbidden,
        num_search_workers=num_search_workers,
        forced_ids=forced_ids,
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
    alt_keys = {txn_key(t) for t in alt_txns}

    if alt_keys == primary_keys:
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
    # Currency first: amounts are integer minor units with no unit attached, so
    # the solver once cleared INR 300 from two INR legs and a USD leg at 0.97.
    # Foreign-currency candidates are excluded until FX conversion exists.
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
    # Money that never moved is not a settlement member. Failed payments once
    # entered the pool as spendable and created impossible alternate subsets. Only
    # explicitly non-settling states are dropped; blank or unknown stays.
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
    # Exact subset-sum in tiers of decreasing evidence, first tier to CLEAR wins:
    # records naming the settlement, then the learned cohort, then strong links,
    # then everything linked. Weak signals admitted beside strong ones add
    # degeneracy, not information (solving anchors first took near-collision from
    # 0% to solved; one pooled tier took ref_partial from 100% to 13%).
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

    # Records the Fellegi-Sunter model rates likely members (linkage_em.py),
    # with the anchors. Where references are gone this is the capture-day
    # cohort the learned settlement cycle points at; where some survive it
    # is the anchors plus records that look like them but lost their id.
    learned_keys = getattr(link_result, "learned_keys", None) or set()
    learned_txns = [t for t in solver_candidates
                    if txn_key(t) in learned_keys or txn_key(t) in anchor_keys]

    tiers: list[tuple[str, list[NormalizedTxn]]] = []
    if anchor_txns:
        tiers.append(("anchor", anchor_txns))
    if len(anchor_txns) < len(learned_txns) < len(solver_candidates):
        tiers.append(("learned", learned_txns))
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

        # Substitutability guard: a tier can hide one copy of a payment that exists
        # in another feed with the same amount, so a solve looks unique when it is
        # not. This produced the first false clears ever recorded ("_MIRROR" records),
        # so a member with an equal-amount twin in another feed outside the tier
        # withholds the clear.
        if tier_result.cleared and tier_result.matched_txn_ids:
            # Keys, not bare ids — a bare-id collision across feeds could
            # make an `o` that IS in this tier look like it isn't (or vice
            # versa) below. Same class of bug as link_confidence's, see
            # linkage.txn_key.
            tier_ids = {txn_key(t) for t in tier_txns}
            matched_txns = members_of(tier_result, tier_txns)

            # Exempt: anchored members (they name the settlement) and members of the
            # declared member feed (the other copy was never a candidate). Scoring the
            # two copies against each other was tried and endorsed the wrong one (30
            # false clears).
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
                    and txn_key(o) not in tier_ids
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
                fallback_matched_keys = {txn_key(t) for t in
                                         members_of(fallback, windowed_candidates)}
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
        # Exact, unambiguous clear. The rest of the window belongs to other
        # settlements and is NOT an exception: routing it produced 20,078 exceptions
        # on a perfect 5-transaction match and demanded review of a certain answer.
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
        member_keys = {txn_key(t) for t in members_of(result, windowed_candidates)}
        unmatched = [t for t in windowed_candidates if txn_key(t) not in member_keys]
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
            result.matched_keys = [txn_key(t) for t in windowed_candidates
                                   if t.source_txn_id in recovered_set]
            result.matched_sum_cents = sum(
                t.amount_cents for t in windowed_candidates
                if t.source_txn_id in recovered_set
            )
            result.method = MatchMethod.FUZZY_SEMANTIC
            result.confidence = FUZZY_RECOVERY_CONFIDENCE
            # Append to the reasoning rather than replace it: the earlier text says why
            # subset-sum found nothing, and a fuzzy set need not sum to the target.
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
                f"on its own. Measured over 167 such bundles: the exact set is "
                f"never right, about 4% of what it contains belongs to the "
                f"settlement, and the true members are somewhere inside it "
                f"roughly 40% of the time. Read it as a shortlist to search, "
                f"not as a proposed answer — which is why it is reported at "
                f"{FUZZY_RECOVERY_CONFIDENCE:.2f} rather than at the "
                f"similarity threshold that selected it."
            )
            audit.log_decision(
                batch_id=batch.batch_id,
                agent="fuzzy_fallback",
                detail=f"Recovered {len(fuzzy_recovered)} txns via fuzzy pass. "
                       f"{len(unmatched)} still unresolved.",
            )

    return exceptions, unmatched


def _tiebreak_if_ambiguous(
    batch: SettlementBatch,
    result: MatchResult,
    windowed_candidates: list[NormalizedTxn],
    gross_target: int,
    cfg: SubsetSumConfig,
    enable_tiebreak: bool,
    learned_keys: set | None = None,
) -> None:
    """Agent 3b: tiebreak ambiguous matches via fuzzy plausibility scoring."""
    if result.ambiguous and enable_tiebreak and result.matched_txn_ids:
        matched_pool = members_of(result, windowed_candidates)
        # A proposal drawn entirely from the learned cohort is withheld because
        # its confidence is below the gate — "a person should confirm this" —
        # not because a better set exists. The tiebreak scores reference and
        # memo similarity, which is noise in a pool whose references are gone,
        # and it once swapped an exact, cohort-backed 15-payment set for a
        # wrong 20-payment one on exactly such a pool. Evidence beats a
        # preference between equal sums, so the evidenced proposal stands.
        if learned_keys and {txn_key(t) for t in matched_pool} <= learned_keys:
            return
        # txn_key, not the bare matched_txn_ids: candidates here is fed by
        # _solve_cpsat, which compares by txn_key (see its docstring), and the
        # chosen set below is compared in the same form.
        primary_keys = {txn_key(t) for t in matched_pool}
        # Recomputed rather than threaded through MatchResult: it's a cheap,
        # pure filter over windowed_candidates (same inputs match_batch used
        # internally), and re-deriving it here is far less invasive than
        # widening the MatchResult schema to carry it.
        forced_ids = _anchored_negatives(batch.batch_id, windowed_candidates)
        # A declared member feed holds for the tiebreak too. Linkage scoped the
        # solve to it; this probe searched every feed and so could "prefer" a
        # set of ledger copies — on the demo sample, 11 ERP records of which 7
        # doubled a gateway payment already in the set. Withheld either way,
        # but a reviewer was shown a set that could never be right.
        # FAILURE_LOG 31.
        pool = ([t for t in windowed_candidates if t.source is batch.member_source]
                if batch.member_source is not None else windowed_candidates)
        chosen = _tiebreak_ambiguous_match(
            matched_pool, pool, gross_target,
            cfg.tolerance_cents, primary_keys, batch.batch_id,
            forced_ids=forced_ids,
            num_search_workers=cfg.num_search_workers,
            time_limit_s=cfg.probe_time_limit_s,
        )
        if chosen is not None:
            chosen_keys = {txn_key(t) for t in chosen}
            if chosen_keys != primary_keys:
                result.matched_txn_ids = [t.source_txn_id for t in chosen]
                result.matched_keys = sorted(chosen_keys)
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
    exceptions = exceptions or []
    fee_findings = []
    fee_summary = None

    # By txn_key: a bare id can also name another feed's record, which put
    # that record's amount into the tie-out and its fees into the audit.
    matched_txns = members_of(result, windowed_candidates) if result else []
    if matched_txns:
        fee_findings, fee_summary = run_fee_audit(
            matched_txns,
            batch_deduction_cents=batch.declared_deductions_cents,
            rate_card=MethodRateCard(),
            # A row with no timestamp is taxed under the law on the payout's day.
            as_of=ist_date(batch.settled_at_utc),
        )

    matched_gross = sum(t.amount_cents for t in matched_txns)
    report = ReconciliationReport(
        batch_id=batch.batch_id,
        total_candidates=len(windowed_candidates),
        match_result=result,
        exceptions=exceptions,
        fee_audit_findings=fee_findings,
        fee_audit_summary=fee_summary,
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


@dataclass
class _NMBatchPrep:
    """One batch's own pre-solve state within a joint N:M group — the exact
    per-batch pipeline reconcile_batch runs before it ever calls the solver,
    computed once here and reused by both the joint solve and, if needed,
    the independent per-batch fallback below."""
    batch: SettlementBatch
    fee_breakdown: object
    gross_target: int
    batch_candidates: list[NormalizedTxn]   # currency/status filtered, NOT windowed
    windowed: list[NormalizedTxn]
    link_result: LinkageResult
    narrowed: list[NormalizedTxn]
    forced_keys: set[str]


def reconcile_many(
    batches: list[SettlementBatch],
    candidates: list[NormalizedTxn],
    settlement_window_days: int = 5,
    subset_config: SubsetSumConfig | None = None,
    rate_card: FeeRateCard = DEFAULT_RATE_CARD,
) -> list[ReconciliationReport]:
    """
    N:M: several settlements solved at once against one shared pool, so a
    payment two settlements could claim is assigned by the solver, not by order.

    Reused per batch from the 1:N path: linkage narrowing, forced anchored
    refunds, a per-target ambiguity probe, and the same refusal gates. Not
    reused: the evidence tiers and the substitutability guard, which have not
    been generalised to several targets. If the joint model is infeasible (one
    batch's leg missing makes the whole model infeasible), every batch falls
    back to reconcile_batch against the original pool, with siblings' anchored
    records withheld, and double claims across the group are then withheld.
    The joint solve prevents double claims by construction (AddAtMostOne).
    """
    if not batches:
        return []

    cfg = subset_config or SubsetSumConfig()

    safe_candidates, _ = compliance_agent.scan(candidates)
    audit.log_decision(
        batch_id="+".join(b.batch_id for b in batches),
        agent="orchestrator",
        detail=(
            f"Joint N:M solve requested for {len(batches)} batch(es) sharing "
            f"one pool of {len(candidates)} candidate(s) "
            f"({len(safe_candidates)} after compliance screening)."
        ),
    )

    prep: list[_NMBatchPrep] = []
    for batch in batches:
        fee_breakdown = compute_fee_breakdown(batch, rate_card, safe_candidates)
        gross_target = fee_breakdown.gross_target_cents(batch.net_amount_cents)

        batch_candidates = _filter_to_settlement_currency(batch, safe_candidates)
        batch_candidates = _filter_out_non_settling(batch, batch_candidates)
        windowed = filter_candidates_by_settlement_window(
            batch_candidates, batch.settled_at_utc, settlement_window_days
        )
        link_result = build_candidate_links(batch, windowed, settlement_window_days)
        narrowed = link_result.candidates
        forced_keys = _anchored_negatives(batch.batch_id, narrowed)

        audit.log_decision(
            batch_id=batch.batch_id, agent="linkage",
            detail=f"[joint] [{link_result.method}] {link_result.reasoning}",
        )

        prep.append(_NMBatchPrep(
            batch=batch, fee_breakdown=fee_breakdown, gross_target=gross_target,
            batch_candidates=batch_candidates, windowed=windowed,
            link_result=link_result, narrowed=narrowed, forced_keys=forced_keys,
        ))

    # Anchor evidence is passed in so a candidate that NAMES one settlement
    # cannot be assigned to a different one. Contention between equal-valued
    # legs is invisible to arithmetic, so without this the solver resolved it
    # by whichever assignment it reached first. See build_union_pool.
    union, eligible = build_union_pool(
        [p.narrowed for p in prep],
        [p.link_result.anchor_keys for p in prep],
    )
    target_cents_list = [p.gross_target for p in prep]
    forced_per_target = [p.forced_keys for p in prep]

    joint = exact_subset_sum_nm(
        union, eligible, target_cents_list,
        tolerance_cents=cfg.tolerance_cents,
        time_limit_s=cfg.solver_time_limit_s,
        num_search_workers=cfg.num_search_workers,
        forced_per_target=forced_per_target,
    ) if union else None

    reports: list[ReconciliationReport] = []

    if joint is not None:
        nm_probe: dict = {}
        ambiguous_flags = probe_for_alternate_nm_assignment(
            union, eligible, joint, target_cents_list,
            cfg.tolerance_cents, cfg.probe_time_limit_s, cfg.ambiguity_probe_limit,
            num_search_workers=cfg.num_search_workers,
            forced_per_target=forced_per_target,
            outcome=nm_probe,
        )
        probe_timed_out = bool(nm_probe.get("timed_out"))
        for i, p in enumerate(prep):
            matched_txns = joint.matched[i]
            achieved_sum = joint.achieved_sums[i]
            ambiguous = ambiguous_flags[i]
            diff = abs(achieved_sum - p.gross_target)

            result = MatchResult(
                batch_id=p.batch.batch_id,
                matched_txn_ids=[t.source_txn_id for t in matched_txns],
                matched_keys=[txn_key(t) for t in matched_txns],
                method=MatchMethod.EXACT_SUBSET_SUM,
                # Same 0.36/1.0 split as subset_sum.match_batch's arithmetic
                # confidence, and the same meaning: 1.0 is not a guess, the
                # probe found no alternate joint assignment where THIS
                # target's set differed, within the probed budget. It is
                # not independently calibrated for the joint case — no N:M
                # benchmark exists yet to measure it against, unlike the
                # 1:N bands above, so this borrows the 1:N number honestly
                # rather than inventing an untested one of its own.
                confidence=(0.36 if ambiguous else
                            UNPROVEN_UNIQUE_CONFIDENCE if probe_timed_out else 1.0),
                matched_sum_cents=achieved_sum,
                target_cents=p.gross_target,
                cleared=not ambiguous,
                ambiguous=ambiguous,
                withheld_reason="alternate_assignment" if ambiguous else None,
                reasoning=(
                    f"Joint N:M subset-sum match (CP-SAT, {len(batches)} "
                    f"batch(es) solved together): {len(matched_txns)} "
                    f"transactions sum to {achieved_sum} cents (target "
                    f"{p.gross_target} cents, diff {diff} cents)."
                    + (
                        " WARNING (bounded probe, not exhaustive): this "
                        "settlement's own matched set varied across at "
                        "least one alternate joint assignment found within "
                        "the probe budget — not uniquely determined by "
                        "arithmetic alone within the probed alternatives."
                        if ambiguous else ""
                    )
                ),
            )

            if probe_timed_out and not ambiguous:
                result.reasoning += UNPROVEN_UNIQUE_NOTE

            _withhold_if_unevidenced(
                p.batch, result, p.narrowed, p.link_result, p.link_result.anchor_keys
            )
            audit.log_decision(
                batch_id=p.batch.batch_id, agent="subset_sum_nm", detail=result.reasoning
            )
            _apply_confidence_gate(p.batch, result, p.narrowed, p.link_result)

            exceptions, unmatched = _collect_unmatched(
                p.batch, result, p.batch_candidates, p.windowed, p.gross_target,
                FuzzyMatchConfig(),
            )
            if unmatched:
                exceptions = diagnose_batch_exceptions(unmatched, p.windowed, p.batch.batch_id)
                for exc in exceptions:
                    audit.log_decision(
                        batch_id=p.batch.batch_id, agent="exception_diagnosis",
                        detail=f"{exc.reason.value}: {exc.diagnosis_note}",
                    )

            reports.append(_build_report_and_tie_out(
                p.batch, result, p.windowed, exceptions, p.fee_breakdown, p.gross_target
            ))
    else:
        audit.log_decision(
            batch_id="+".join(b.batch_id for b in batches), agent="orchestrator",
            detail=(
                "Joint CP-SAT solve found no assignment satisfying every "
                f"target across this group of {len(batches)} simultaneously "
                "(or linkage left no candidate eligible for any of them). "
                "Falling back to reconciling each batch independently "
                "through the full 1:N pipeline against the shared pool."
            ),
        )
        # Anchor evidence binds in the fallback too: a batch alone cannot see that a
        # record names its sibling, and once took a sibling's equal-valued leg at
        # 0.91. Records anchored to another settlement in the group are withheld
        # from this batch's pool.
        for i, p in enumerate(prep):
            foreign_anchors: set[str] = set()
            for j, sibling in enumerate(prep):
                if j != i:
                    foreign_anchors |= sibling.link_result.anchor_keys
            foreign_anchors -= p.link_result.anchor_keys

            batch_pool = (
                [t for t in safe_candidates if txn_key(t) not in foreign_anchors]
                if foreign_anchors else safe_candidates
            )
            if foreign_anchors:
                audit.log_decision(
                    batch_id=p.batch.batch_id, agent="linkage",
                    detail=(
                        f"[fallback] {len(foreign_anchors)} candidate(s) "
                        f"reference a different settlement in this group and "
                        f"were withheld from this batch's pool."
                    ),
                )
            reports.append(reconcile_batch(
                p.batch, batch_pool, subset_config=cfg,
                settlement_window_days=settlement_window_days, rate_card=rate_card,
            ))
        _withhold_cross_batch_double_claims(reports)

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
        # Discard the EVIDENCE, not just the narrowing: keeping anchors and tiers made
        # this flag measure 62.0%, identical to linkage on. Arithmetic alone means one
        # tier and nothing for the guards to weigh.
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
    if link_result.em:
        em = link_result.em
        top = (em.get("strongest_evidence") or [{}])[0]
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="linkage",
            detail=(
                f"Learned linkage (Fellegi-Sunter, {'; '.join(em.get('notes') or [])}): "
                f"cohort of {em.get('cohort_size', 0)} candidate(s) for about "
                f"{em.get('expected_members')} expected member(s). Strongest evidence: "
                f"{top.get('comparison')}={top.get('level')} at "
                f"{top.get('weight_bits')} bits (m {top.get('m')}, u {top.get('u')})."
            ),
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
    _tiebreak_if_ambiguous(batch, result, windowed_candidates, gross_target, cfg,
                           enable_tiebreak, learned_keys=link_result.learned_keys)

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

    report = _build_report_and_tie_out(
        batch, result, windowed_candidates, exceptions, fee_breakdown, gross_target
    )
    report.learned_linkage = link_result.em
    return report
