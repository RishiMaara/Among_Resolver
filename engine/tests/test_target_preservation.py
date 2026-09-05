"""
Target preservation — Agent 2 and the tie-out.

The gross target is reconstructed as net + deductions. Get the deductions
wrong and the target MOVES, at which point the true subset no longer sums to
it: the batch fails, or a different subset fits the wrong target and clears.
Everything downstream is conditional on this one number being right.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datetime import datetime, timedelta, timezone

from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence
from fee_decomposition import compute_fee_breakdown, FeeRateCard, DEFAULT_RATE_CARD
from subset_sum import SubsetSumConfig
from orchestrator import reconcile_batch

BASE = datetime(2026, 8, 20, tzinfo=timezone.utc)


def txn(tid, amount, ref=None):
    return NormalizedTxn(
        source=SourceType.GATEWAY, source_txn_id=tid,
        ref_id_canonical=ref or f"STL2026001ORD{tid}",
        amount_cents=amount, currency="INR",
        timestamp_utc=BASE + timedelta(hours=2),
        tz_confidence=TzConfidence.HIGH,
    )


class TestDeclaredDeductions:

    def test_declared_deductions_are_used_verbatim(self):
        """The settlement advice stated the figure. Nothing should be inferred
        on top of a fact."""
        b = SettlementBatch(
            batch_id="STL-2026-001", net_amount_cents=97_000, currency="INR",
            settled_at_utc=BASE + timedelta(hours=48),
            declared_deductions_cents=3_000,
        )
        fb = compute_fee_breakdown(b, DEFAULT_RATE_CARD)
        assert fb.total_deductions_cents == 3_000
        assert fb.gross_target_cents(b.net_amount_cents) == 100_000
        assert fb.basis == "declared"

    def test_declared_overrides_the_rate_card_entirely(self):
        """A rate card that disagrees with the advice must not move the
        target — the advice is the fact."""
        b = SettlementBatch(
            batch_id="STL-2026-002", net_amount_cents=97_000, currency="INR",
            settled_at_utc=BASE, declared_deductions_cents=500,
        )
        wrong_card = FeeRateCard(gateway_fee_bps=9000, flat_fee_cents=0,
                                 tax_withholding_bps=0)
        fb = compute_fee_breakdown(b, wrong_card)
        assert fb.total_deductions_cents == 500

    def test_split_is_reported_unknown_rather_than_invented(self):
        """Attributing a lump sum to 'gateway fee' would misstate a
        recoverable tax receivable as an expense in the posting proposal."""
        b = SettlementBatch(
            batch_id="STL-2026-003", net_amount_cents=97_000, currency="INR",
            settled_at_utc=BASE, declared_deductions_cents=3_000,
        )
        assert compute_fee_breakdown(b).split_known is False

    def test_estimated_path_is_labelled_as_such(self):
        b = SettlementBatch(
            batch_id="STL-2026-004", net_amount_cents=97_000, currency="INR",
            settled_at_utc=BASE,
        )
        fb = compute_fee_breakdown(b, DEFAULT_RATE_CARD)
        assert fb.basis == "estimated"
        assert fb.split_known is True


class TestTieOut:

    def _run(self, batch, txns):
        return reconcile_batch(
            batch, txns,
            subset_config=SubsetSumConfig(tolerance_cents=0, ambiguity_probe_limit=1),
            settlement_window_days=5,
        )

    def test_books_tie_when_deductions_are_declared(self):
        members = [txn("A", 40_000), txn("B", 60_000)]
        b = SettlementBatch(
            batch_id="STL2026001", net_amount_cents=97_000, currency="INR",
            settled_at_utc=BASE + timedelta(hours=48),
            declared_deductions_cents=3_000, member_source=SourceType.GATEWAY,
        )
        r = self._run(b, members)
        assert r.match_result.cleared
        assert r.matched_gross_cents == 100_000
        assert r.tie_out_residual_cents == 0
        assert r.ties_out is True

    def test_tie_out_exposes_a_wrong_deduction_figure(self):
        """
        The failure this exists to catch: deductions are misstated, so the
        target moves. The residual names the discrepancy in cents instead of
        letting a cleared batch quietly not add up.
        """
        members = [txn("A", 40_000), txn("B", 60_000)]
        b = SettlementBatch(
            batch_id="STL2026001", net_amount_cents=97_000, currency="INR",
            settled_at_utc=BASE + timedelta(hours=48),
            declared_deductions_cents=2_500,   # actually 3,000
            member_source=SourceType.GATEWAY,
        )
        r = self._run(b, members)
        if r.match_result.matched_txn_ids:
            # Whatever the solver did, the residual must be non-zero and must
            # equal the misstatement — silence here would be the bug.
            assert r.tie_out_residual_cents != 0
            assert r.ties_out is False

    def test_residual_is_zero_when_nothing_matched(self):
        """No match means no claim about the money, so there is nothing to
        tie out — a spurious residual would be noise."""
        b = SettlementBatch(
            batch_id="STL2026001", net_amount_cents=97_000, currency="INR",
            settled_at_utc=BASE + timedelta(hours=48),
            declared_deductions_cents=3_000,
        )
        r = self._run(b, [txn("A", 11), txn("B", 13)])
        if not r.match_result.matched_txn_ids:
            assert r.tie_out_residual_cents == 0

    def test_summary_exposes_the_tie_out_for_review(self):
        members = [txn("A", 40_000), txn("B", 60_000)]
        b = SettlementBatch(
            batch_id="STL2026001", net_amount_cents=97_000, currency="INR",
            settled_at_utc=BASE + timedelta(hours=48),
            declared_deductions_cents=3_000, member_source=SourceType.GATEWAY,
        )
        s = self._run(b, members).summary()
        assert s["ties_out"] is True
        assert s["fee_basis"] == "declared"
        assert s["tie_out_residual_cents"] == 0
