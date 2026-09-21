"""
Indian tax by date: the provision in force on the day a payment was made.

The fee audit once carried one TDS rate, 1%, and it was wrong for two years
after the law cut it to 0.1%. These tests pin the schedule to the dates the
law set, including the day the Income-tax Act 2025 replaced Section 194-O
with Section 393(1) Sl. 8(v), and the halving of GST TCS on 10 July 2024.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pytest

import india_tax
from fee_audit import (
    FeeAuditCategory,
    MethodRateCard,
    audit_tcs_section52,
    audit_tds_194o,
    audit_transaction_fees,
    fee_fields,
    run_fee_audit,
)


@dataclass
class _Txn:
    source_txn_id: str
    amount_cents: int
    timestamp_utc: datetime | None = None
    memo_normalized: str = ""
    extra: dict = field(default_factory=dict)


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


class TestTheSchedules:
    @pytest.mark.parametrize("on, bps, act", [
        (date(2021, 3, 31), 75, "1961"),
        (date(2024, 9, 30), 100, "1961"),
        (date(2024, 10, 1), 10, "1961"),
        (date(2026, 3, 31), 10, "1961"),
        (date(2026, 4, 1), 10, "2025"),
    ])
    def test_tds_rate_and_act_on_each_boundary(self, on, bps, act):
        p = india_tax.in_force(india_tax.TDS_ECOMMERCE, on)
        assert p.rate_bps == bps
        assert act in p.citation

    def test_from_april_2026_the_citation_is_393_not_194o(self):
        p = india_tax.in_force(india_tax.TDS_ECOMMERCE, date(2026, 4, 1))
        assert "393(1)" in p.citation and "Sl. 8(v)" in p.citation
        assert "194-O" not in p.citation

    def test_before_194o_existed_nothing_applies(self):
        assert india_tax.in_force(india_tax.TDS_ECOMMERCE, date(2020, 9, 30)) is None

    @pytest.mark.parametrize("on, bps", [
        (date(2024, 7, 9), 100),
        (date(2024, 7, 10), 50),
        (date(2026, 9, 21), 50),
    ])
    def test_tcs_halved_on_10_july_2024(self, on, bps):
        assert india_tax.in_force(india_tax.TCS_GST_ECOMMERCE, on).rate_bps == bps

    def test_the_day_is_the_day_in_india(self):
        # 20:00 UTC on 31 March is 01:30 on 1 April in India: the new Act.
        assert india_tax.ist_date(utc(2026, 3, 31, 20, 0)) == date(2026, 4, 1)
        assert india_tax.ist_date(utc(2026, 3, 31, 18, 0)) == date(2026, 3, 31)


class TestTdsByPaymentDate:
    NO_THRESHOLD = MethodRateCard(tds_annual_threshold_cents=0)

    def test_a_2023_payment_is_checked_at_the_rate_of_2023(self):
        txns = [_Txn("P1", 1_000_000, utc(2023, 6, 1))]
        f = audit_tds_194o(txns, rate_card=self.NO_THRESHOLD)
        assert f[0].expected_cents == 10_000, "1% of Rs 10,000 in 2023"

    def test_a_2026_payment_is_checked_at_the_rate_of_2026(self):
        txns = [_Txn("P1", 1_000_000, utc(2026, 6, 1))]
        f = audit_tds_194o(txns, rate_card=self.NO_THRESHOLD)
        assert f[0].expected_cents == 1_000, "0.1% of Rs 10,000"
        assert "393(1)" in f[0].citation

    def test_a_batch_straddling_1_april_2026_cites_both_acts(self):
        txns = [_Txn("MAR", 1_000_000, utc(2026, 3, 30, 10)),
                _Txn("APR", 2_000_000, utc(2026, 4, 2, 10))]
        f = audit_tds_194o(txns, rate_card=self.NO_THRESHOLD)
        assert "194-O" in f[0].citation and "393(1)" in f[0].citation
        assert "10,000.00" in f[0].citation and "20,000.00" in f[0].citation

    def test_threshold_zero_means_no_threshold_not_off(self):
        """
        A company seller has no Rs 5 lakh exemption. Zero used to switch the
        check off, which made that seller impossible to configure correctly.
        """
        txns = [_Txn("CO", 100_000, utc(2026, 6, 1))]
        f = audit_tds_194o(txns, rate_card=MethodRateCard(tds_annual_threshold_cents=0))
        assert f == [], "Rs 1 of TDS is inside the Rs 1 tolerance"
        big = [_Txn("CO", 50_000_000, utc(2026, 6, 1))]
        f = audit_tds_194o(big, rate_card=MethodRateCard(tds_annual_threshold_cents=0))
        assert f and f[0].expected_cents == 50_000

    def test_the_exemption_is_used_up_by_the_earliest_payments(self):
        """
        Rs 4,95,000 already this year, then Rs 10,000 on 30 September 2024
        (1%) and Rs 10,000 on 2 October 2024 (0.1%). In date order the first
        payment uses the last Rs 5,000 of exemption: Rs 5,000 at 1% plus
        Rs 10,000 at 0.1% is Rs 60. Taken in file order — October first — it
        would be Rs 105, and the difference is the rate change.
        """
        txns = [_Txn("OCT", 1_000_000, utc(2024, 10, 2)),
                _Txn("SEP", 1_000_000, utc(2024, 9, 30))]
        f = audit_tds_194o(txns, annual_gross_cents=495_000_00, rate_card=MethodRateCard())
        assert f[0].expected_cents == 6_000

    def test_withheld_tds_can_come_from_a_file_column(self):
        txns = [_Txn("P1", 1_000_000, utc(2026, 6, 1), extra={"tds": "10.00"})]
        assert audit_tds_194o(txns, rate_card=self.NO_THRESHOLD) == []

    def test_an_explicit_rate_still_overrides_the_schedule(self):
        txns = [_Txn("P1", 1_000_000, utc(2026, 6, 1))]
        f = audit_tds_194o(txns, rate_card=MethodRateCard(
            tds_rate_bps=100, tds_annual_threshold_cents=0))
        assert f[0].expected_cents == 10_000


class TestTcs:
    def test_nothing_is_expected_when_the_data_reports_no_tcs(self):
        txns = [_Txn("P1", 10_000_000, utc(2026, 6, 1))]
        assert audit_tcs_section52(txns) == [], (
            "a gateway that correctly collects no TCS must not be flagged"
        )

    def test_tcs_at_half_a_percent_passes(self):
        txns = [_Txn("P1", 10_000_000, utc(2026, 6, 1), extra={"tcs_amount_cents": 50_000})]
        assert audit_tcs_section52(txns) == []

    def test_tcs_still_at_the_old_one_percent_is_flagged(self):
        txns = [_Txn("P1", 10_000_000, utc(2026, 6, 1), extra={"tcs_amount_cents": 100_000})]
        f = audit_tcs_section52(txns)
        assert f and f[0].category == FeeAuditCategory.TCS_COLLECTION_ERROR
        assert f[0].expected_cents == 50_000
        assert "Section 52" in f[0].citation

    def test_returns_reduce_the_base(self):
        # Net taxable value is supplies less returns: Rs 1,00,000 - Rs 40,000.
        txns = [_Txn("P1", 10_000_000, utc(2026, 6, 1), extra={"tcs_amount_cents": 30_000}),
                _Txn("R1", -4_000_000, utc(2026, 6, 3))]
        assert audit_tcs_section52(txns) == []

    def test_a_june_2024_supply_is_at_one_percent(self):
        txns = [_Txn("P1", 10_000_000, utc(2024, 6, 1), extra={"tcs_amount_cents": 100_000})]
        assert audit_tcs_section52(txns) == []


class TestTheTwoFeeConventions:
    def test_razorpay_fee_includes_its_tax(self):
        # Razorpay reference: fee 296 with tax 46 is 250 of fee and 46 of GST.
        assert fee_fields({"fee": 296, "tax": 46}) == (29_600 - 4_600, 4_600)

    def test_the_engine_keys_are_exclusive_of_tax(self):
        assert fee_fields({"fee_amount_cents": 250, "gst_amount_cents": 45}) == (250, 45)

    def test_a_razorpay_row_raises_no_false_gst_finding(self):
        # Rs 1,000 card payment, 2% fee = Rs 20, GST Rs 3.60, fee column Rs 23.60.
        txn = _Txn("pay_1", 100_000, extra={"method": "card", "fee": "23.60", "tax": "3.60"})
        assert audit_transaction_fees([txn], MethodRateCard(card_bps=200)) == [], (
            "reading Razorpay's fee as pre-GST would flag every row"
        )

    def test_silence_is_not_zero(self):
        assert fee_fields({}) == (None, None)


class TestInputTaxCredit:
    def invoice(self, gst):
        return india_tax.GstInvoice("RZP/26-27/0042", "2026-08", 1_000_000, gst)

    def test_matched(self):
        r = india_tax.itc_check(self.invoice(180_000), 1_000_000, 180_000)
        assert r["status"] == "matched"
        assert r["claimable_itc_cents"] == 180_000
        assert r["invoice_internally_consistent"]

    def test_deducted_more_than_invoiced_is_not_yet_claimable(self):
        r = india_tax.itc_check(self.invoice(180_000), 1_050_000, 189_000)
        assert r["status"] == "deducted_more_than_invoiced"
        assert r["not_yet_claimable_cents"] == 9_000
        assert r["claimable_itc_cents"] == 180_000
        assert "debit note" in r["plain"]

    def test_invoiced_more_than_deducted_claims_only_what_was_paid(self):
        r = india_tax.itc_check(self.invoice(180_000), 900_000, 162_000)
        assert r["status"] == "invoiced_more_than_deducted"
        assert r["claimable_itc_cents"] == 162_000
        assert "credit note" in r["plain"]

    def test_an_invoice_whose_gst_is_not_18_percent_of_itself_is_flagged(self):
        r = india_tax.itc_check(self.invoice(200_000), 1_000_000, 200_000)
        assert not r["invoice_internally_consistent"]


class TestTheAuditRunsItAll:
    def test_tcs_is_in_the_summary(self):
        txns = [_Txn("P1", 10_000_000, utc(2026, 6, 1), extra={"tcs_amount_cents": 100_000})]
        _, summary = run_fee_audit(txns, rate_card=MethodRateCard(tds_rate_bps=0))
        assert summary["tcs_compliance"] == "issue"


class TestTheEndpoints:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        import main
        return TestClient(main.app)

    def test_provisions_on_a_date(self, client):
        body = client.get("/tax/provisions", params={"on": "2026-04-01"}).json()
        assert "393(1)" in body["ecommerce_tds"]["citation"]
        assert body["gst_tcs"]["rate_bps"] == 50

    def test_a_bad_date_is_refused_plainly(self, client):
        r = client.get("/tax/provisions", params={"on": "1 April"})
        assert r.status_code == 422

    def test_itc_check(self, client):
        r = client.post("/tax/itc-check", json={
            "invoice_number": "RZP/26-27/0042", "period": "2026-08",
            "invoice_taxable_value_cents": 1_000_000, "invoice_gst_cents": 180_000,
            "deducted_fee_cents": 1_050_000, "deducted_gst_cents": 189_000})
        assert r.status_code == 200
        assert r.json()["not_yet_claimable_cents"] == 9_000

    def test_a_reconciliation_now_returns_its_fee_audit(self, client):
        """
        The fee audit ran on every cleared match and no response carried it.
        A check nobody can see is not a check.
        """
        from pathlib import Path
        samples = Path(__file__).resolve().parents[2] / "public" / "sample-data"
        files = {
            "gateway_file": ("g.csv", (samples / "gateway_report.csv").read_bytes(), "text/csv"),
            "bank_file": ("b.csv", (samples / "bank_statement.csv").read_bytes(), "text/csv"),
            "erp_file": ("e.json", (samples / "erp_ledger.json").read_bytes(), "application/json"),
        }
        r = client.post("/reconcile/upload", data={
            "batch_id": "TAX-FEE-VIEW", "net_amount": "66466.36",
            "settled_at": "2026-09-02T00:00:00Z", "settlement_window_days": "5",
            "currency": "INR", "declared_deductions": "2055.66",
            "member_source": "gateway"}, files=files)
        assert r.status_code == 200
        assert "fee_audit" in r.json()
        fa = r.json()["fee_audit"]
        assert fa is None or "summary" in fa
