"""
Chargebacks: file a dispute, get a reversal that a later settlement absorbs.

WHY A NEW ROW AND NOT AN EDIT
-----------------------------
When a processor claws money back, the tempting move is to reopen the
settlement the original payment cleared in and subtract it. That destroys the
thing the settlement is for: a record of what was true on the day it closed.
A reconciliation that can be rewritten after the fact cannot be audited, and
the batch that was signed off is no longer the batch anyone signed.

So a chargeback produces a NEW transaction — a negative one, referencing the
original — which waits here until a settlement takes it in. The processor
deducts the money in some later payout; that payout is the settlement this
reversal belongs to, and the arithmetic ties out there. The closed batch is
left exactly as it was.

Taking reversals in is opt-in per reconciliation (`include_chargebacks` on
the upload form) rather than automatic, because which payout absorbs a
clawback is a fact about the processor's timing, not something this engine
should assume on a reviewer's behalf.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import chargeback_engine

router = APIRouter()


class ChargebackFiling(BaseModel):
    original_txn_id: str
    dispute_amount_cents: int
    reason_code: str
    currency: str = "INR"
    filed_at_utc: str = ""


@router.post("/chargebacks", summary="File a chargeback and emit its reversal")
def file_chargeback(body: ChargebackFiling):
    original = (body.original_txn_id or "").strip()
    if not original:
        raise HTTPException(status_code=422, detail={
            "message": "original_txn_id is required",
            "plain": "A reversal has to name the payment being clawed back.",
        })
    if body.dispute_amount_cents <= 0:
        raise HTTPException(status_code=422, detail={
            "message": "dispute_amount_cents must be positive",
            "plain": ("Send the disputed amount as a positive figure in paise. "
                      "The engine writes the reversal as a negative row itself, "
                      "so a negative here would cancel out into a credit."),
        })

    filed_at = datetime.now(timezone.utc)
    if (body.filed_at_utc or "").strip():
        try:
            filed_at = datetime.fromisoformat(body.filed_at_utc.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail={
                "message": "filed_at_utc must be ISO-8601",
                "plain": f"Could not read {body.filed_at_utc!r} as a date and time.",
            })

    reversal = chargeback_engine.record(chargeback_engine.ChargebackNotice(
        original_txn_id=original,
        dispute_amount_cents=int(body.dispute_amount_cents),
        currency=(body.currency or "INR").upper(),
        reason_code=(body.reason_code or "").strip() or "unspecified",
        filed_at_utc=filed_at,
    ))
    return {
        "reversal_txn_id": reversal.source_txn_id,
        "original_txn_id": original,
        "amount_cents": reversal.amount_cents,
        "currency": reversal.currency,
        "pending_count": len(chargeback_engine.pending()),
        "note": ("Held until a settlement takes it in. Reconcile with "
                 "include_chargebacks=true on the batch where the processor "
                 "deducted the money. The original settlement is unchanged."),
    }


@router.get("/chargebacks/pending", summary="Reversals awaiting a settlement")
def pending_chargebacks():
    rows = chargeback_engine.pending()
    return {
        "count": len(rows),
        "total_cents": sum(t.amount_cents for t in rows),
        "pending": [
            {
                "reversal_txn_id": t.source_txn_id,
                "original_txn_id": (t.extra or {}).get("original_txn_id"),
                "amount_cents": t.amount_cents,
                "currency": t.currency,
                "reason_code": (t.extra or {}).get("reason_code"),
                "filed_at_utc": (t.extra or {}).get("filed_at_utc"),
            }
            for t in rows
        ],
    }
