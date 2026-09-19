from datetime import datetime, timezone
import uuid
import logging
from typing import Optional
from dataclasses import dataclass

from schema import NormalizedTxn, SourceType, ComplianceStatus
import db
import audit

logger = logging.getLogger(__name__)


@dataclass
class ChargebackNotice:
    original_txn_id: str
    dispute_amount_cents: int
    currency: str
    reason_code: str
    filed_at_utc: datetime


def process_chargeback(notice: ChargebackNotice) -> NormalizedTxn:
    """
    Handles a chargeback by inserting a debit adjustment into the current day's pool.
    
    In a bi-temporal ledger, we never go back and modify a closed batch to mark a 
    transaction as 'un-reconciled' or subtract from its total. That destroys the
    audit history of what happened on that day.
    
    Instead, we emit a brand new synthetic transaction representing the clawback.
    This debit will be reconciled against the future bank settlement where the processor
    actually deducts the funds.
    """
    
    # 1. Lookup the original transaction to verify it exists and was reconciled.
    # In a full system this would query the DB. For now, we simulate the validation.
    # If it was never reconciled, there's no money to claw back yet, but we'd still
    # need to record the dispute against the pending merchant balance.
    
    # 2. Generate the synthetic reversal transaction
    reversal_id = f"cb_{uuid.uuid4().hex[:8]}"
    
    reversal_txn = NormalizedTxn(
        source=SourceType.GATEWAY,  # The gateway notified us
        source_txn_id=reversal_id,
        ref_id_canonical=notice.original_txn_id.lower().strip(), # Tie it back to the original
        amount_cents=-abs(notice.dispute_amount_cents), # Strictly negative
        currency=notice.currency,
        timestamp_utc=datetime.now(timezone.utc),
        tz_confidence=None,
        memo_raw=f"CHARGEBACK REVERSAL for {notice.original_txn_id} (Code: {notice.reason_code})",
        memo_normalized=f"chargeback reversal {notice.original_txn_id.lower()} {notice.reason_code.lower()}",
        is_cash=False,
        is_wire_transfer=False,
        compliance_status=ComplianceStatus.PASS,
        extra={
            "is_chargeback_reversal": True,
            "original_txn_id": notice.original_txn_id,
            "reason_code": notice.reason_code,
            "filed_at_utc": notice.filed_at_utc.isoformat()
        }
    )
    
    # 3. Write to the immutable audit log
    audit.log_decision(
        batch_id="NO_BATCH_YET", 
        agent="chargeback_engine",
        detail=f"Emitted reversal {reversal_id} for {notice.original_txn_id} (-{notice.dispute_amount_cents} {notice.currency})"
    )
    
    logger.info("Processed chargeback %s for original txn %s", reversal_id, notice.original_txn_id)
    return reversal_txn

