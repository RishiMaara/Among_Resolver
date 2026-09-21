"""
Exceptions filed the way a finance team routes them, and the approved posting
exported for Tally.

The Tally file is only produced for a balanced journal that a person approved
— and separation of duties already stops that person being whoever accepted
the match. Tally's sign convention (debits negative, deemed positive) is
tested rather than trusted, because getting it backwards imports a mirror
image without an error.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from defusedxml import ElementTree as ET
from fastapi.testclient import TestClient

import audit
import exception_taxonomy as tax
import main
import tally_export

SAMPLES = Path(__file__).resolve().parents[2] / "public" / "sample-data"
BATCH = "SETTLE-001"      # the id the sample's references carry, so it clears


class TestTheTaxonomy:
    @pytest.mark.parametrize("reason, feeds, expected", [
        ("timing_lag", {"gateway"}, "in_transit"),
        ("duplicate", {"gateway"}, "duplicate"),
        ("partial_payment", {"erp"}, "split_or_partial"),
        ("low_confidence", {"bank"}, "amount_mismatch"),
        ("compliance_block", {"gateway"}, "compliance_hold"),
        ("missing_entry", {"bank"}, "unidentified_receipt"),
        ("missing_entry", {"erp"}, "not_in_gateway"),
        ("missing_entry", {"gateway"}, "missing_in_books"),
        ("unresolved", {"gateway"}, "unidentified"),
    ])
    def test_each_reason_lands_in_a_finance_category(self, reason, feeds, expected):
        sources = {"T1": feeds}
        assert tax.categorise({"reason": reason, "candidate_txn_ids": ["T1"]}, sources) == expected

    def test_every_category_has_an_owner_and_a_first_step(self):
        for meta in tax.CATEGORIES.values():
            assert meta["owner"] and meta["next_action"] and meta["label"]


@pytest.fixture
def client():
    return TestClient(main.app)


def reconcile(client, bid=BATCH):
    files = {
        "gateway_file": ("g.csv", (SAMPLES / "gateway_report.csv").read_bytes(), "text/csv"),
        "bank_file": ("b.csv", (SAMPLES / "bank_statement.csv").read_bytes(), "text/csv"),
        "erp_file": ("e.json", (SAMPLES / "erp_ledger.json").read_bytes(), "application/json"),
    }
    r = client.post("/reconcile/upload", data={
        "batch_id": bid, "net_amount": "66466.36", "settled_at": "2026-09-02T00:00:00Z",
        "settlement_window_days": "5", "currency": "INR", "declared_deductions": "2055.66",
        "member_source": "gateway"}, files=files)
    assert r.status_code == 200, r.text
    return r.json()


class TestTheReportCarriesIt:
    def test_every_exception_says_whose_it_is(self, client):
        audit.clear_trail("TAX-SAMPLE")
        body = reconcile(client, "TAX-SAMPLE")
        assert body["exceptions"], "the sample leaves exceptions to file"
        for e in body["exceptions"]:
            assert e["category"] in tax.CATEGORIES and e["owner"] and e["next_action"]
        cats = body["exceptions_summary"]["by_category"]
        assert sum(c["count"] for c in cats) == len(body["exceptions"])


class TestTheVoucher:
    JOURNAL = {"entry_id": "JE-X", "lines": [
        {"account": tally_export.cash_position.ACC_BANK, "debit_inr": 664.66, "credit_inr": 0},
        {"account": tally_export.cash_position.ACC_GATEWAY_FEES, "debit_inr": 20.56, "credit_inr": 0},
        {"account": tally_export.cash_position.ACC_GATEWAY_CLEARING, "debit_inr": 0, "credit_inr": 685.22},
    ]}

    def xml(self, **kw):
        from datetime import date
        return tally_export.journal_to_tally(self.JOURNAL, voucher_date=date(2026, 9, 2), **kw)

    def entries(self, xml):
        root = ET.fromstring(xml)
        return [(e.findtext("LEDGERNAME"), e.findtext("ISDEEMEDPOSITIVE"), Decimal(e.findtext("AMOUNT")))
                for e in root.iter("ALLLEDGERENTRIES.LIST")]

    def test_debits_are_negative_and_deemed_positive(self):
        rows = self.entries(self.xml())
        bank = next(r for r in rows if r[0] == "Bank Account")
        clearing = next(r for r in rows if r[0] == "Razorpay Clearing")
        assert bank[1] == "Yes" and bank[2] == Decimal("-664.66")
        assert clearing[1] == "No" and clearing[2] == Decimal("685.22")

    def test_the_voucher_sums_to_zero(self):
        assert sum(r[2] for r in self.entries(self.xml())) == 0

    def test_ledger_names_come_from_the_company(self):
        rows = self.entries(self.xml(ledgers={tally_export.cash_position.ACC_BANK: "HDFC Current A/c"}))
        assert rows[0][0] == "HDFC Current A/c"

    def test_an_unbalanced_journal_is_refused(self):
        from datetime import date
        bad = {"lines": [{"account": "A", "debit_inr": 10, "credit_inr": 0},
                         {"account": "B", "debit_inr": 0, "credit_inr": 9.99}]}
        with pytest.raises(ValueError):
            tally_export.journal_to_tally(bad, voucher_date=date(2026, 9, 2))

    def test_company_and_narration_are_escaped(self):
        xml = self.xml(company="A & B <Traders>", narration="x < y")
        assert "A &amp; B &lt;Traders&gt;" in xml
        ET.fromstring(xml)


class TestTheExportRoute:
    @pytest.fixture(autouse=True)
    def fresh(self):
        audit.clear_trail(BATCH)
        yield
        audit.clear_trail(BATCH)

    def export(self, client, who="Meera"):
        return client.post(f"/settlement/{BATCH}/export/tally", json={"reviewer": who})

    def decide(self, client, who, decision="approved"):
        return client.post(f"/settlement/{BATCH}/journal/decision",
                           json={"decision": decision, "reviewer": who, "entry_id": f"JE-{BATCH}"})

    def test_nothing_is_exported_before_approval(self, client):
        body = reconcile(client)
        assert body["summary"]["cleared"] and body["cash_position"]["journal"]["balanced"]
        r = self.export(client)
        assert r.status_code == 409 and "Nobody has approved" in r.json()["detail"]["plain"]

    def test_an_approved_posting_exports(self, client):
        reconcile(client)
        client.post(f"/settlement/{BATCH}/decision", json={"decision": "confirmed", "reviewer": "Rishi"})
        assert self.decide(client, "Priya").status_code == 200
        r = self.export(client)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/xml")
        assert "tally_SETTLE-001.xml" in r.headers["content-disposition"]
        amounts = [Decimal(e.findtext("AMOUNT")) for e in ET.fromstring(r.text).iter("ALLLEDGERENTRIES.LIST")]
        assert sum(amounts) == 0
        assert any(e["agent"] == "tally_export" and "Meera" in e["detail"] and "Priya" in e["detail"]
                   for e in audit.get_audit_trail(BATCH)), "the export is on the record"

    def test_a_rejected_posting_does_not_export(self, client):
        reconcile(client)
        self.decide(client, "Priya")
        self.decide(client, "Priya", "rejected")
        r = self.export(client)
        assert r.status_code == 409 and "rejection by priya" in r.json()["detail"]["plain"].lower()

    def test_a_batch_never_reconciled_has_nothing_to_export(self, client):
        r = client.post("/settlement/NEVER-SEEN-XYZ/export/tally", json={"reviewer": "Meera"})
        assert r.status_code == 409

    def test_an_export_needs_a_name(self, client):
        assert client.post(f"/settlement/{BATCH}/export/tally", json={"reviewer": " "}).status_code == 422
