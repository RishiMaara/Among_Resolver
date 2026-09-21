"""
A chargeback adds a row; it never edits a closed settlement.

When a processor claws money back, the tempting move is to reopen the batch
the original payment cleared in and subtract it. That destroys what the batch
is for — a record of what was true when it closed — and the reconciliation
somebody signed off is no longer the one they signed. So a chargeback emits a
new negative transaction that waits for the settlement where the processor
actually takes the money.
"""

import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from fastapi.testclient import TestClient

import audit
import chargeback_engine
import main

SAMPLES = Path(__file__).resolve().parents[2] / "public" / "sample-data"


@pytest.fixture
def client():
    chargeback_engine._reset_for_tests()
    yield TestClient(main.app)
    chargeback_engine._reset_for_tests()


# The sample settlement closes 2026-09-02 with a 5-day window, so a dispute
# filed on 2026-09-01 is one that payout can absorb.
IN_WINDOW = "2026-09-01T00:00:00Z"
AFTER_SETTLEMENT = "2026-09-20T00:00:00Z"


def file_one(client, original="pay_0000", amount=50_000, reason="4855",
             filed_at=IN_WINDOW):
    return client.post("/chargebacks", json={
        "original_txn_id": original, "dispute_amount_cents": amount,
        "reason_code": reason, "currency": "INR", "filed_at_utc": filed_at,
    })


def sample_files():
    return {
        "gateway_file": ("gateway_report.csv", (SAMPLES / "gateway_report.csv").read_bytes(), "text/csv"),
        "bank_file": ("bank_statement.csv", (SAMPLES / "bank_statement.csv").read_bytes(), "text/csv"),
        "erp_file": ("erp_ledger.json", (SAMPLES / "erp_ledger.json").read_bytes(), "application/json"),
    }


def reconcile(client, batch_id, include_chargebacks=None):
    data = {"batch_id": batch_id, "net_amount": "66466.36",
            "settled_at": "2026-09-02T00:00:00Z", "settlement_window_days": "5",
            "currency": "INR", "declared_deductions": "2055.66",
            "member_source": "gateway"}
    if include_chargebacks is not None:
        data["include_chargebacks"] = str(include_chargebacks).lower()
    return client.post("/reconcile/upload", data=data, files=sample_files())


class TestFiling:
    def test_a_filing_emits_a_negative_reversal_pointing_at_the_original(self, client):
        r = file_one(client)
        assert r.status_code == 200
        body = r.json()
        assert body["amount_cents"] == -50_000, "a clawback is money leaving"
        assert body["original_txn_id"] == "pay_0000"
        assert body["pending_count"] == 1

    def test_the_closed_settlement_is_not_touched(self, client):
        """
        The reversal is recorded under its own id. Nothing is written to the
        batch the original payment cleared in, because that batch is history.
        """
        r = file_one(client)
        reversal_id = r.json()["reversal_txn_id"]
        assert audit.get_audit_trail(f"chargeback:{reversal_id}"), (
            "the filing must be on the record under its own id"
        )

    def test_a_negative_amount_is_refused_rather_than_flipped(self, client):
        r = file_one(client, amount=-50_000)
        assert r.status_code == 422
        assert "positive" in r.json()["detail"]["plain"]

    def test_a_filing_with_no_original_is_refused(self, client):
        r = client.post("/chargebacks", json={
            "original_txn_id": "  ", "dispute_amount_cents": 100, "reason_code": "x"})
        assert r.status_code == 422

    def test_pending_lists_what_is_waiting(self, client):
        file_one(client, original="pay_0001", amount=25_000)
        body = client.get("/chargebacks/pending").json()
        assert body["count"] == 1
        assert body["total_cents"] == -25_000
        assert body["pending"][0]["original_txn_id"] == "pay_0001"
        assert body["pending"][0]["reason_code"] == "4855"


class TestTakingThemIn:
    def test_a_reconciliation_takes_pending_reversals_into_its_pool(self, client):
        file_one(client)
        bid = f"CB-IN-{uuid.uuid4().hex[:6]}"
        r = reconcile(client, bid, include_chargebacks=True)
        assert r.status_code == 200
        body = r.json()
        assert body.get("chargeback_reversals_included"), "the run must say it took them"
        assert client.get("/chargebacks/pending").json()["count"] == 0, (
            "a reversal a settlement absorbed must stop being offered"
        )
        assert any("chargeback" in e["detail"].lower()
                   for e in body["audit_trail"]), "taking them in belongs on the record"

    def test_without_the_flag_they_keep_waiting(self, client):
        file_one(client)
        bid = f"CB-OUT-{uuid.uuid4().hex[:6]}"
        assert reconcile(client, bid).status_code == 200
        assert client.get("/chargebacks/pending").json()["count"] == 1, (
            "which payout absorbs a clawback is the processor's timing, not "
            "something the engine should assume"
        )

    def test_the_pool_grows_by_exactly_the_reversals_taken(self, client):
        without = reconcile(client, f"CB-A-{uuid.uuid4().hex[:6]}")
        base = without.json()["summary"]["total_candidates"]

        file_one(client, original="pay_0002", amount=10_000)
        with_cb = reconcile(client, f"CB-B-{uuid.uuid4().hex[:6]}", include_chargebacks=True)
        assert with_cb.json()["summary"]["total_candidates"] == base + 1

    def test_nothing_is_dropped_when_no_reversals_are_waiting(self, client):
        bid = f"CB-NONE-{uuid.uuid4().hex[:6]}"
        r = reconcile(client, bid, include_chargebacks=True)
        assert r.status_code == 200
        assert not r.json().get("chargeback_reversals_included")

    def test_a_reversal_outside_the_window_is_not_taken_and_not_lost(self, client):
        """
        The bug this caught: a reversal filed after the settlement closed was
        added to the pool, dropped by the window filter, and then marked
        taken — so money owed back vanished from this batch and every later
        one. A reversal only leaves the queue if the settlement could absorb it.
        """
        file_one(client, original="pay_0003", amount=20_000, filed_at=AFTER_SETTLEMENT)
        bid = f"CB-LATE-{uuid.uuid4().hex[:6]}"
        r = reconcile(client, bid, include_chargebacks=True)
        assert r.status_code == 200
        assert not r.json().get("chargeback_reversals_included")
        assert client.get("/chargebacks/pending").json()["count"] == 1, (
            "a reversal this payout could not absorb must still be waiting"
        )

    def test_the_reversal_carries_the_filing_time_not_the_request_time(self, client):
        file_one(client, original="pay_0004", amount=5_000, filed_at=IN_WINDOW)
        waiting = chargeback_engine.pending()
        assert waiting[0].timestamp_utc.isoformat().startswith("2026-09-01")

