"""
Exceptions come out ranked by the rupees waiting on them, largest first.

An exception list is a work queue: the screen shows ten, history keeps five
hundred, Q&A grounds on a few. Whatever sits at the top gets reviewed, so the
order is a decision about which money a human looks at first.
"""

import os
import sys
from types import SimpleNamespace as T

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import exception_ranking


def txn(tid, cents):
    return T(source_txn_id=tid, amount_cents=cents)


POOL = [txn("A", 400_000_00), txn("B", 40_00), txn("C", 2_500_00),
        txn("R", -1_000_00)]


def exc(reason, *ids):
    return {"reason": reason, "candidate_txn_ids": list(ids), "diagnosis_note": ""}


def test_the_largest_amount_at_stake_comes_first():
    ranked, _ = exception_ranking.rank(
        [exc("unmatched", "B"), exc("unmatched", "A"), exc("unmatched", "C")], POOL)
    assert [e["candidate_txn_ids"] for e in ranked] == [["A"], ["C"], ["B"]]
    assert [e["rank"] for e in ranked] == [1, 2, 3]
    assert ranked[0]["amount_at_stake_cents"] == 400_000_00


def test_a_refund_is_money_at_stake_too():
    ranked, _ = exception_ranking.rank([exc("unmatched", "R")], POOL)
    assert ranked[0]["amount_at_stake_cents"] == 1_000_00


def test_one_payment_in_two_exceptions_is_counted_once_in_the_total():
    ranked, summary = exception_ranking.rank(
        [exc("unmatched", "A", "B"), exc("ambiguous", "A")], POOL)
    assert ranked[0]["amount_at_stake_cents"] == 400_040_00      # A + B
    assert ranked[1]["amount_at_stake_cents"] == 400_000_00      # A again, honestly
    assert summary["total_at_stake_cents"] == 400_040_00, (
        "the batch total must not count payment A twice"
    )


def test_an_id_shared_by_two_feeds_never_doubles_a_figure():
    pool = [txn("1001", 5_000_00), txn("1001", 70_00)]   # gateway vs unrelated ERP line
    ranked, summary = exception_ranking.rank([exc("unmatched", "1001")], pool)
    assert ranked[0]["amount_at_stake_cents"] == 5_000_00
    assert summary["total_at_stake_cents"] == 5_000_00


def test_an_unpriceable_exception_is_kept_and_labelled_not_zeroed():
    ranked, summary = exception_ranking.rank(
        [exc("unmatched", "GHOST"), exc("unmatched", "B")], POOL)
    ghost = next(e for e in ranked if e["candidate_txn_ids"] == ["GHOST"])
    assert ghost["amount_known"] is False
    assert summary["unpriced_count"] == 1
    assert summary["count"] == 2


def test_a_legal_block_outranks_an_arithmetic_one_of_equal_value():
    ranked, _ = exception_ranking.rank(
        [exc("unmatched", "C"), exc("compliance_block", "C")], POOL)
    assert ranked[0]["reason"] == "compliance_block"


def test_the_summary_says_what_share_of_the_settlement_is_waiting():
    _, summary = exception_ranking.rank([exc("unmatched", "C")], POOL, target_cents=10_000_00)
    assert summary["share_of_target"] == 0.25


def test_the_callers_list_is_not_mutated():
    original = [exc("unmatched", "B")]
    exception_ranking.rank(original, POOL)
    assert "rank" not in original[0]


def test_a_real_reconciliation_returns_its_exceptions_ranked():
    """End to end on the demo's own 'withholds' sample."""
    from pathlib import Path
    from fastapi.testclient import TestClient
    import main

    samples = Path(__file__).resolve().parents[2] / "public" / "sample-data"
    files = {
        "gateway_file": ("gateway_report.csv", (samples / "gateway_report.csv").read_bytes(), "text/csv"),
        "bank_file": ("bank_statement.csv", (samples / "bank_statement.csv").read_bytes(), "text/csv"),
        "erp_file": ("erp_ledger.json", (samples / "erp_ledger.json").read_bytes(), "application/json"),
    }
    r = TestClient(main.app).post("/reconcile/upload", files=files, data={
        "batch_id": "SETTLE-001", "net_amount": "66466.36",
        "settled_at": "2026-09-02T00:00:00Z", "settlement_window_days": "5",
        "currency": "INR", "declared_deductions": "2055.66"})
    body = r.json()
    exceptions = body["exceptions"]
    assert exceptions, "the withholds sample is expected to raise exceptions"
    amounts = [e["amount_at_stake_cents"] for e in exceptions]
    assert amounts == sorted(amounts, reverse=True)
    assert [e["rank"] for e in exceptions] == list(range(1, len(exceptions) + 1))
    summary = body["exceptions_summary"]
    assert summary["count"] == len(exceptions)
    assert summary["total_at_stake_cents"] > 0
