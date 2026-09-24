"""
Notes about foreign payments: advisory, offline in tests, never deciding.

A foreign payment with no declared rate is left out (fx.py); the note says so
and what to add. A declared rate far from the ECB reference for the day is
named, since a typo there stops the foreign payments tying. No network means
no reference note, never a failed run.
"""
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from fastapi.testclient import TestClient

import fx_reference
import main
from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence

DAY = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def _batch(rates=None):
    return SettlementBatch(batch_id="FXN1", net_amount_cents=1_000_00, currency="INR",
                           settled_at_utc=DAY, fx_rates=rates or {})


def _t(cur):
    return NormalizedTxn(source=SourceType.GATEWAY, source_txn_id=f"p{cur}", ref_id_canonical="R",
                         amount_cents=100, currency=cur, timestamp_utc=DAY, tz_confidence=TzConfidence.HIGH)


def test_a_foreign_payment_without_a_rate_is_named_and_the_fix_given():
    notes = fx_reference.notes_for(_batch(), [_t("INR"), _t("USD"), _t("USD")])
    assert len(notes) == 1 and "2 USD payment(s) were left out" in notes[0] and "USD=" in notes[0]


def test_a_declared_rate_far_from_the_reference_is_named(monkeypatch):
    monkeypatch.setenv("FX_REFERENCE", "1")
    fx_reference._ecb_per_eur.cache_clear()
    monkeypatch.setattr(fx_reference, "_ecb_per_eur",
                        lambda codes, day: {"INR": Decimal("110.714"), "USD": Decimal("1.1616")})
    notes = fx_reference.notes_for(_batch({"USD": Decimal("9.531")}), [_t("USD")])
    assert len(notes) == 1 and "below the ECB reference of 95.3116" in notes[0]
    assert fx_reference.notes_for(_batch({"USD": Decimal("95.10")}), [_t("USD")]) == []


def test_no_network_means_no_reference_note_and_no_failure(monkeypatch):
    monkeypatch.setenv("FX_REFERENCE", "1")
    fx_reference._ecb_per_eur.cache_clear()

    def down(*a, **k):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(fx_reference.httpx, "get", down)
    assert fx_reference.notes_for(_batch({"USD": Decimal("9.5")}), [_t("USD")]) == []


def test_the_upload_reports_a_foreign_payment_left_out():
    body = ("txn_id,ref_id,amount,currency,timestamp,status\n"
            "fxn0,FXN77-1,100.00,INR,2026-09-16T08:00:00Z,captured\n"
            "fxn1,FXN77-2,2.00,USD,2026-09-16T08:01:00Z,captured\n")
    j = TestClient(main.app).post("/reconcile/upload", data={
        "batch_id": "FXN77", "net_amount": "100.00", "settled_at": "2026-09-17T11:30:00Z",
        "declared_deductions": "0"}, files={"gateway_file": ("g.csv", body.encode())}).json()
    assert any("1 USD payment(s) were left out" in n for n in j["ingestion_notes"])
