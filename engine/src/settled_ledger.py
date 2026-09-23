"""
Payments already consumed by a cleared settlement, remembered across runs.

Recorded only on a CLEAR (a withheld set is a proposal). A batch never
conflicts with its own earlier claim, so re-running is free. A payment
another settlement already cleared withholds the new clear but is never
removed from the pool: the earlier record could itself be wrong.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Iterable

import audit

import stores
logger = logging.getLogger(__name__)
_lock = threading.Lock()
_memory: dict[str, str] = {}          # txn_id -> batch_id, when no DB exists
_REDIS_KEY = "settled:payments"


def _redis():
    """
    The shared store, when one is configured (the audit trail's Redis).

    On serverless there is no writable disk, so without this the ledger was
    per-instance memory: a payment one instance recorded as paid out was
    unknown to the next, and the double-payment check could not see it.
    """
    try:
        return stores.shared_redis()
    except Exception as exc:
        logger.debug("_redis: best-effort step skipped (%s: %s)", type(exc).__name__, exc)
        return None


def _db():
    """Reuse the audit database so durability is one decision, not two."""
    try:
        conn = stores.durable_db()
    except Exception as exc:
        logger.debug("_db: best-effort step skipped (%s: %s)", type(exc).__name__, exc)
        return None
    if conn is None:
        return None
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settled_payments ("
            " txn_id TEXT PRIMARY KEY,"
            " batch_id TEXT NOT NULL,"
            " settled_at TEXT NOT NULL)"
        )
        conn.commit()
    except sqlite3.Error as e:
        logger.warning("Could not prepare settled_payments table: %s", e)
        return None
    return conn


def record_settled(batch_id: str, txn_ids: Iterable[str], when: str = "") -> int:
    """Remember that these payments are spoken for. Returns rows written."""
    ids = [t for t in (txn_ids or []) if t]
    if not batch_id or not ids:
        return 0
    client = _redis()
    if client is not None:
        try:
            pipe = client.pipeline()
            for t in ids:
                pipe.hsetnx(_REDIS_KEY, t, batch_id)
            pipe.execute()
            return len(ids)
        except Exception as e:
            logger.warning("Could not record settled payments in Redis: %s", e)
    conn = _db()
    with _lock:
        if conn is None:
            for t in ids:
                _memory.setdefault(t, batch_id)
            return len(ids)
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO settled_payments (txn_id, batch_id, settled_at)"
                " VALUES (?, ?, ?)",
                [(t, batch_id, when or "") for t in ids],
            )
            conn.commit()
            return len(ids)
        except sqlite3.Error as e:
            logger.warning("Could not record settled payments for %s: %s", batch_id, e)
            return 0


_FEEDS = ("gateway", "bank", "erp")


def _aliases(txn_id: str) -> list[str]:
    """
    The forms a payment may have been recorded under. Clears record the
    collision-safe key ("gateway:1001"); a FIFO acceptance and rows written
    before keys existed hold the bare id. A lookup in one form must find the
    other, or a payment paid out once looks unpaid to every caller that asks
    the other way (FAILURE_LOG 43). A bare id matching another feed's key
    reads as a conflict, which withholds; that is the safe direction.
    """
    feed, sep, bare = txn_id.partition(":")
    if sep and feed in _FEEDS:
        return [txn_id, bare]
    return [txn_id] + [f"{f}:{txn_id}" for f in _FEEDS]


def owners(txn_ids: Iterable[str]) -> dict[str, str]:
    """Every one of these payments a cleared settlement took, and which one."""
    asked = [t for t in (txn_ids or []) if t]
    if not asked:
        return {}
    alias_of = {a: t for t in asked for a in _aliases(t)}
    return {alias_of[a]: b for a, b in _owners_raw(list(alias_of)).items()}


def _owners_raw(ids: list[str]) -> dict[str, str]:
    """Owners of exactly these recorded ids."""
    if not ids:
        return {}
    found: dict[str, str] = {}
    client = _redis()
    if client is not None:
        try:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                for t, owner in zip(chunk, client.hmget(_REDIS_KEY, chunk)):
                    if owner:
                        found[t] = owner.decode() if isinstance(owner, bytes) else owner
            return found
        except Exception as e:
            logger.warning("Could not read settled payments from Redis: %s", e)
            found = {}
    conn = _db()
    if conn is None:
        for t in ids:
            owner = _memory.get(t)
            if owner:
                found[t] = owner
        return found
    try:
        # Chunked so a 50k pool cannot blow SQLite's variable limit.
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            # nosec B608 — the f-string interpolates only the PLACEHOLDER
            # list ("?,?,?"), built from len(chunk). No caller data reaches
            # the SQL text; every value is bound through execute() below.
            # This is the standard way to write a variable-length IN clause
            # in SQLite, and a scanner cannot distinguish it from real
            # interpolation, so the reasoning is recorded here rather than
            # rediscovered on every scan.
            placeholders = ",".join("?" * len(chunk))  # nosec B608
            q = ("SELECT txn_id, batch_id FROM settled_payments"
                 f" WHERE txn_id IN ({placeholders})")  # nosec B608
            for txn, owner in conn.execute(q, chunk):
                found[txn] = owner
    except sqlite3.Error as e:
        logger.warning("Could not check settled payments: %s", e)
        return {}
    return found


def check_claims(batch_id: str, txn_ids: Iterable[str]) -> dict:
    """
    Which of these payments a DIFFERENT settlement already cleared on.

    A batch never conflicts with itself: re-running a reconciliation is how a
    person checks it, and that must stay free.
    """
    found = {t: b for t, b in owners(txn_ids).items() if b != batch_id}

    if not found:
        return {"count": 0, "claims": [], "summary": ""}

    by_batch: dict[str, list[str]] = {}
    for txn, owner in found.items():
        by_batch.setdefault(owner, []).append(txn)

    return {
        "count": len(found),
        "claims": [{"batch_id": b, "payment_count": len(t), "sample": sorted(t)[:10]}
                   for b, t in sorted(by_batch.items(), key=lambda kv: -len(kv[1]))],
        "summary": (
            f"{len(found)} of these payments were already cleared into "
            f"{'another settlement' if len(by_batch) == 1 else f'{len(by_batch)} other settlements'} "
            f"in an earlier run. A payment can only be paid out once, so either "
            f"this settlement or the earlier one is wrong. Nothing has been "
            f"excluded automatically — that is a decision for a person."
        ),
    }
