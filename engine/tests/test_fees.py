"""
Fee decomposition tests — Agent 2.

Key regression tests:
  - round() not int(): 1-cent truncation bug that caused systematic drift
  - net -> gross reconstruction is accurate within tolerance
  - zero flat fee path works
  - gross_target_cents() correctness
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from datetime import datetime, timezone
from schema import SettlementBatch, SourceType
from fee_decomposition import compute_fee_breakdown, FeeRateCard, DEFAULT_RATE_CARD


def make_batch(batch_id: str, net_cents: int, settled: str = "2026-08-22T00:00:00+00:00") -> SettlementBatch:
    return SettlementBatch(
        batch_id=batch_id,
        net_amount_cents=net_cents,
        currency="INR",
        settled_at_utc=datetime.fromisoformat(settled),
        source=SourceType.BANK,
    )


class TestFeeBreakdown:

    def test_default_rate_card_reconstructs_gross(self):
        """
        The core invariant: net + deductions == gross_target_cents.
        Use a known gross, deduct fees, then verify we get back to gross.
        """
        gross = 100_000  # Rs 1000.00 — known starting value
        # default card: 2% gw + 1% tax = 3% total deduction from gross
        net = round(gross * 0.97)

        batch = make_batch("FEE_TEST_1", net)
        breakdown = compute_fee_breakdown(batch, DEFAULT_RATE_CARD)
        reconstructed_gross = breakdown.gross_target_cents(net)

        # Should reconstruct within 2 cents of the true gross
        # (inevitable rounding at integer-cent level)
        assert abs(reconstructed_gross - gross) <= 2, (
            f"Gross reconstruction off by {abs(reconstructed_gross - gross)} cents. "
            f"Expected ~{gross}, got {reconstructed_gross}"
        )

    def test_round_not_int_no_systematic_drift(self):
        """
        Regression: int() truncates toward zero, creating a systematic
        -1 cent bias. round() centers the estimate. Run 100 batches at
        different amounts and confirm no systematic underestimate.
        """
        errors = []
        for gross in range(50_000, 150_000, 1_000):
            net = round(gross * 0.97)
            batch = make_batch("DRIFT", net)
            breakdown = compute_fee_breakdown(batch, DEFAULT_RATE_CARD)
            reconstructed = breakdown.gross_target_cents(net)
            errors.append(reconstructed - gross)

        # No systematic bias — mean error should be near 0
        mean_error = sum(errors) / len(errors)
        assert abs(mean_error) < 0.5, (
            f"Systematic drift detected: mean error = {mean_error:.3f} cents. "
            "This indicates int() truncation bias, not round()."
        )

    def test_zero_flat_fee(self):
        """Flat fee = 0 (default) should be handled correctly."""
        batch = make_batch("FEE_FLAT0", 97_000)
        card = FeeRateCard(gateway_fee_bps=200, flat_fee_cents=0, tax_withholding_bps=100)
        breakdown = compute_fee_breakdown(batch, card)
        assert breakdown.flat_fee_cents == 0
        assert breakdown.gateway_fee_cents >= 0
        assert breakdown.tax_withholding_cents >= 0

    def test_total_deductions_are_positive(self):
        """Deductions should always be non-negative."""
        batch = make_batch("FEE_POS", 50_000)
        breakdown = compute_fee_breakdown(batch, DEFAULT_RATE_CARD)
        assert breakdown.total_deductions_cents >= 0
        assert breakdown.gateway_fee_cents >= 0
        assert breakdown.tax_withholding_cents >= 0

    def test_gross_target_greater_than_net(self):
        """With any fee rate > 0, gross must be strictly greater than net."""
        batch = make_batch("FEE_GT", 97_000)
        breakdown = compute_fee_breakdown(batch, DEFAULT_RATE_CARD)
        gross = breakdown.gross_target_cents(batch.net_amount_cents)
        assert gross > batch.net_amount_cents

    def test_custom_rate_card(self):
        """Custom fee rates should produce proportionally different deductions."""
        batch = make_batch("FEE_CUSTOM", 95_000)
        card_low = FeeRateCard(gateway_fee_bps=100, flat_fee_cents=0, tax_withholding_bps=0)
        card_high = FeeRateCard(gateway_fee_bps=500, flat_fee_cents=0, tax_withholding_bps=0)

        bd_low = compute_fee_breakdown(batch, card_low)
        bd_high = compute_fee_breakdown(batch, card_high)

        assert bd_high.gateway_fee_cents > bd_low.gateway_fee_cents

    def test_fee_breakdown_decomposition_sum(self):
        """gateway_fee + flat_fee + tax_withholding == total_deductions_cents."""
        batch = make_batch("FEE_SUM", 63_000)
        breakdown = compute_fee_breakdown(batch, DEFAULT_RATE_CARD)
        assert (
            breakdown.gateway_fee_cents
            + breakdown.flat_fee_cents
            + breakdown.tax_withholding_cents
        ) == breakdown.total_deductions_cents
