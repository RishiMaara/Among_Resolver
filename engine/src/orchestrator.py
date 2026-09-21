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
    _anchored_negatives,
)
from subset_sum_nm import build_union_pool, exact_subset_sum_nm, probe_for_alternate_nm_assignment
from fuzzy_match import (
    match_batch_fuzzy, FuzzyMatchConfig,
    score_subset_plausibility, build_similarity_context, bulk_fuzzy_recover,
)
import compliance_agent
from exception_diagnosis import diagnose_batch_exceptions
from linkage import LinkageResult, build_candidate_links, link_confidence, txn_key
import audit
from fee_audit import run_fee_audit, MethodRateCard, FeeAuditFinding
from india_tax import ist_date





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
    - Ambiguous exact match (confidence=0.36, re-measured — see subset_sum.py's
      match_batch for why: calibration measured the 0.65 band right only
      54.5% of the time and the value was moved to the observed rate): 5%
      of matched sum (plausible restatement error if wrong subset chosen)
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
    primary_keys: set[str],
    batch_id: str,
    forced_ids: set[str] | None = None,
    num_search_workers: int = 1,
    time_limit_s: float = 5.0,
) -> Optional[list[NormalizedTxn]]:
    """
    When CP-SAT is ambiguous (multiple valid subsets), use ref_id/memo
    plausibility scoring to pick the more coherent subset.

    Strategy: find one alternate subset via CP-SAT with forbidding constraint,
    then score both primary and alternate against the full pool's ref_id/memo
    signal — higher plausibility score wins.

    `forced_ids` must be threaded through to this solve for the same reason
    `_probe_for_alternate_subset` in subset_sum.py forces them: without it,
    this probe happily "finds" an alternate that simply drops an anchored
    refund — a set that is arithmetically valid and factually impossible —
    and if fuzzy plausibility scores that alternate higher, the tiebreak
    would CHOOSE it. That comment already existed next to the fix in
    subset_sum.py; this call site was the twin it wasn't applied to.
    `num_search_workers`/`time_limit_s` default to the same values as
    before (1 worker, 5s) when not supplied, so this is not a behavior
    change for any caller that doesn't pass them.

    `primary_keys` (and `forced_ids`) are txn_key values, not bare
    source_txn_id — `candidates` here is a settlement's own windowed pool,
    which routinely merges several feeds, so a bare id is not a safe
    identity. See subset_sum._solve_cpsat's docstring.
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
            # Keys, not bare ids — a bare-id collision across feeds could
            # make an `o` that IS in this tier look like it isn't (or vice
            # versa) below. Same class of bug as link_confidence's, see
            # linkage.txn_key.
            tier_ids = {txn_key(t) for t in tier_txns}
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
            result.confidence = FUZZY_RECOVERY_CONFIDENCE
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
    enable_tiebreak: bool
) -> None:
    """Agent 3b: tiebreak ambiguous matches via fuzzy plausibility scoring."""
    if result.ambiguous and enable_tiebreak and result.matched_txn_ids:
        matched_pool = [t for t in windowed_candidates if t.source_txn_id in set(result.matched_txn_ids)]
        primary_ids = set(result.matched_txn_ids)
        # txn_key, not the bare matched_txn_ids: candidates here is fed by
        # _solve_cpsat, which now compares by txn_key (see its docstring).
        # primary_ids stays around too — chosen_ids below is compared against
        # it in bare-id form, since result.matched_txn_ids is bare-id by
        # schema (see MatchResult) and that comparison never reaches the solver.
        primary_keys = {txn_key(t) for t in matched_pool}
        # Recomputed rather than threaded through MatchResult: it's a cheap,
        # pure filter over windowed_candidates (same inputs match_batch used
        # internally), and re-deriving it here is far less invasive than
        # widening the MatchResult schema to carry it.
        forced_ids = _anchored_negatives(batch.batch_id, windowed_candidates)
        chosen = _tiebreak_ambiguous_match(
            matched_pool, windowed_candidates, gross_target,
            cfg.tolerance_cents, primary_keys, batch.batch_id,
            forced_ids=forced_ids,
            num_search_workers=cfg.num_search_workers,
            time_limit_s=cfg.probe_time_limit_s,
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
    exceptions = exceptions or []
    fee_findings = []
    fee_summary = None

    if result and result.matched_txn_ids:
        # Resolve matched IDs back to objects for the audit
        matched_txns = [t for t in windowed_candidates if t.source_txn_id in set(result.matched_txn_ids)]
        fee_findings, fee_summary = run_fee_audit(
            matched_txns,
            batch_deduction_cents=batch.declared_deductions_cents,
            rate_card=MethodRateCard(),
            # A row with no timestamp is taxed under the law on the payout's day.
            as_of=ist_date(batch.settled_at_utc),
        )

    matched_gross = sum(
        t.amount_cents for t in windowed_candidates
        if t.source_txn_id in set(result.matched_txn_ids)
    )
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
    N:M pathway — several settlement batches solved SIMULTANEOUSLY against
    one shared candidate pool, so a transaction that could plausibly belong
    to more than one settlement is assigned by the solver rather than by
    whichever batch happens to be processed first.

    This is not reconcile_batch called in a loop, and it is not
    /reconcile/queue with a different name. The queue's batches are
    reconciled one after another, and a payment claimed by the first is
    simply unavailable to the second (settled_ledger records the claim) —
    correct, and order-dependent: a batch processed later can lose a
    transaction to an earlier one that could ALSO have been satisfied a
    different way, and the outcome depends on queue order rather than on
    the evidence. The joint CP-SAT solve below removes the ordering
    dependency: every batch's assignment is decided at once, with a
    transaction eligible for more than one target resolved by the solver
    rather than by processing order. See subset_sum_nm.py's module
    docstring for the solver itself.

    WHAT THIS REUSES FROM THE 1:N PATH, AND WHAT IT DOES NOT
    ----------------------------------------------------------
    Per batch: linkage narrows its candidates before the joint solver ever
    runs (build_candidate_links, unchanged, called once per batch against
    that batch's own window); an anchored refund is forced rather than
    optional (_anchored_negatives, unchanged); the joint assignment is
    probed for alternates and a batch whose OWN matched set varies across
    the probe is marked ambiguous
    (subset_sum_nm.probe_for_alternate_nm_assignment, the per-target
    sibling of subset_sum._probe_for_alternate_subset); and
    _withhold_if_unevidenced / _apply_confidence_gate — the exact functions
    reconcile_batch calls — decide whether each batch's result is evidenced
    and confident enough to auto-clear, reused verbatim, per batch.

    What it does NOT reuse: _solve_in_tiers' anchor/strong_link/all_linked
    tiering, and its cross-feed substitutability guard. Both are real
    refinements on top of the core safety net above, measured and tuned for
    one target at a time. Generalising substitutability correctly — is a
    member substitutable by a twin, now that the twin could belong to a
    DIFFERENT target instead of simply being excluded — is a materially
    different analysis that has not been built. Said here rather than
    shipped silently under the same name as full parity.

    THE JOINT SOLVE, AND ITS FALLBACK
    ----------------------------------
    A single infeasible target — one batch's leg genuinely missing, say —
    makes the joint CP-SAT model infeasible for every target at once; one
    CP-SAT solve has no notion of partial credit. Rather than fail the
    whole group for one batch's sake, an infeasible (or empty-pool) joint
    solve falls back to running every batch through the full, proven
    reconcile_batch independently against the ORIGINAL shared pool. That
    can never be worse than calling reconcile_batch on each batch
    separately in the first place — the same "never worse than the
    unconstrained pool" principle _solve_in_tiers already applies to one
    batch's own linkage narrowing, extended here to the whole group.

    Deliberately NOT integrated: settled_ledger's cross-run double-claim
    ledger. The joint solve already prevents double-claiming BY
    CONSTRUCTION within this one call (AddAtMostOne per candidate), which
    is the problem settled_ledger exists to catch across SEPARATE calls —
    a real gap (this call cannot see a payment /reconcile/queue claimed a
    moment ago) but a different one, left for the queue's own use of it
    rather than folded in here without being asked for.
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
        ambiguous_flags = probe_for_alternate_nm_assignment(
            union, eligible, joint, target_cents_list,
            cfg.tolerance_cents, cfg.probe_time_limit_s, cfg.ambiguity_probe_limit,
            num_search_workers=cfg.num_search_workers,
            forced_per_target=forced_per_target,
        )
        for i, p in enumerate(prep):
            matched_txns = joint.matched[i]
            achieved_sum = joint.achieved_sums[i]
            ambiguous = ambiguous_flags[i]
            diff = abs(achieved_sum - p.gross_target)

            result = MatchResult(
                batch_id=p.batch.batch_id,
                matched_txn_ids=[t.source_txn_id for t in matched_txns],
                method=MatchMethod.EXACT_SUBSET_SUM,
                # Same 0.36/1.0 split as subset_sum.match_batch's arithmetic
                # confidence, and the same meaning: 1.0 is not a guess, the
                # probe found no alternate joint assignment where THIS
                # target's set differed, within the probed budget. It is
                # not independently calibrated for the joint case — no N:M
                # benchmark exists yet to measure it against, unlike the
                # 1:N bands above, so this borrows the 1:N number honestly
                # rather than inventing an untested one of its own.
                confidence=0.36 if ambiguous else 1.0,
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
        # Anchor evidence binds in the fallback too, not only in the joint
        # solve. Each batch is reconciled alone here, so on its own it cannot
        # know that a candidate names one of its siblings — it sees an
        # unanchored record of the right size and takes it.
        #
        # Measured: a batch whose own leg had not arrived reached for a
        # sibling's equal-valued leg and reported 0.91, the "partially
        # anchored, used every anchor available" band. Every anchor available
        # TO IT was indeed used; the evidence it ignored belonged to the batch
        # next to it, which a single-batch view has no way to consult. Only
        # the double-claim guard below caught those, and only because the
        # sibling happened to claim the same record — take that coincidence
        # away and it is a false clear at 0.91.
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
