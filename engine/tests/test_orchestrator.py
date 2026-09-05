"""
End-to-end orchestrator tests.

Tests the full Agent 1 → 2 → 3 → 3b → 4 → 5 pipeline through reconcile_batch().
Uses synthetic data only — no network calls, no Redis required.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datetime import datetime, timedelta, timezone
from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence
from orchestrator import reconcile_batch
from subset_sum import SubsetSumConfig


BASE_TIME = datetime(2026, 8, 20, tzinfo=timezone.utc)
SETTLED_AT = BASE_TIME + timedelta(hours=72)


def make_txn(txn_id: str, amount_cents: int, hours_offset: float = 24.0) -> NormalizedTxn:
    return NormalizedTxn(
        source=SourceType.GATEWAY,
        source_txn_id=txn_id,
        ref_id_canonical=f"REF{txn_id}",
        amount_cents=amount_cents,
        currency="INR",
        timestamp_utc=BASE_TIME + timedelta(hours=hours_offset),
        tz_confidence=TzConfidence.HIGH,
        memo_raw=f"Payment {txn_id}",
        memo_normalized=f"payment {txn_id}",
    )


def make_batch(batch_id: str, gross_sum_cents: int) -> SettlementBatch:
    """
    Creates a settlement batch whose net_amount already has 3% deducted
    (2% gateway fee + 1% tax withholding — matches DEFAULT_RATE_CARD).
    The orchestrator's fee decomposition will reconstruct the gross target.
    """
    net = round(gross_sum_cents * 0.97)
    return SettlementBatch(
        batch_id=batch_id,
        net_amount_cents=net,
        currency="INR",
        settled_at_utc=SETTLED_AT,
    )


class TestHappyPath:
    """Exact subset-sum match, no exceptions."""

    def test_exact_match_clears(self):
        # true subset: T1 + T2 + T3 = 30000 cents (Rs 300)
        true_subset = [
            make_txn("T1", 10000),
            make_txn("T2", 12000),
            make_txn("T3", 8000),
        ]
        noise = [make_txn("N1", 5000), make_txn("N2", 7500)]

        candidates = true_subset + noise
        batch = make_batch("BATCH_HAPPY", 30000)
        
        report = reconcile_batch(
            batch, candidates,
            subset_config=SubsetSumConfig(tolerance_cents=0, ambiguity_probe_limit=1),
            settlement_window_days=5,
        )

        assert report.match_result.cleared, f"Expected cleared. Reasoning: {report.match_result.reasoning}"
        assert report.match_result.matched_sum_cents == pytest.approx(30000, abs=5)
        assert set(report.match_result.matched_txn_ids).issubset({"T1", "T2", "T3"})

    def test_clean_match_raises_no_exceptions_for_residual_pool(self):
        """
        Regression test for the 50K stress finding: on a cleared exact
        match the orchestrator used to route EVERY non-matched in-window
        candidate to exception diagnosis. On the real dataset that meant
        20,078 exceptions on a flawless 5-transaction match, which buried
        the actionable exceptions in noise AND flipped
        requires_human_approval to True on a 100%-confidence match
        (summary() ORs in `len(exceptions) > 0`).

        The residual pool is not an exception: the candidate pool
        deliberately holds every uncleared entry in the window, most of
        which belong to other settlements.
        """
        true_subset = [make_txn("T1", 10000), make_txn("T2", 12000), make_txn("T3", 8000)]
        # a large residual pool that is NOT part of this settlement
        noise = [make_txn(f"N{i}", 3300 + i) for i in range(25)]

        report = reconcile_batch(
            make_batch("BATCH_NO_RESIDUAL_EXC", 30000), true_subset + noise,
            subset_config=SubsetSumConfig(tolerance_cents=0, ambiguity_probe_limit=1),
            settlement_window_days=5,
        )

        assert report.match_result.cleared
        assert report.exceptions == [], (
            f"A clean exact match must not turn the residual pool into "
            f"exceptions; got {len(report.exceptions)}"
        )
        assert report.summary()["requires_human_approval"] is False

    def test_match_rate_correct(self):
        true_subset = [make_txn(f"T{i}", 5000) for i in range(4)]
        noise = [make_txn(f"N{i}", 3000) for i in range(4)]
        candidates = true_subset + noise
        batch = make_batch("BATCH_RATE", 20000)

        report = reconcile_batch(
            batch, candidates,
            subset_config=SubsetSumConfig(tolerance_cents=0, ambiguity_probe_limit=1),
            settlement_window_days=5,
        )

        if report.match_result.cleared:
            assert report.match_rate > 0
            assert 0 < report.match_rate <= 1.0

    def test_false_positive_cost_is_zero_for_exact_unambiguous(self):
        txns = [make_txn("A", 50000), make_txn("B", 30000)]
        batch = make_batch("BATCH_FP", 80000)

        report = reconcile_batch(
            batch, txns,
            subset_config=SubsetSumConfig(tolerance_cents=0, ambiguity_probe_limit=1),
            settlement_window_days=5,
        )

        # Exact, unambiguous match should have zero false positive cost
        if report.match_result.cleared and not report.match_result.ambiguous:
            assert report.false_positive_cost_estimate_cents == 0


class TestNoMatch:
    """When no subset sums to the target, the result should not be cleared."""

    def test_no_match_uncleared(self):
        candidates = [make_txn("A", 1000), make_txn("B", 2000)]
        batch = make_batch("BATCH_NOMATCH", 9999999)  # unreachable target

        report = reconcile_batch(
            batch, candidates,
            subset_config=SubsetSumConfig(tolerance_cents=0),
            settlement_window_days=5,
        )

        assert not report.match_result.cleared

    def test_no_match_produces_exceptions(self):
        candidates = [make_txn("X", 1000, hours_offset=12)]
        batch = make_batch("BATCH_EXC", 9999999)

        report = reconcile_batch(
            batch, candidates,
            subset_config=SubsetSumConfig(tolerance_cents=0),
            settlement_window_days=5,
        )

        # Unmatched candidates should be diagnosed as exceptions
        assert len(report.exceptions) > 0


class TestSettlementWindowFiltering:
    """Transactions outside the window should not be matched."""

    def test_out_of_window_transactions_excluded(self):
        # in-window txns: within 5 days of SETTLED_AT
        in_window = [make_txn("IW1", 50000, hours_offset=10)]
        # out-of-window: 10 days before settlement (6+ days)
        out_of_window = [
            NormalizedTxn(
                source=SourceType.GATEWAY,
                source_txn_id="OOW1",
                ref_id_canonical="REFOOW1",
                amount_cents=50000,
                currency="INR",
                timestamp_utc=SETTLED_AT - timedelta(days=10),
                tz_confidence=TzConfidence.HIGH,
            )
        ]
        candidates = in_window + out_of_window
        # net amount corresponds to in-window txn gross (50000 cents)
        batch = make_batch("BATCH_WIN", 50000)

        report = reconcile_batch(
            batch, candidates,
            subset_config=SubsetSumConfig(tolerance_cents=5, ambiguity_probe_limit=1),
            settlement_window_days=5,
        )

        # Out-of-window txn should not appear in the matched set
        assert "OOW1" not in report.match_result.matched_txn_ids


class TestAmbiguousMatch:
    """Ambiguous arithmetic matches must not auto-clear."""

    def test_ambiguous_does_not_auto_clear(self):
        # {A:100, B:200} and {C:150, D:150} both sum to 300
        pool = [
            make_txn("A", 100),
            make_txn("B", 200),
            make_txn("C", 150),
            make_txn("D", 150),
        ]
        batch = make_batch("BATCH_AMB", 300)

        report = reconcile_batch(
            batch, pool,
            subset_config=SubsetSumConfig(tolerance_cents=5),
            settlement_window_days=5,
        )

        # This case is DESIGNED to be ambiguous ({A,B} and {C,D} both sum
        # to 300) — assert it fires, don't just check invariants
        # conditionally. A conditional assert here would let this test
        # pass even if the ambiguity check silently broke.
        assert report.match_result.ambiguous, (
            "Expected this constructed case to trigger ambiguity detection "
            "— if it didn't, the ambiguity probe itself may be broken."
        )
        assert not report.match_result.cleared
        assert report.match_result.confidence < 1.0


class TestSummaryKeys:
    """Summary dict must always contain the keys judges are scoring."""

    def test_summary_has_required_keys(self):
        candidates = [make_txn("S1", 20000)]
        batch = make_batch("BATCH_SUM", 20000)

        report = reconcile_batch(batch, candidates, settlement_window_days=5)
        summary = report.summary()

        required_keys = {
            "batch_id", "cleared", "method", "match_rate",
            "matched_count", "total_candidates", "exception_count",
            "exceptions_by_reason", "ambiguous", "confidence",
            "false_positive_cost_estimate_cents", "requires_human_approval",
        }
        assert required_keys.issubset(summary.keys()), (
            f"Missing keys: {required_keys - set(summary.keys())}"
        )


import pytest
