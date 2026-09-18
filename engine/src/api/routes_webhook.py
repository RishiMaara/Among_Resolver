"""
The webhook endpoint, and the queue of what it has verified.

Thin on purpose: verification, replay suppression and payload handling all
live in webhook.py, where they can be tested without an HTTP client. This
file decides status codes and reads the body safely.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import webhook

router = APIRouter()


async def read_body_capped(request: Request, limit: int = webhook.WEBHOOK_MAX_BYTES) -> bytes:
    """
    Buffer the raw body, refusing anything over `limit` before paying for it.

    Streamed rather than `await request.body()` for the same reason
    main.read_upload_capped exists: reading it all and then checking the
    length detects the problem after the memory is already spent.

    The RAW bytes matter here beyond size. The HMAC was computed over exactly
    what was sent, so anything that re-serialises the JSON before verifying
    changes the bytes and rejects every genuine delivery.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail={
                    "message": f"Webhook body exceeds {limit} bytes.",
                    "plain": "That delivery is far larger than any settlement "
                             "event, so it was refused before being buffered.",
                },
            )
        chunks.append(chunk)
    return b"".join(chunks)


# Status -> HTTP. A duplicate or an uninteresting event returns 200: both are
# normal, and a non-2xx would make Razorpay retry something that needs no
# retry. Only a real failure to authenticate is an error.
_STATUS_CODES = {
    "unconfigured": 503,
    "invalid_signature": 401,
    "malformed": 400,
    "duplicate": 200,
    "ignored": 200,
    "accepted": 200,
}


@router.post("/webhooks/razorpay", summary="Receive a signed Razorpay settlement event")
async def razorpay_webhook(request: Request):
    """
    Push-based settlement notification.

    Settlements are pushed here; the transactions they decompose into are
    still pulled from the gateway/bank/ERP feeds, so an accepted delivery
    queues a settlement for reconciliation rather than reconciling it. See
    webhook.py's module docstring for why that boundary is where it is.
    """
    body = await read_body_capped(request)
    signature = request.headers.get(webhook.SIGNATURE_HEADER, "")
    result = webhook.handle(body, signature)
    return JSONResponse(status_code=_STATUS_CODES[result["status"]], content=result)


@router.get("/webhooks/pending", summary="Verified settlement notifications awaiting reconciliation")
def pending_deliveries():
    return {
        "configured": webhook.is_configured(),
        "pending": [
            {
                "settlement_id": d.settlement_id,
                "event": d.event,
                "amount_cents": d.amount_cents,
                "currency": d.currency,
                "received_utc": d.received_utc.isoformat(),
            }
            for d in webhook.pending()
        ],
        "note": (
            "Verified settlement notifications. Reconciling one still needs "
            "its transaction feed, which is pulled — POST the settlement to "
            "/reconcile/upload or /reconcile/queue with the feed."
            if webhook.is_configured() else
            "RAZORPAY_WEBHOOK_SECRET is not set, so the endpoint refuses every "
            "delivery with 503 rather than accepting unsigned ones."
        ),
    }
