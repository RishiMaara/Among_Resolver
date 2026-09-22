"""
Razorpay as the primary feed: its membership, checked five ways.

The sample under public/sample-data/razorpay is built so each outcome
appears once: three clean payouts, one with a fee overcharge and an unbooked
order, one whose bank credit is Rs 10 short, and two lines on hold.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
import razorpay_recon
import razorpay_source as rz
import settlement_cycle
from schema import NormalizedTxn, SourceType, TzConfidence

SAMPLE = Path(__file__).resolve().parents[2] / "public" / "sample-data" / "razorpay"


def load(name):
    return json.loads((SAMPLE / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sample_result():
    c = TestClient(main.app)
    files = {
        "settlements_file": ("settlements.json", (SAMPLE / "settlements.json").read_bytes(), "application/json"),
        "recon_file": ("recon_combined.json", (SAMPLE / "recon_combined.json").read_bytes(), "application/json"),
        "bank_file": ("bank_statement.csv", (SAMPLE / "bank_statement.csv").read_bytes(), "text/csv"),
        "ledger_file": ("ledger.json", (SAMPLE / "ledger.json").read_bytes(), "application/json"),
    }
    r = c.post("/razorpay/reconcile/upload", files=files)
    assert r.status_code == 200, r.text
    return r.json()


def by_id(result, sid):
    return next(r for r in result["results"] if r["settlement_id"] == sid)


class TestTheSample:
    def test_each_outcome_appears(self, sample_result):
        assert sample_result["tally"] == {"verified": 3, "verified_with_findings": 1,
                                          "not_verified": 1, "no_lines": 0}

    def test_every_payout_ties_out_to_the_paisa(self, sample_result):
        for r in sample_result["results"]:
            assert r["checks"]["tie_out"]["residual_paise"] == 0

    def test_a_short_bank_credit_is_not_verified(self, sample_result):
        r = by_id(sample_result, "setl_SAMPLE000004")
        assert r["status"] == "not_verified"
        bank = r["checks"]["bank"]
        assert bank["verdict"] == "amount_differs" and bank["difference_cents"] == -1_000
        assert "₹10.00 less" in bank["plain"]

    def test_an_overcharge_and_an_unbooked_order_are_findings(self, sample_result):
        r = by_id(sample_result, "setl_SAMPLE000002")
        assert r["status"] == "verified_with_findings"
        assert r["checks"]["books"]["verdict"] == "missing_in_books"
        assert len(r["checks"]["books"]["missing"]) == 1
        cats = {f["category"] for f in r["checks"]["fees"]["findings"]}
        assert cats == {"fee_overcharge"}

    def test_a_refund_is_not_audited_as_a_sale(self, sample_result):
        """Refunds carry no MDR; read as sales they were flagged as undercharges."""
        for r in sample_result["results"]:
            for f in r["checks"]["fees"]["findings"]:
                assert not f["txn_id"].startswith("rfnd_")

    def test_lines_on_hold_are_reported(self, sample_result):
        assert sample_result["unsettled_lines"]["count"] == 2

    def test_the_blind_solve_reaches_razorpays_set_without_its_ids(self, sample_result):
        for r in sample_result["results"]:
            blind = r["checks"]["blind_solve"]
            assert blind["verdict"] in ("agrees", "agrees_as_proposal"), blind["plain"]
            assert blind["cycle_learned_from_other_settlements"] == 4


class TestTheBlindCheck:
    def test_without_other_settlements_there_is_no_cycle_to_use(self):
        settlements, recon = load("settlements.json")["items"], load("recon_combined.json")["items"]
        out = razorpay_recon.blind_check(settlements[0], recon, others=[])
        assert out["cycle_learned_from_other_settlements"] == 0

    def test_a_different_set_cleared_blind_is_a_disagreement(self, monkeypatch):
        """If the engine confidently clears something else, that is a finding."""
        import pipeline
        settlements, recon = load("settlements.json")["items"], load("recon_combined.json")["items"]
        real = pipeline.reconcile_settlement

        def wrong(*a, **k):
            report = real(*a, **k)
            report.match_result.matched_txn_ids = ["pay_NOT_RAZORPAYS"]
            report.match_result.cleared = True
            return report

        monkeypatch.setattr(pipeline, "reconcile_settlement", wrong)
        out = razorpay_recon.reconcile(settlements[:1], recon)
        assert out["results"][0]["checks"]["blind_solve"]["verdict"] == "disagrees"
        assert out["results"][0]["status"] == "not_verified"

    def test_the_checked_settlement_never_teaches_its_own_blind_solve(self):
        settlements = load("settlements.json")["items"]
        recon = load("recon_combined.json")["items"]
        seen = []
        real = settlement_cycle.profile_from

        def spy(lags, n):
            seen.append(n)
            return real(lags, n)

        settlement_cycle.profile_from = spy
        try:
            razorpay_recon.reconcile(settlements, recon)
        finally:
            settlement_cycle.profile_from = real
        assert seen == [4] * 5, "each blind solve learns from the other four only"


class TestTheOtherChecks:
    def txn(self, tid, amount, memo=""):
        from datetime import datetime, timezone
        return NormalizedTxn(source=SourceType.BANK, source_txn_id=tid, ref_id_canonical=tid,
                             amount_cents=amount, currency="INR",
                             timestamp_utc=datetime(2026, 9, 12, tzinfo=timezone.utc),
                             tz_confidence=TzConfidence.HIGH, memo_raw=memo)

    def test_bank_amount_only(self):
        out = razorpay_recon.bank_check({"utr": "UTRX1", "amount": 5_000}, [self.txn("B1", 5_000)])
        assert out["verdict"] == "amount_only"

    def test_bank_not_found(self):
        out = razorpay_recon.bank_check({"utr": "UTRX1", "amount": 5_000}, [self.txn("B1", 4_999)])
        assert out["verdict"] == "not_found"

    def test_bank_utr_in_the_narration(self):
        out = razorpay_recon.bank_check({"utr": "UTRX1", "amount": 5_000},
                                        [self.txn("B1", 5_000, "NEFT CR UTRX1 RAZORPAY")])
        assert out["verdict"] == "arrived"

    def test_a_tampered_line_does_not_tie_out(self):
        settlements = load("settlements.json")["items"]
        recon = copy.deepcopy(load("recon_combined.json")["items"])
        first = next(i for i in recon if i["settlement_id"] == settlements[0]["id"])
        first["credit"] += 100
        out = razorpay_recon.reconcile(settlements[:1], recon, blind=False)
        assert out["results"][0]["checks"]["tie_out"]["verdict"] == "does_not_tie_out"
        assert out["results"][0]["status"] == "not_verified"

    def test_razorpays_membership_teaches_the_cycle(self):
        settlements, recon = load("settlements.json")["items"], load("recon_combined.json")["items"]
        razorpay_recon.reconcile(settlements, recon, blind=False)
        prof = settlement_cycle.profile("gateway", "INR")
        assert prof and max(prof["m"], key=prof["m"].get) == "2", "T+2"


class TestTheRoutes:
    @pytest.fixture
    def client(self):
        return TestClient(main.app)

    def test_status_without_keys(self, client, monkeypatch):
        monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
        monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
        assert client.get("/razorpay/status").json()["connected"] is False

    def test_status_never_shows_the_secret(self, client, monkeypatch):
        monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_ABCDEFGHIJKL")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "s3cr3t-value-never-shown")
        body = client.get("/razorpay/status").json()
        assert body["mode"] == "test"
        assert "s3cr3t" not in json.dumps(body)

    def test_live_without_keys_says_how_to_connect(self, client, monkeypatch):
        monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
        r = client.post("/razorpay/reconcile", json={"year": 2026, "month": 9})
        assert r.status_code == 503
        assert "RAZORPAY_KEY_ID" in r.json()["detail"]["plain"]

    def test_bad_json_is_refused_plainly(self, client):
        r = client.post("/razorpay/reconcile/upload", files={
            "settlements_file": ("s.json", b"not json", "application/json"),
            "recon_file": ("r.json", b"[]", "application/json")})
        assert r.status_code == 422

    def test_a_single_settlement_entity_and_a_bare_list_are_accepted(self, client):
        s = load("settlements.json")["items"][0]
        recon = [i for i in load("recon_combined.json")["items"] if i["settlement_id"] == s["id"]]
        r = client.post("/razorpay/reconcile/upload", files={
            "settlements_file": ("s.json", json.dumps(s).encode(), "application/json"),
            "recon_file": ("r.json", json.dumps(recon).encode(), "application/json")})
        assert r.status_code == 200
        assert r.json()["results"][0]["checks"]["tie_out"]["verdict"] == "ties_out"


class TestTheProfileOverride:
    def test_an_override_is_used_and_then_forgotten(self):
        prof = settlement_cycle.profile_from([2] * 30, 3)
        with settlement_cycle.using(prof):
            assert settlement_cycle.profile("gateway", "INR") is prof
        assert settlement_cycle.profile("gateway", "INR") is None


def upload_report(files):
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app).post("/razorpay/reconcile/upload", files=files).json()


class TestTheDashboardReport:
    """
    A merchant reconciles the report they download, with no keys and no one
    else's help. It must reach what the API path reaches, and say how it read
    the file.
    """

    def test_the_report_is_read_as_the_api_sends_it(self):
        items, note = rz.report_items((SAMPLE / "settlement_report.csv").read_bytes())
        api = load("recon_combined.json")["items"]
        assert "rupees" in note and len(items) == len(api)
        for got, want in zip(items, api):
            assert (got["entity_id"], got["credit"], got["debit"], got["settled_at"]) == \
                (want["entity_id"], want["credit"], want["debit"], want["settled_at"])

    def test_the_report_alone_with_the_bank_reaches_the_api_verdicts(self, sample_result):
        body = upload_report({
            "recon_file": ("settlement_report.csv", (SAMPLE / "settlement_report.csv").read_bytes(), "text/csv"),
            "bank_file": ("bank_statement.csv", (SAMPLE / "bank_statement.csv").read_bytes(), "text/csv"),
            "ledger_file": ("ledger.json", (SAMPLE / "ledger.json").read_bytes(), "application/json"),
        })
        assert body["read"]["recon"].startswith("dashboard report")
        assert {r["settlement_id"]: r["status"] for r in body["results"]} == \
            {r["settlement_id"]: r["status"] for r in sample_result["results"]}
        assert all(r["checks"]["tie_out"]["verdict"] == "derived" for r in body["results"])

    def test_without_a_bank_statement_nothing_is_verified_by_its_own_sum(self):
        body = upload_report({
            "recon_file": ("settlement_report.csv", (SAMPLE / "settlement_report.csv").read_bytes(), "text/csv")})
        assert body["tally"]["verified"] == 0
        assert "by construction" in body["results"][0]["checks"]["tie_out"]["plain"]

    def test_a_report_read_in_the_wrong_unit_is_caught_by_the_bank(self):
        # Whole-rupee amounts with no decimals read as paise: 100x too small.
        text = (SAMPLE / "settlement_report.csv").read_text(encoding="utf-8")
        whole = "\n".join(",".join(c.split(".")[0] if c.replace(".", "", 1).isdigit() else c
                                   for c in line.split(",")) for line in text.splitlines())
        body = upload_report({
            "recon_file": ("r.csv", whole.encode(), "text/csv"),
            "bank_file": ("bank_statement.csv", (SAMPLE / "bank_statement.csv").read_bytes(), "text/csv")})
        assert "paise" in body["read"]["units"]
        assert body["tally"]["verified"] == 0 and body["tally"]["not_verified"] > 0

    def test_documented_units_hold_in_the_api_sample(self):
        # Razorpay documents recon amounts as integer subunits and times as
        # Unix seconds; the sample the checks are built on keeps to that.
        for i in load("recon_combined.json")["items"]:
            assert all(isinstance(i[f], int) for f in ("debit", "credit", "amount", "fee", "tax"))
            # A line on hold has not settled, so it has no settlement time yet.
            if i["settled"]:
                assert isinstance(i["settled_at"], int) and i["settled_at"] > 1_600_000_000
            else:
                assert i["settled_at"] is None and not i["settlement_id"]
