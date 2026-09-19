import pytest
from datetime import datetime, timezone
from chargeback_engine import process_chargeback, ChargebackNotice
from schema import SourceType


def test_process_chargeback_emits_negative_synthetic_txn():
    notice = ChargebackNotice(
        original_txn_id="TXN_12345",
        dispute_amount_cents=15000, # $150.00
        currency="INR",
        reason_code="FRAUD",
        filed_at_utc=datetime.now(timezone.utc)
    )
    
    reversal = process_chargeback(notice)
    
    assert reversal.source == SourceType.GATEWAY
    assert reversal.amount_cents == -15000
    assert reversal.ref_id_canonical == "txn_12345"
    assert "is_chargeback_reversal" in reversal.extra
    assert reversal.extra["is_chargeback_reversal"] is True
    assert reversal.extra["original_txn_id"] == "TXN_12345"
    assert "FRAUD" in reversal.memo_raw


def test_process_chargeback_handles_already_negative_input():
    # If the gateway passes a negative amount, the engine should still ensure it's negative,
    # not double-negate it into a positive.
    notice = ChargebackNotice(
        original_txn_id="TXN_999",
        dispute_amount_cents=-5000, 
        currency="INR",
        reason_code="PRODUCT_UNACCEPTABLE",
        filed_at_utc=datetime.now(timezone.utc)
    )
    
    reversal = process_chargeback(notice)
    assert reversal.amount_cents == -5000
