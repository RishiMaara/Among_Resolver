"""
What one reconciliation reports: the verdict, the tie-out arithmetic that
proves the money adds up (matched gross - deductions - net = 0), the fee
audit on the matched set, and the estimated cost if the match is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from schema import NormalizedTxn, SettlementBatch, MatchResult, MatchMethod, ExceptionRecord
from linkage import members_of
import audit
from fee_audit import run_fee_audit, MethodRateCard, FeeAuditFinding
from india_tax import ist_date
import calibration_map


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
            "unreferenced_members": self.match_result.unreferenced_txn_ids,
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
    fee_findings: list[FeeAuditFinding] = []
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
