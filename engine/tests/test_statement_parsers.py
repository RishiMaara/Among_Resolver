"""
Bank statements in the formats banks send, and the proof each was read right.

Every sample is the same account and week in a different format, carrying
the demo settlement credit. Each must balance; each tampered copy must be
refused with the arithmetic shown, not partly used.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import file_agent
import main
import statement_parsers as sp

ROOT = Path(__file__).resolve().parents[2] / "public" / "sample-data"
STATEMENTS = ROOT / "statements"
SAMPLES = {"mt940": "statement.mt940", "camt053": "statement_camt053.xml",
           "ofx": "statement.ofx", "pdf": "statement.pdf"}
SETTLEMENT = ("UTR20260901001", "66466.36")


def read(name):
    return (STATEMENTS / name).read_bytes()


@pytest.mark.parametrize("fmt, name", SAMPLES.items())
class TestEveryFormat:
    def test_it_is_recognised(self, fmt, name):
        assert sp.detect(read(name), name) == fmt

    def test_it_balances(self, fmt, name):
        st, check = sp.parse(read(name), name)
        assert check["holds"], check["plain"]
        assert len(st.lines) == 6
        assert st.opening_cents == 1_254_320_00 and st.closing_cents == 923_753_86

    def test_the_settlement_credit_comes_through(self, fmt, name):
        st, _ = sp.parse(read(name), name)
        rows = sp.to_rows(st)
        assert any(r["ref_id"] == SETTLEMENT[0] and r["amount"] == SETTLEMENT[1] for r in rows)

    def test_only_credits_go_to_reconciliation(self, fmt, name):
        st, _ = sp.parse(read(name), name)
        assert all(float(r["amount"]) > 0 for r in sp.to_rows(st))
        assert len(sp.to_rows(st)) == 3


class TestTamperingIsRefused:
    def test_an_mt940_with_a_changed_amount_does_not_balance(self):
        text = read("statement.mt940").decode().replace("C66466,36", "C66466,63")
        with pytest.raises(file_agent.FileRejected) as exc:
            file_agent.parse_file_content(text.encode(), "statement.mt940")
        assert "does not balance" in exc.value.plain
        assert "off by ₹0.27" in exc.value.plain

    def test_a_pdf_line_that_does_not_move_the_balance_is_refused(self):
        body = read("statement.pdf").replace(b"66,466.36", b"66,466.63")
        with pytest.raises(file_agent.FileRejected) as exc:
            file_agent.parse_file_content(body, "statement.pdf")
        assert "does not move the balance" in exc.value.plain

    def test_a_scan_with_no_text_layer_is_refused_with_the_reason(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from make_statement_samples import pdf
        with pytest.raises(file_agent.FileRejected) as exc:
            file_agent.parse_file_content(pdf([]), "scan.pdf")
        assert "no text layer" in exc.value.plain

    def test_camt053_with_an_entity_is_not_expanded(self):
        """External entities in uploaded XML are an attack, not a statement."""
        evil = (b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                b'<Document><BkToCstmrStmt><Stmt><Id>&x;</Id></Stmt></BkToCstmrStmt></Document>')
        with pytest.raises(sp.StatementUnreadable):
            sp.parse(evil, "evil.xml")


class TestFormatDetails:
    def test_ofx_says_its_balance_check_is_weaker(self):
        st, check = sp.parse(read("statement.ofx"), "statement.ofx")
        assert check["golden_rule"]["checkable"] is False
        assert any("derived" in n for n in st.notes)

    def test_mt940_debit_balance_is_negative(self):
        text = (":20:X\n:25:ACC\n:60F:D260901INR100,00\n"
                ":61:2609010901C150,00NTRFREF1\n:86:CREDIT\n:62F:C260901INR50,00\n")
        st, check = sp.parse(text.encode(), "x.sta")
        assert st.opening_cents == -10_000 and check["holds"]

    def test_camt_pending_entries_are_left_out(self):
        xml = read("statement_camt053.xml").decode()
        xml = xml.replace("<Sts><Cd>BOOK</Cd></Sts>", "<Sts><Cd>PDNG</Cd></Sts>", 1)
        st, check = sp.parse(xml.encode(), "c.xml")
        assert len(st.lines) == 5
        assert not check["holds"], "a pending line that moved the balance would show here"

    def test_a_csv_is_not_a_statement(self):
        assert sp.detect((ROOT / "bank_statement.csv").read_bytes(), "bank_statement.csv") is None


class TestInTheApp:
    @pytest.fixture
    def client(self):
        return TestClient(main.app)

    def test_parse_endpoint(self, client):
        r = client.post("/statements/parse", files={"file": ("statement.pdf", read("statement.pdf"),
                                                             "application/pdf")})
        body = r.json()
        assert r.status_code == 200 and body["format"] == "pdf" and body["check"]["holds"]
        assert body["check"]["running_balance"]["holds"]

    def test_an_mt940_stands_in_for_the_csv_bank_file(self, client):
        """Same payout, same answer, whichever form the bank file takes."""
        def run(name, body, bid):
            files = {
                "gateway_file": ("g.csv", (ROOT / "gateway_report.csv").read_bytes(), "text/csv"),
                "bank_file": (name, body, "text/plain"),
                "erp_file": ("e.json", (ROOT / "erp_ledger.json").read_bytes(), "application/json"),
            }
            r = client.post("/reconcile/upload", data={
                "batch_id": bid, "net_amount": "66466.36",
                "settled_at": "2026-09-02T00:00:00Z", "settlement_window_days": "5",
                "currency": "INR", "declared_deductions": "2055.66",
                "member_source": "gateway"}, files=files)
            assert r.status_code == 200, r.text
            return r.json()

        csv_run = run("bank_statement.csv", (ROOT / "bank_statement.csv").read_bytes(), "SETTLE-001")
        mt_run = run("statement.mt940", read("statement.mt940"), "SETTLE-001")
        # SETTLE-001 is the id the sample's references carry, so the payout is
        # anchored and decidable; an undecidable one proposes an arbitrary set
        # among equal sums, and comparing arbitrary sets proves nothing.
        assert csv_run["summary"]["cleared"] and mt_run["summary"]["cleared"]
        for key in ("matched_count", "confidence"):
            assert mt_run["summary"][key] == csv_run["summary"][key], key
        assert sorted(mt_run["matched_txn_ids"]) == sorted(csv_run["matched_txn_ids"])
        notes = " ".join(mt_run.get("ingestion_notes") or [])
        assert re.search(r"Read as a MT940 bank statement", notes)
