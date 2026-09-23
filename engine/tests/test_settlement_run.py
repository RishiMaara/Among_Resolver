"""
settlement_run: what a run does, called without a web request.

The upload and queue handlers only read files and form fields; these pin the
behaviour that moved out of them: a payment already paid out withholds a
clear and teaches nothing, a failing settlement in a queue is contained, and
a bad member feed is named.
"""
from datetime import datetime, timedelta, timezone

import pytest

import settlement_run
from fee_decomposition import FeeRateCard
from orchestrator import reconcile_batch
from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence
from subset_sum import SubsetSumConfig

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _tx(txn_id, amount, ref):
    return NormalizedTxn(source=SourceType.GATEWAY, source_txn_id=txn_id, ref_id_canonical=ref,
                         amount_cents=amount, currency="INR", timestamp_utc=T0,
                         tz_confidence=TzConfidence.HIGH, memo_raw="", memo_normalized="")


POOL = [_tx("SR1", 30000, "SRUN000777"), _tx("SR2", 50000, "SRUN000777"),
        _tx("SR3", 41000, "ORD9")]


def _batch():
    return SettlementBatch(batch_id="SRUN-000777", net_amount_cents=80000, currency="INR",
                           settled_at_utc=T0 + timedelta(days=1),
                           member_source=SourceType.GATEWAY, declared_deductions_cents=0)


@pytest.fixture
def quiet_stores(monkeypatch):
    """Nothing in these tests writes to the shared stores."""
    calls = {"recorded": [], "learned": []}
    monkeypatch.setattr(settlement_run.settled_ledger, "record_settled",
                        lambda *a, **k: calls["recorded"].append(a))
    monkeypatch.setattr(settlement_run.settlement_cycle, "learn_from",
                        lambda *a, **k: calls["learned"].append(a))
    monkeypatch.setattr(settlement_run.open_items, "update_from_run", lambda *a, **k: {})
    monkeypatch.setattr(settlement_run.open_items, "update_from_runs", lambda *a, **k: {})
    monkeypatch.setattr(settlement_run.history, "record_run", lambda *a, **k: None)
    monkeypatch.setattr(settlement_run.history, "list_runs", lambda *a, **k: [])
    return calls


def _finish(report, notes):
    return settlement_run.finish_upload_run(
        _batch(), POOL, report, notes=notes, inputs={}, taken_reversals=[],
        investigate=False, investigate_with_model=False, reviewer=None,
        rate_card_assumed=False, deductions_declared=True)


def test_a_clear_is_recorded_and_teaches_the_cycle(monkeypatch, quiet_stores):
    monkeypatch.setattr(settlement_run.settled_ledger, "check_claims",
                        lambda *a: {"count": 0, "claims": [], "summary": ""})
    report = reconcile_batch(_batch(), POOL, subset_config=SubsetSumConfig(num_search_workers=1))
    out = _finish(report, [])
    assert out["summary"]["cleared"] is True
    assert len(quiet_stores["recorded"]) == 1 and len(quiet_stores["learned"]) == 1


def test_a_payment_paid_out_before_withholds_and_teaches_nothing(monkeypatch, quiet_stores):
    monkeypatch.setattr(settlement_run.settled_ledger, "check_claims",
                        lambda *a: {"count": 1, "claims": [], "summary": "one already paid out"})
    report = reconcile_batch(_batch(), POOL, subset_config=SubsetSumConfig(num_search_workers=1))
    assert report.match_result.cleared
    out = _finish(report, [])
    assert out["summary"]["cleared"] is False
    assert out["summary"]["withheld_reason"] == "already_settled_elsewhere"
    assert quiet_stores["recorded"] == [] and quiet_stores["learned"] == []


def test_a_failing_settlement_in_a_queue_is_contained(monkeypatch, quiet_stores):
    monkeypatch.setattr(settlement_run.settled_ledger, "check_claims",
                        lambda *a: {"count": 0, "claims": [], "summary": ""})
    rows = [
        {"batch_id": "SRUN-000777", "net_amount": "800.00", "settled_at": "2026-09-02",
         "declared_deductions": "0"},
        {"batch_id": "BROKEN-1", "net_amount": "not a number", "settled_at": "2026-09-02"},
    ]
    out = settlement_run.run_queue(rows, POOL, member_source=SourceType.GATEWAY,
                                   merchant_id="", window_days=5,
                                   rate_card=FeeRateCard(gateway_fee_bps=0, flat_fee_cents=0,
                                                         tax_withholding_bps=0))
    status = {r["batch_id"]: r["status"] for r in out["results"]}
    assert status == {"SRUN-000777": "cleared", "BROKEN-1": "error"}
    assert out["tally"]["error"] == 1 and out["needs_review"] == 1


def test_a_member_feed_that_does_not_exist_is_named():
    assert settlement_run.parse_member_source(" Gateway ") is SourceType.GATEWAY
    assert settlement_run.parse_member_source(None) is None
    with pytest.raises(ValueError, match="member_source must be one of"):
        settlement_run.parse_member_source("paypal")
