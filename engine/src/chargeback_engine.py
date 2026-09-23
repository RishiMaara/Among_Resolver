from datetime import datetime, timezone
import json
import uuid
import logging
from typing import Optional
from dataclasses import dataclass

from schema import NormalizedTxn, SourceType, ComplianceStatus, TzConfidence
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
        # When the dispute was filed, not when this API happened to be called.
        # The settlement window decides which payout can absorb a reversal,
        # so an artifact of request timing here would silently decide it.
        timestamp_utc=(notice.filed_at_utc if notice.filed_at_utc.tzinfo
                       else notice.filed_at_utc.replace(tzinfo=timezone.utc)),
        tz_confidence=TzConfidence.HIGH,
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
        batch_id=f"chargeback:{reversal_id}", 
        agent="chargeback_engine",
        detail=f"Emitted reversal {reversal_id} for {notice.original_txn_id} (-{notice.dispute_amount_cents} {notice.currency})"
    )
    
    logger.info("Processed chargeback %s for original txn %s", reversal_id, notice.original_txn_id)
    return reversal_txn



# ── where reversals wait until a settlement takes them in ─────────────────
#
# A reversal is useless if it only exists in the response to the notice that
# created it. It has to survive until the next batch is reconciled, and on a
# serverless host that next request lands on a different instance — so the
# store is the shared one the audit trail and webhook queue already use, with
# per-process memory as the fallback when no REDIS_URL is configured.

_REDIS_PENDING = "chargebacks:pending"
_pending: "dict[str, NormalizedTxn]" = {}


def _shared_store():
    if not audit.redis_url():
        return None
    return audit._get_redis()


def _to_record(t: NormalizedTxn) -> str:
    return json.dumps({
        "source_txn_id": t.source_txn_id,
        "ref_id_canonical": t.ref_id_canonical,
        "amount_cents": t.amount_cents,
        "currency": t.currency,
        "timestamp_utc": t.timestamp_utc.isoformat(),
        "memo_raw": t.memo_raw,
        "memo_normalized": t.memo_normalized,
        "extra": t.extra,
    })


def _from_record(raw: str) -> NormalizedTxn:
    r = json.loads(raw)
    return NormalizedTxn(
        source=SourceType.GATEWAY,
        source_txn_id=r["source_txn_id"],
        ref_id_canonical=r["ref_id_canonical"],
        amount_cents=int(r["amount_cents"]),
        currency=r["currency"],
        timestamp_utc=datetime.fromisoformat(r["timestamp_utc"]),
        tz_confidence=TzConfidence.HIGH,
        memo_raw=r.get("memo_raw", ""),
        memo_normalized=r.get("memo_normalized", ""),
        compliance_status=ComplianceStatus.PASS,
        extra=r.get("extra") or {},
    )


def record(notice: ChargebackNotice) -> NormalizedTxn:
    """Process a notice and hold the reversal until a settlement takes it in."""
    reversal = process_chargeback(notice)
    shared = _shared_store()
    if shared is not None:
        try:
            shared.hset(_REDIS_PENDING, reversal.source_txn_id, _to_record(reversal))
            return reversal
        except Exception as exc:
            logger.warning("Chargeback: shared store failed (%s); holding locally.",
                           type(exc).__name__)
    _pending[reversal.source_txn_id] = reversal
    return reversal


def pending() -> list[NormalizedTxn]:
    """Reversals not yet taken into a settlement, oldest first."""
    shared = _shared_store()
    if shared is not None:
        try:
            found = [_from_record(v) for v in shared.hgetall(_REDIS_PENDING).values()]
            return sorted(found, key=lambda t: t.timestamp_utc)
        except Exception as exc:
            logger.warning("Chargeback: shared read failed (%s); showing local.",
                           type(exc).__name__)
    return sorted(_pending.values(), key=lambda t: t.timestamp_utc)


def mark_taken(reversal_ids: list[str], batch_id: str) -> int:
    """
    Record that a settlement took these reversals in, and stop offering them.

    Called only once a batch has actually been reconciled with them in the
    pool — a reversal dropped from pending before that would be lost from
    every future batch, which is the wrong direction for money owed back.
    """
    taken = 0
    shared = _shared_store()
    for rid in reversal_ids:
        if shared is not None:
            try:
                if shared.hdel(_REDIS_PENDING, rid) is not None:
                    taken += 1
                    continue
            except Exception as exc:
                logger.debug("mark_taken: best-effort step skipped (%s: %s)", type(exc).__name__, exc)
        if _pending.pop(rid, None) is not None:
            taken += 1
    if taken:
        audit.log_decision(
            batch_id=batch_id, agent="chargeback_engine",
            detail=(f"{taken} chargeback reversal(s) taken into this settlement's "
                    f"pool: {', '.join(reversal_ids[:10])}"),
        )
    return taken


def _reset_for_tests() -> None:
    _pending.clear()
