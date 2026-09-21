"""
Tests for the Indian fee and tax audit module.

Covers every detection category and both edges: amounts that should pass
cleanly and amounts that should be flagged. The rate card is always
explicit so the tests do not drift when defaults change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

import fee_audit
from fee_audit import (
    FeeAuditCategory,
    FeeAuditSeverity,
    MethodRateCard,
    audit_settlement_shortfall,
    audit_tds_194o,
    audit_transaction_fees,
    run_fee_audit,
)


@dataclass
class _Txn:
    """Minimal stand-in for NormalizedTxn in these tests."""
    source_txn_id: str
    amount_cents: int
    memo_normalized: str = ""
    extra: dict = field(default_factory=dict)


CARD = MethodRateCard(card_bps=200, tolerance_bps=10)  # 2%, 0.1% tolerance


# ── Per-transaction fee checks ────────────────────────────────────────────────

class TestTransactionFeeAudit:
    def test_upi_zero_fee_passes(self):
        txn = _Txn("UPI001", 100_000, extra={"payment_method": "upi", "fee_amount_cents": 0})
        findings = audit_transaction_fees([txn], MethodRateCard(upi_bps=0))
        assert findings == []

    def test_card_correct_fee_passes(self):
        # 2% of Rs 1000 = Rs 20 = 2000 cents
        txn = _Txn("CARD001", 100_000, extra={"payment_method": "card", "fee_amount_cents": 2000})
        findings = audit_transaction_fees([txn], CARD)
        assert findings == []

    def test_card_overcharge_detected(self):
        # 3% instead of 2% on Rs 1000 — overcharge of Rs 10
        txn = _Txn("CARD002", 100_000, extra={"payment_method": "card", "fee_amount_cents": 3000})
        findings = audit_transaction_fees([txn], CARD)
        assert len(findings) == 1
        f = findings[0]
        assert f.category == FeeAuditCategory.FEE_OVERCHARGE
        assert f.expected_cents == 2000
        assert f.actual_cents == 3000
        assert f.difference_cents == 1000

    def test_card_undercharge_detected(self):
        # 1% instead of 2% on Rs 1000 — undercharge
        txn = _Txn("CARD003", 100_000, extra={"payment_method": "card", "fee_amount_cents": 1000})
        findings = audit_transaction_fees([txn], CARD)
        assert len(findings) == 1
        assert findings[0].category == FeeAuditCategory.FEE_UNDERCHARGE

    def test_within_tolerance_passes(self):
        # 2.05% instead of 2% on Rs 1000 — within 0.1% tolerance
        txn = _Txn("CARD004", 100_000, extra={"payment_method": "card", "fee_amount_cents": 2050})
        findings = audit_transaction_fees([txn], CARD)
        assert findings == []

    def test_netbanking_flat_fee_correct(self):
        card = MethodRateCard(netbanking_flat_cents=500)
        txn = _Txn("NB001", 500_000, extra={"payment_method": "netbanking", "fee_amount_cents": 500})
        findings = audit_transaction_fees([txn], card)
        assert findings == []

    def test_netbanking_flat_fee_overcharge(self):
        card = MethodRateCard(netbanking_flat_cents=500, tolerance_bps=10)
        # Rs 20 charged instead of Rs 5 flat — well above 0.1% tolerance
        txn = _Txn("NB002", 500_000, extra={"payment_method": "netbanking", "fee_amount_cents": 2000})
        findings = audit_transaction_fees([txn], card)
        assert len(findings) == 1
        assert findings[0].category == FeeAuditCategory.FEE_OVERCHARGE

    def test_no_fee_data_skipped(self):
        txn = _Txn("SKIP001", 100_000, extra={"payment_method": "card"})
        findings = audit_transaction_fees([txn], CARD)
        assert findings == []

    def test_method_inferred_from_memo(self):
        txn = _Txn("MEMO001", 100_000, memo_normalized="upi payment ref abc",
                    extra={"fee_amount_cents": 500})
        card = MethodRateCard(upi_bps=0, tolerance_bps=10)
        findings = audit_transaction_fees([txn], card)
        assert len(findings) == 1
        assert findings[0].payment_method == "upi"


# ── GST checks ───────────────────────────────────────────────────────────────

class TestGSTAudit:
    def test_gst_correct_on_fee(self):
        # Fee: Rs 20, GST: 18% of 20 = Rs 3.60 = 360 cents
        txn = _Txn("GST001", 100_000, extra={
            "payment_method": "card",
            "fee_amount_cents": 2000,
            "gst_amount_cents": 360,
        })
        findings = audit_transaction_fees([txn], CARD)
        assert findings == []

    def test_gst_on_principal_caught(self):
        # GST wrongly calculated on principal (Rs 1000) instead of fee (Rs 20)
        # 18% of Rs 1000 = Rs 180 = 18000 cents
        txn = _Txn("GST002", 100_000, extra={
            "payment_method": "card",
            "fee_amount_cents": 2000,
            "gst_amount_cents": 18000,
        })
        findings = audit_transaction_fees([txn], CARD)
        gst_findings = [f for f in findings if f.category == FeeAuditCategory.GST_MISCALCULATION]
        assert len(gst_findings) == 1
        assert gst_findings[0].severity == FeeAuditSeverity.HIGH
        assert "principal" in gst_findings[0].description.lower()

    def test_gst_wrong_rate_caught(self):
        # GST at 12% instead of 18%: 12% of Rs 20 = Rs 2.40 = 240 cents
        txn = _Txn("GST003", 100_000, extra={
            "payment_method": "card",
            "fee_amount_cents": 2000,
            "gst_amount_cents": 240,
        })
        findings = audit_transaction_fees([txn], CARD)
        gst_findings = [f for f in findings if f.category == FeeAuditCategory.GST_MISCALCULATION]
        assert len(gst_findings) == 1
        assert gst_findings[0].severity == FeeAuditSeverity.WARNING


# ── Settlement shortfall ──────────────────────────────────────────────────────

class TestSettlementShortfall:
    def test_no_shortfall(self):
        txns = [
            _Txn("S1", 50_000, extra={"fee_amount_cents": 1000, "gst_amount_cents": 180}),
            _Txn("S2", 50_000, extra={"fee_amount_cents": 1000, "gst_amount_cents": 180}),
        ]
        # Total fees+GST = 2000+360 = 2360
        result = audit_settlement_shortfall(txns, 2360)
        assert result is None

    def test_shortfall_detected(self):
        txns = [
            _Txn("S1", 50_000, extra={"fee_amount_cents": 1000}),
            _Txn("S2", 50_000, extra={"fee_amount_cents": 1000}),
        ]
        # Batch deduction 3000 but item fees only 2000 — shortfall of 1000
        result = audit_settlement_shortfall(txns, 3000)
        assert result is not None
        assert result.category == FeeAuditCategory.SETTLEMENT_SHORTFALL
        assert result.difference_cents == 1000

    def test_no_batch_deduction_skipped(self):
        txns = [_Txn("S1", 50_000, extra={"fee_amount_cents": 1000})]
        result = audit_settlement_shortfall(txns, None)
        assert result is None

    def test_no_item_fees_skipped(self):
        txns = [_Txn("S1", 50_000, extra={})]
        result = audit_settlement_shortfall(txns, 3000)
        assert result is None


# ── TDS 194-O ─────────────────────────────────────────────────────────────────

class TestTDS194O:
    def test_below_threshold_no_finding(self):
        card = MethodRateCard(tds_rate_bps=100, tds_annual_threshold_cents=500_000_00)
        txns = [_Txn("TDS001", 100_000)]  # Rs 1000, way below Rs 5L threshold
        findings = audit_tds_194o(txns, annual_gross_cents=0, rate_card=card)
        assert findings == []

    def test_above_threshold_tds_missing(self):
        card = MethodRateCard(tds_rate_bps=100, tds_annual_threshold_cents=500_000_00)
        # Annual gross already at Rs 4,99,000 + this batch Rs 10,000 = Rs 5,09,000
        # Taxable: Rs 9,000. Expected TDS: 1% of 9000 = Rs 90 = 9000 cents
        txns = [_Txn("TDS002", 1_000_000, extra={})]  # Rs 10,000
        findings = audit_tds_194o(txns, annual_gross_cents=499_000_00, rate_card=card)
        assert len(findings) == 1
        assert findings[0].category == FeeAuditCategory.TDS_WITHHOLDING_ERROR
        assert findings[0].rule_basis == "statutory"

    def test_above_threshold_tds_present(self):
        card = MethodRateCard(tds_rate_bps=100, tds_annual_threshold_cents=500_000_00)
        # Same scenario but TDS is properly withheld
        txns = [_Txn("TDS003", 1_000_000, extra={"tds_amount_cents": 9000})]
        findings = audit_tds_194o(txns, annual_gross_cents=499_000_00, rate_card=card)
        # Within Rs 1 tolerance
        assert findings == []

    def test_tds_disabled(self):
        card = MethodRateCard(tds_rate_bps=0)
        txns = [_Txn("TDS004", 100_000_000)]  # Rs 10 lakh, way above threshold
        findings = audit_tds_194o(txns, annual_gross_cents=600_000_00, rate_card=card)
        assert findings == []


# ── Duplicate fee detection ───────────────────────────────────────────────────

class TestDuplicateFee:
    def test_duplicate_fee_detected(self):
        txns = [
            _Txn("DUP001", 100_000, extra={"payment_method": "card", "fee_amount_cents": 2000}),
            _Txn("DUP001", 100_000, extra={"payment_method": "card", "fee_amount_cents": 2000}),
        ]
        findings = audit_transaction_fees(txns, CARD)
        dup_findings = [f for f in findings if f.category == FeeAuditCategory.DUPLICATE_FEE]
        assert len(dup_findings) == 1


# ── Full audit suite ──────────────────────────────────────────────────────────

class TestRunFeeAudit:
    def test_clean_batch_no_findings(self):
        txns = [
            _Txn("C1", 100_000, extra={"payment_method": "upi", "fee_amount_cents": 0}),
            _Txn("C2", 100_000, extra={"payment_method": "card", "fee_amount_cents": 2000,
                                        "gst_amount_cents": 360}),
        ]
        findings, summary = run_fee_audit(txns, rate_card=CARD)
        assert findings == []
        assert summary["total_findings"] == 0
        assert summary["tds_compliance"] == "ok"

    def test_mixed_method_batch_with_issues(self):
        txns = [
            _Txn("M1", 100_000, extra={"payment_method": "upi", "fee_amount_cents": 500}),  # UPI should be 0
            _Txn("M2", 100_000, extra={"payment_method": "card", "fee_amount_cents": 2000}),  # correct
        ]
        card = MethodRateCard(upi_bps=0, card_bps=200, tolerance_bps=10)
        findings, summary = run_fee_audit(txns, rate_card=card)
        assert summary["total_findings"] >= 1
        assert any(f.txn_id == "M1" for f in findings)

    def test_findings_sorted_by_severity(self):
        txns = [
            _Txn("S1", 100_000, extra={"payment_method": "card", "fee_amount_cents": 5000}),  # overcharge
            _Txn("S2", 100_000, extra={"payment_method": "card", "fee_amount_cents": 2000,
                                        "gst_amount_cents": 18000}),  # GST on principal (HIGH)
        ]
        findings, _ = run_fee_audit(txns, rate_card=CARD)
        high = [f for f in findings if f.severity == FeeAuditSeverity.HIGH]
        if high and len(findings) > 1:
            # HIGH severity findings should come first
            assert findings[0].severity == FeeAuditSeverity.HIGH


class TestTheDefaultTDSRateIsCurrent:
    """
    The Finance (No. 2) Act 2024 cut 194-O TDS from 1% to 0.1% with effect
    from 1 October 2024. This module shipped with a 1% default, which
    over-expects TDS tenfold and would raise a withholding finding against a
    merchant who was charged correctly. The tests above pass 100 bps
    explicitly — configurability is their point — so only this one notices
    the default drifting back.
    """

    def test_the_default_is_ten_basis_points(self):
        # The default follows the statutory schedule; today that is 0.1%.
        assert MethodRateCard().tds_rate_bps is None
        import india_tax
        from datetime import date
        assert india_tax.in_force(india_tax.TDS_ECOMMERCE, date(2026, 9, 21)).rate_bps == 10

    def test_the_default_expects_a_tenth_of_what_one_percent_expects(self):
        txns = [_Txn("TDS-DEF", 1_000_000, extra={})]
        at_default = audit_tds_194o(txns, annual_gross_cents=499_000_00,
                                    rate_card=MethodRateCard())
        at_one_percent = audit_tds_194o(txns, annual_gross_cents=499_000_00,
                                        rate_card=MethodRateCard(tds_rate_bps=100))
        assert at_default and at_one_percent, "both rates should flag missing TDS"
        # Rs 5,09,000 gross - Rs 5,00,000 threshold = Rs 9,000 taxable.
        # At 0.1% that is Rs 9; at the superseded 1% it was Rs 90.
        assert at_default[0].expected_cents == 900
        assert at_one_percent[0].expected_cents == 9_000
        assert at_one_percent[0].expected_cents == 10 * at_default[0].expected_cents

    def test_setting_the_rate_to_zero_turns_the_check_off(self):
        txns = [_Txn("TDS-OFF", 1_000_000, extra={})]
        assert audit_tds_194o(txns, annual_gross_cents=499_000_00,
                              rate_card=MethodRateCard(tds_rate_bps=0)) == []
