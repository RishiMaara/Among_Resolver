"""
The audit trail is a hash chain, and tampering with it is detected.

Each test tampers the way someone with database access would: edit a word,
delete a row, cut the tail off. The chain has to name the first entry that
no longer checks out — and a head hash handed out earlier has to catch the
one edit a chain cannot see by itself, the truncated tail.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import audit
import main

SAMPLES = Path(__file__).resolve().parents[2] / "public" / "sample-data"


@pytest.fixture
def batch():
    bid = f"CHAIN-{uuid.uuid4().hex[:8]}"
    audit.clear_trail(bid)
    yield bid
    audit.clear_trail(bid)


def write(bid, n=5):
    return [audit.log_decision(bid, "test", f"decision {i}") for i in range(n)]


def db():
    conn = audit._get_db()
    assert conn is not None, "these tests tamper with the SQLite tier directly"
    return conn


def row_ids(bid):
    return [r[0] for r in db().execute("SELECT id FROM audit WHERE batch_id = ? ORDER BY id", (bid,))]


class TestTheChain:
    def test_each_entry_points_at_the_one_before(self, batch):
        entries = write(batch)
        assert entries[0]["prev_hash"] == audit.GENESIS
        for before, after in zip(entries, entries[1:]):
            assert after["prev_hash"] == before["hash"]

    def test_an_untouched_trail_verifies(self, batch):
        write(batch)
        v = audit.verify_chain(batch)
        assert v["intact"] and v["verified"] == 5
        assert "none altered" in v["plain"]

    def test_the_hash_covers_the_content(self, batch):
        e = write(batch, 1)[0]
        assert audit.entry_hash(dict(e, detail="decision X")) != e["hash"]


class TestTamperingIsDetected:
    def test_an_edited_entry(self, batch):
        write(batch)
        target = row_ids(batch)[2]
        db().execute("UPDATE audit SET detail = 'approved by nobody' WHERE id = ?", (target,))
        db().commit()
        v = audit.verify_chain(batch)
        assert not v["intact"] and v["broken_at"] == 2
        assert "changed after it was written" in v["reason"]
        assert v["plain"].startswith("TAMPERING DETECTED")

    def test_a_deleted_entry(self, batch):
        write(batch)
        db().execute("DELETE FROM audit WHERE id = ?", (row_ids(batch)[1],))
        db().commit()
        v = audit.verify_chain(batch)
        assert not v["intact"] and v["broken_at"] == 1
        assert "removed, reordered or inserted" in v["reason"]

    def test_a_rewritten_entry_with_a_fresh_hash_still_breaks_the_next_link(self, batch):
        """Recomputing the edited entry's own hash does not help: the next
        entry still points at the old one."""
        write(batch)
        target = row_ids(batch)[2]
        entries = audit.get_audit_trail(batch)
        forged = dict(entries[2], detail="approved by nobody")
        forged["hash"] = audit.entry_hash(forged)
        db().execute("UPDATE audit SET detail = ?, hash = ? WHERE id = ?",
                     (forged["detail"], forged["hash"], target))
        db().commit()
        v = audit.verify_chain(batch)
        assert not v["intact"] and v["broken_at"] == 3

    def test_a_row_slipped_in_without_the_log(self, batch):
        write(batch, 3)
        db().execute("INSERT INTO audit (batch_id, ts, agent, detail) VALUES (?,?,?,?)",
                     (batch, "2026-09-21T00:00:00+00:00", "human_reviewer", "APPROVED"))
        db().commit()
        v = audit.verify_chain(batch)
        assert not v["intact"] and v["broken_at"] == 3

    def test_a_cut_tail_is_caught_by_the_receipt(self, batch):
        entries = write(batch)
        receipt = entries[-1]["hash"]
        db().execute("DELETE FROM audit WHERE id = ?", (row_ids(batch)[-1],))
        db().commit()
        assert audit.verify_chain(batch)["intact"], "a shortened chain is still a chain"
        v = audit.verify_chain(batch, receipt=receipt)
        assert not v["intact"]
        assert "no longer in the trail" in v["reason"]

    def test_a_receipt_still_present_is_confirmed(self, batch):
        entries = write(batch)
        write(batch, 2)
        v = audit.verify_chain(batch, receipt=entries[-1]["hash"])
        assert v["intact"] and v["receipt_position"] == 4


class TestHistoryAndConcurrency:
    def test_entries_from_before_the_chain_are_unchained_not_verified(self, batch):
        for i in range(2):
            db().execute("INSERT INTO audit (batch_id, ts, agent, detail) VALUES (?,?,?,?)",
                         (batch, f"2026-01-0{i + 1}T00:00:00+00:00", "old", f"legacy {i}"))
        db().commit()
        write(batch, 3)
        v = audit.verify_chain(batch)
        assert v["intact"] and v["verified"] == 3
        assert v["unchained_before_chain_began"] == 2
        assert "predate the chain" in v["plain"]

    def test_concurrent_writers_do_not_fork_the_chain(self, batch):
        def burst():
            for i in range(25):
                audit.log_decision(batch, "worker", f"entry {i}")
        threads = [threading.Thread(target=burst) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        v = audit.verify_chain(batch)
        assert v["entries"] == 200 and v["intact"], v["reason"]

    def test_the_file_tier_chains_too(self, batch, monkeypatch):
        monkeypatch.setattr(audit, "_get_db", lambda: None)
        write(batch, 4)
        entries = audit.get_audit_trail(batch)
        assert len(entries) == 4 and entries[1]["prev_hash"] == entries[0]["hash"]
        assert audit.verify_chain(batch)["intact"]


class TestTheEndpointAndTheReceipt:
    @pytest.fixture
    def client(self):
        return TestClient(main.app)

    def test_verify_endpoint(self, client, batch):
        write(batch)
        body = client.get(f"/audit/{batch}/verify").json()
        assert body["intact"] and body["verified"] == 5

    def test_every_reconciliation_hands_out_a_receipt_that_verifies(self, client, batch):
        files = {
            "gateway_file": ("g.csv", (SAMPLES / "gateway_report.csv").read_bytes(), "text/csv"),
            "bank_file": ("b.csv", (SAMPLES / "bank_statement.csv").read_bytes(), "text/csv"),
            "erp_file": ("e.json", (SAMPLES / "erp_ledger.json").read_bytes(), "application/json"),
        }
        r = client.post("/reconcile/upload", data={
            "batch_id": batch, "net_amount": "66466.36",
            "settled_at": "2026-09-02T00:00:00Z", "settlement_window_days": "5",
            "currency": "INR", "declared_deductions": "2055.66",
            "member_source": "gateway"}, files=files)
        head = r.json()["audit_head"]
        assert head and len(head) == 64
        v = client.get(f"/audit/{batch}/verify", params={"receipt": head}).json()
        assert v["intact"], v["plain"]
