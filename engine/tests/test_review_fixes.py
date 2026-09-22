"""
What the architecture review found, held in place.

Each test is one finding: a result resolved to its records by bare id, a
timed-out uniqueness check reported as proof, a batch cleared while some of
its payments were already paid out, a public endpoint that spends compute
for anyone, and a reviewer name that could forge a separation-of-duties
marker. FAILURE_LOG 38-40.
"""
from datetime import datetime, timedelta, timezone

import pytest

import four_eyes
import subset_sum
from api import presentation
from orchestrator import reconcile_batch
from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence
from subset_sum import SubsetSumConfig, UNPROVEN_UNIQUE_CONFIDENCE, match_batch

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _tx(source, txn_id, amount, ref):
    return NormalizedTxn(source=source, source_txn_id=txn_id, ref_id_canonical=ref,
                         amount_cents=amount, currency="INR", timestamp_utc=T0,
                         tz_confidence=TzConfidence.HIGH, memo_raw="", memo_normalized="")


def _collision_pool():
    # Gateway 1001 and 1002 name the settlement; ERP 1001 is an unrelated
    # line that happens to carry the same number.
    return [
        _tx(SourceType.GATEWAY, "1001", 30000, "SETTLE000123"),
        _tx(SourceType.GATEWAY, "1002", 50000, "SETTLE000123"),
        _tx(SourceType.GATEWAY, "1003", 41000, "ORD77"),
        _tx(SourceType.ERP, "1001", 7777, "INV9"),
    ]


def _batch():
    return SettlementBatch(batch_id="SETTLE-000123", net_amount_cents=80000, currency="INR",
                           settled_at_utc=T0 + timedelta(days=1),
                           member_source=SourceType.GATEWAY, declared_deductions_cents=0)


def test_another_feeds_record_with_the_same_id_stays_out_of_the_tie_out():
    report = reconcile_batch(_batch(), _collision_pool(),
                             subset_config=SubsetSumConfig(num_search_workers=1))
    assert report.match_result.cleared
    assert sorted(report.match_result.matched_keys) == ["gateway:1001", "gateway:1002"]
    # It read 87,777 and "does not tie" by the ERP line's 77.77.
    assert report.matched_gross_cents == 80000
    assert report.ties_out


def test_the_matched_rows_a_reviewer_sees_are_the_matched_feeds_only():
    pool = _collision_pool()
    report = reconcile_batch(_batch(), pool, subset_config=SubsetSumConfig(num_search_workers=1))
    rows = presentation.matched_rows(report.match_result, pool)
    assert sorted((r["source"], r["txn_id"]) for r in rows) == [
        ("gateway", "1001"), ("gateway", "1002")]


def test_a_probe_that_ran_out_of_time_is_recorded_as_such(monkeypatch):
    def out_of_time(*args, status_out=None, **kwargs):
        status_out.append("UNKNOWN")
        return None

    monkeypatch.setattr(subset_sum, "_solve_cpsat", out_of_time)
    outcome: dict = {}
    found = subset_sum._probe_for_alternate_subset(
        [], [], 100, 0, 0.1, 3, outcome=outcome)
    assert found is None and outcome.get("timed_out")


def test_a_probe_that_proved_there_is_no_alternate_is_not_a_timeout(monkeypatch):
    def proven_none(*args, status_out=None, **kwargs):
        status_out.append("INFEASIBLE")
        return None

    monkeypatch.setattr(subset_sum, "_solve_cpsat", proven_none)
    outcome: dict = {}
    subset_sum._probe_for_alternate_subset([], [], 100, 0, 0.1, 3, outcome=outcome)
    assert not outcome.get("timed_out")


def test_a_timed_out_uniqueness_check_is_not_reported_as_certainty(monkeypatch):
    def timed_out(*args, outcome=None, **kwargs):
        outcome["timed_out"] = True
        return None

    monkeypatch.setattr(subset_sum, "_probe_for_alternate_subset", timed_out)
    pool = [_tx(SourceType.GATEWAY, "A1", 1000, ""), _tx(SourceType.GATEWAY, "A2", 2500, "")]
    result = match_batch("B1", pool, 3500, SubsetSumConfig(num_search_workers=1))
    assert result.cleared
    assert result.confidence == UNPROVEN_UNIQUE_CONFIDENCE
    assert "Uniqueness not established" in result.reasoning


def test_one_payment_already_paid_out_withholds_the_clear(monkeypatch):
    recorded = []
    monkeypatch.setattr(presentation.settled_ledger, "check_claims",
                        lambda batch_id, ids: {"count": 1, "claims": [], "summary": "x"})
    monkeypatch.setattr(presentation.settled_ledger, "record_settled",
                        lambda *a, **k: recorded.append(a))
    summary = {"cleared": True, "ambiguous": False}
    presentation._check_then_record("B2", summary, [f"gateway:{i}" for i in range(10)])
    assert summary["cleared"] is False
    assert summary["withheld_reason"] == "already_settled_elsewhere"
    assert recorded == []


def test_the_compute_heavy_demo_is_off_on_a_public_deployment(monkeypatch):
    from fastapi.testclient import TestClient
    import main

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("ENABLE_DEMO_ENDPOINT", raising=False)
    assert TestClient(main.app).get("/demo").status_code == 404


@pytest.mark.parametrize("name", ["bob] [sod actor=alice|act=batch_decision",
                                  "eve|act=fifo_acceptance"])
def test_a_reviewer_name_cannot_forge_a_marker(name):
    line = "Decision recorded." + four_eyes.marker(name, four_eyes.ACT_JOURNAL_APPROVAL)
    found = list(four_eyes._MARKER_RE.finditer(line))
    assert len(found) == 1
    assert found[0].group("act") == four_eyes.ACT_JOURNAL_APPROVAL
