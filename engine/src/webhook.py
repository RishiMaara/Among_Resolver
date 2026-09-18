"""
Push-based settlement notification, with the signature actually checked.

WHAT THIS CLOSES, AND WHAT IT DOES NOT
--------------------------------------
Ingest was polling only: `razorpay_source.fetch_settlements` asks
`/v1/settlements` on a schedule. The docs claimed a webhook once, the code
had none, and the claim was withdrawn rather than faked. This is the claim
made true — but only the part that is true, so read the boundary carefully.

A verified delivery here means: Razorpay says settlement X has processed, and
the HMAC proves the message is from whoever holds the shared secret. That is
a NOTIFICATION. It is not a reconciliation, because the payments the
settlement decomposes into do not arrive in the webhook payload — they come
from the gateway/bank/ERP feeds, which are still pulled. So the honest
description of the pipeline after this change is: settlements are pushed,
transactions are pulled.

What that buys is real: the lag between a settlement landing and the engine
knowing about it drops from the poll interval to the delivery latency, and
the engine stops asking an API for things that have not happened.

WHY THE SIGNATURE IS THE WHOLE FEATURE
--------------------------------------
An endpoint that accepts unsigned settlement notifications is strictly worse
than polling. Polling at least talks to an authenticated API; an open webhook
lets anyone who finds the URL assert that a settlement of any amount has
processed. Every refusal below exists for that reason:

  * no secret configured        503, not 200. A deployment that forgot the
                                secret must fail loudly, not quietly accept
                                unauthenticated instructions about money.
  * bad or missing signature    401, and the body is never parsed. Nothing
                                reads an unverified payload, because parsing
                                is already acting on it.
  * replayed event id           accepted-and-ignored. A redelivery is normal
                                (Razorpay retries), but processing one twice
                                would record the same settlement twice.
  * oversized body              413 before buffering, same reasoning as
                                main.read_upload_capped.

`hmac.compare_digest`, not `==`. String comparison short-circuits on the
first differing byte, which leaks how much of a guess was right through
timing. That is a textbook finding in a security review and free to avoid.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import audit

logger = logging.getLogger(__name__)

# Razorpay signs webhook bodies with a secret set per endpoint in the
# dashboard. It is NOT the API key secret — a deployment that reuses
# RAZORPAY_KEY_SECRET here will reject every genuine delivery.
WEBHOOK_SECRET_ENV = "RAZORPAY_WEBHOOK_SECRET"
SIGNATURE_HEADER = "x-razorpay-signature"

# Bounded like every other inbound read in this engine. Settlement events are
# a few KB; a megabyte is four orders of magnitude of headroom and still far
# too small to be a memory problem.
WEBHOOK_MAX_BYTES = 1024 * 1024

# How many event ids to remember for replay suppression. Razorpay retries a
# failed delivery for up to 24 hours, so this needs to outlive a retry storm,
# not a week. An OrderedDict gives eviction in insertion order for free.
#
# In-memory ON PURPOSE, and it is a real limitation rather than an oversight:
# a restart forgets, and two processes behind a load balancer do not share it.
# Production wants the settled-ledger table or Redis for this. Said here
# instead of implied, because "replay protected" with a per-process dict is a
# claim that quietly stops being true the moment you scale out.
REPLAY_MEMORY = 4096

_seen: "OrderedDict[str, str]" = OrderedDict()

# Events worth acting on. Razorpay sends many more; anything else is
# acknowledged so it stops being retried, and ignored.
SETTLEMENT_EVENTS = ("settlement.processed",)


@dataclass
class Delivery:
    """One verified notification, kept for the reconciliation path to claim."""
    event_id: str
    event: str
    settlement_id: str
    amount_cents: int
    currency: str
    received_utc: datetime
    payload: dict = field(default_factory=dict)
    reconciled: bool = False


_pending: "OrderedDict[str, Delivery]" = OrderedDict()


def secret() -> str:
    return os.environ.get(WEBHOOK_SECRET_ENV, "").strip()


def is_configured() -> bool:
    return bool(secret())


def expected_signature(body: bytes, key: str) -> str:
    return hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()


def signature_matches(body: bytes, provided: str, key: str) -> bool:
    """
    Constant-time comparison of the HMAC over the RAW body.

    Raw, not re-serialised JSON: `json.dumps(json.loads(body))` is not
    byte-identical to what was signed — key order, separators and unicode
    escaping all differ — so verifying a round-tripped body rejects every
    genuine delivery. The bytes that arrived are the bytes that were signed.
    """
    if not provided or not key:
        return False
    return hmac.compare_digest(expected_signature(body, key), provided.strip())


def _remember(event_id: str) -> bool:
    """True if this is the first time we have seen `event_id`."""
    if event_id in _seen:
        return False
    _seen[event_id] = ""
    while len(_seen) > REPLAY_MEMORY:
        _seen.popitem(last=False)
    return True


def _settlement_from(payload: dict) -> dict:
    """
    Razorpay nests the entity under payload.<entity>.entity.

    Tolerant of the flatter shape too, because a test fixture written by hand
    and a real delivery should not need different code paths to be accepted.
    """
    entity = (
        payload.get("payload", {}).get("settlement", {}).get("entity")
        or payload.get("settlement")
        or payload.get("entity")
        or {}
    )
    return entity if isinstance(entity, dict) else {}


def handle(body: bytes, provided_signature: str) -> dict:
    """
    Verify, de-duplicate, and record one delivery.

    Returns a disposition dict rather than raising, so the route layer decides
    the HTTP status and this stays testable without a client. `status` is one
    of: unconfigured, invalid_signature, malformed, duplicate, ignored,
    accepted.
    """
    key = secret()
    if not key:
        return {"status": "unconfigured", "detail": (
            f"{WEBHOOK_SECRET_ENV} is not set, so no delivery can be "
            f"authenticated. Refusing rather than accepting unsigned "
            f"instructions about settled money."
        )}

    if not signature_matches(body, provided_signature, key):
        # Deliberately not logging the provided signature or the body: one is
        # an attacker-supplied guess and the other is unverified content.
        logger.warning("Webhook rejected: signature did not verify (%d bytes).", len(body))
        return {"status": "invalid_signature", "detail": (
            "Signature did not verify. The body was not parsed."
        )}

    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("top-level JSON is not an object")
    except (ValueError, UnicodeDecodeError) as exc:
        return {"status": "malformed", "detail": f"Verified but unparseable: {exc}"}

    event = str(payload.get("event", "")).strip()
    # Razorpay's own id when present; otherwise the signature, which is
    # already a content hash and therefore a sound dedupe key.
    event_id = str(payload.get("id") or "").strip() or provided_signature.strip()

    if not _remember(event_id):
        return {"status": "duplicate", "event": event, "event_id": event_id,
                "detail": "Already processed; redelivery acknowledged and ignored."}

    if event not in SETTLEMENT_EVENTS:
        audit.log_decision(
            batch_id=f"webhook:{event_id[:16]}", agent="webhook",
            detail=f"Verified delivery of '{event or '(no event field)'}' — not a "
                   f"settlement event, acknowledged and ignored.",
        )
        return {"status": "ignored", "event": event, "event_id": event_id,
                "detail": "Verified, but not an event this engine acts on."}

    entity = _settlement_from(payload)
    settlement_id = str(entity.get("id") or "").strip()
    if not settlement_id:
        return {"status": "malformed", "event": event, "event_id": event_id,
                "detail": "Settlement event carried no settlement id."}

    # Razorpay amounts are already integer paise, which is the unit this
    # engine works in throughout. No float ever touches the value.
    raw_amount = entity.get("amount", 0)
    amount_cents = int(raw_amount) if isinstance(raw_amount, (int, str)) and str(raw_amount).lstrip("-").isdigit() else 0

    delivery = Delivery(
        event_id=event_id, event=event, settlement_id=settlement_id,
        amount_cents=amount_cents,
        currency=str(entity.get("currency") or "INR").upper(),
        received_utc=datetime.now(timezone.utc), payload=entity,
    )
    _pending[settlement_id] = delivery
    while len(_pending) > REPLAY_MEMORY:
        _pending.popitem(last=False)

    audit.log_decision(
        batch_id=settlement_id, agent="webhook",
        detail=(
            f"Verified {event} for settlement {settlement_id}: "
            f"{amount_cents}c {delivery.currency}. Queued for reconciliation "
            f"— the transaction feed it decomposes into is still pulled, so "
            f"this notifies rather than reconciles."
        ),
    )
    return {"status": "accepted", "event": event, "event_id": event_id,
            "settlement_id": settlement_id, "amount_cents": amount_cents,
            "currency": delivery.currency}


def pending() -> list[Delivery]:
    """Verified settlement notifications not yet reconciled, oldest first."""
    return [d for d in _pending.values() if not d.reconciled]


def mark_reconciled(settlement_id: str) -> bool:
    d = _pending.get(settlement_id)
    if d is None:
        return False
    d.reconciled = True
    return True


def _reset_for_tests() -> None:
    _seen.clear()
    _pending.clear()
