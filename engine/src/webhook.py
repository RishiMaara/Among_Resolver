"""
Razorpay settlement webhooks, with the signature checked.

A verified delivery is a NOTIFICATION that settlement X processed, not a
reconciliation: its payments still come from the pulled feeds. Settlements
are pushed, transactions are pulled.

    no secret configured    503 - fail loudly, never accept unsigned
    bad/missing signature   401 - the body is never parsed
    replayed event id       accepted and ignored (Razorpay retries)
    oversized body          413 before buffering

Signatures are compared with hmac.compare_digest.
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

import stores
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

# Replay suppression and the pending queue live in Redis when REDIS_URL /
# KV_URL is set (shared across instances), else in memory, remembered long
# enough to outlive Razorpay's 24-hour retry window.
REPLAY_MEMORY = 4096

# In Redis, seen ids expire instead: 48 hours comfortably outlives Razorpay's
# 24-hour retry window, and nothing has to be evicted by hand.
REPLAY_TTL_S = 48 * 3600
_REDIS_SEEN_PREFIX = "webhook:seen:"
_REDIS_PENDING = "webhook:pending"

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


def _shared_store():
    """The Redis client — but only when a URL was configured explicitly."""
    if not audit.redis_url():
        return None
    return stores.any_redis()


def storage_status() -> dict:
    return {"backend": "redis" if _shared_store() is not None else "memory",
            "shared": _shared_store() is not None}


def _remember(event_id: str) -> bool:
    """True if this is the first time ANY instance has seen `event_id`."""
    shared = _shared_store()
    if shared is not None:
        try:
            # SET NX is the whole mechanism. It is atomic in Redis, so two
            # instances handed the same retry in the same millisecond cannot
            # both be told it is new — a read-then-write here would let both
            # read "absent" before either wrote.
            return bool(shared.set(f"{_REDIS_SEEN_PREFIX}{event_id}", "1",
                                   nx=True, ex=REPLAY_TTL_S))
        except Exception as exc:
            logger.warning("Webhook: shared replay store failed (%s); falling "
                           "back to this instance's memory.", type(exc).__name__)
    if event_id in _seen:
        return False
    _seen[event_id] = ""
    while len(_seen) > REPLAY_MEMORY:
        _seen.popitem(last=False)
    return True


def _to_record(d: "Delivery") -> str:
    return json.dumps({
        "event_id": d.event_id, "event": d.event, "settlement_id": d.settlement_id,
        "amount_cents": d.amount_cents, "currency": d.currency,
        "received_utc": d.received_utc.isoformat(), "payload": d.payload,
        "reconciled": d.reconciled,
    })


def _from_record(raw: str) -> "Delivery":
    r = json.loads(raw)
    return Delivery(
        event_id=r["event_id"], event=r["event"], settlement_id=r["settlement_id"],
        amount_cents=int(r["amount_cents"]), currency=r["currency"],
        received_utc=datetime.fromisoformat(r["received_utc"]),
        payload=r.get("payload") or {}, reconciled=bool(r.get("reconciled")),
    )


def _store_pending(delivery: "Delivery") -> None:
    shared = _shared_store()
    if shared is not None:
        try:
            shared.hset(_REDIS_PENDING, delivery.settlement_id, _to_record(delivery))
            if shared.hlen(_REDIS_PENDING) > REPLAY_MEMORY:
                # Over the cap: drop the oldest by receipt time. Runs only
                # past 4,096 entries, so the full read is rare and bounded.
                everything = [_from_record(v) for v in shared.hgetall(_REDIS_PENDING).values()]
                everything.sort(key=lambda d: d.received_utc)
                excess = [d.settlement_id for d in everything[:len(everything) - REPLAY_MEMORY]]
                if excess:
                    shared.hdel(_REDIS_PENDING, *excess)
            return
        except Exception as exc:
            logger.warning("Webhook: shared pending store failed (%s); keeping "
                           "this delivery in this instance's memory.", type(exc).__name__)
    _pending[delivery.settlement_id] = delivery
    while len(_pending) > REPLAY_MEMORY:
        _pending.popitem(last=False)


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
    _store_pending(delivery)

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
    shared = _shared_store()
    if shared is not None:
        try:
            found = [_from_record(v) for v in shared.hgetall(_REDIS_PENDING).values()]
            return sorted((d for d in found if not d.reconciled),
                          key=lambda d: d.received_utc)
        except Exception as exc:
            logger.warning("Webhook: shared pending read failed (%s); showing "
                           "this instance's memory.", type(exc).__name__)
    return [d for d in _pending.values() if not d.reconciled]


def mark_reconciled(settlement_id: str) -> bool:
    shared = _shared_store()
    if shared is not None:
        try:
            raw = shared.hget(_REDIS_PENDING, settlement_id)
            if raw is None:
                return False
            d = _from_record(raw)
            d.reconciled = True
            shared.hset(_REDIS_PENDING, settlement_id, _to_record(d))
            return True
        except Exception as exc:
            logger.warning("Webhook: shared pending update failed (%s).",
                           type(exc).__name__)
    local = _pending.get(settlement_id)
    if local is None:
        return False
    local.reconciled = True
    return True


def _reset_for_tests() -> None:
    _seen.clear()
    _pending.clear()
