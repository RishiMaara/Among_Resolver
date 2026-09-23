"""
Foreign-currency payments in a settlement, through a DECLARED rate only.

Without a rate a USD leg is never summed into an INR settlement
(test_real_data_hazards.TestCurrencyIsPartOfTheComparison). With the rate the
settlement advice states, it converts exactly, the set clears on the
arithmetic, and the rate and the original amount stay on the record.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import fx
import main
from orchestrator import reconcile_batch
from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence
from subset_sum import SubsetSumConfig

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _tx(txn_id, ref, amount_minor, currency, extra=None):
    return NormalizedTxn(source=SourceType.GATEWAY, source_txn_id=txn_id, ref_id_canonical=ref,
                         amount_cents=amount_minor, currency=currency, timestamp_utc=T0,
                         tz_confidence=TzConfidence.HIGH, memo_raw="", memo_normalized="",
                         extra=extra or {})


def _pool():
    # 100.00 INR + 100.00 INR + 1.20 USD; at 83.25 the USD leg is 99.90 INR.
    return [_tx("inr1", "FXSTL42LEG1", 100_00, "INR"), _tx("inr2", "FXSTL42LEG2", 100_00, "INR"),
            _tx("usd1", "FXSTL42LEG3", 1_20, "USD", {"fee_amount_cents": 3})]


def _batch(rates=None):
    return SettlementBatch(batch_id="FXSTL42", net_amount_cents=299_90, currency="INR",
                           settled_at_utc=T0 + timedelta(days=1), declared_deductions_cents=0,
                           member_source=SourceType.GATEWAY, fx_rates=rates or {})


def test_a_declared_rate_lets_the_foreign_leg_in_and_the_set_clears():
    report = reconcile_batch(_batch({"USD": Decimal("83.25")}), _pool(),
                             subset_config=SubsetSumConfig(num_search_workers=1))
    assert report.match_result.cleared
    assert sorted(report.match_result.matched_txn_ids) == ["inr1", "inr2", "usd1"]
    assert report.ties_out


def test_without_a_rate_the_foreign_leg_stays_out():
    report = reconcile_batch(_batch(), _pool(), subset_config=SubsetSumConfig(num_search_workers=1))
    assert "usd1" not in report.match_result.matched_txn_ids
    assert not report.match_result.cleared


def test_conversion_is_exact_keeps_the_evidence_and_sets_foreign_fees_aside():
    t = fx.convert(_tx("usd1", "R", 1_20, "USD", {"fee_amount_cents": 3}), "INR", Decimal("83.25"))
    assert (t.amount_cents, t.currency) == (99_90, "INR")
    assert t.extra["fx_rate"] == "83.25" and t.extra["fx_original_amount_minor"] == 1_20
    assert "fee_amount_cents" not in t.extra
    assert t.extra["fx_foreign_fee_fields"] == {"fee_amount_cents": 3}


def test_minor_units_follow_the_currency():
    # 1,000 JPY has no minor unit; at 0.5612 it is 561.20 INR.
    assert fx.convert(_tx("j", "R", 1000, "JPY"), "INR", Decimal("0.5612")).amount_cents == 561_20
    # 1.000 KWD has three; at 271.5 it is 271.50 INR.
    assert fx.convert(_tx("k", "R", 1000, "KWD"), "INR", Decimal("271.5")).amount_cents == 271_50


@pytest.mark.parametrize("text", ["USD", "USD=abc", "US=83", "USD=-1", "USD=0"])
def test_an_unreadable_rate_is_named(text):
    with pytest.raises(ValueError, match="fx_rates"):
        fx.parse_rates(text)


def test_the_upload_form_refuses_an_unreadable_rate():
    r = TestClient(main.app).post("/reconcile/upload", data={
        "batch_id": "FX-1", "net_amount": "1", "settled_at": "2026-09-02", "fx_rates": "USD=abc",
    }, files={"gateway_file": ("g.csv", b"txn_id,amount,timestamp\nA,1.00,2026-09-01\n")})
    assert r.status_code == 422 and "fx_rates" in r.json()["detail"]
