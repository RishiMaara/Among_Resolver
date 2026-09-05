"""
Which payments have already been consumed by a cleared settlement.

WHY
---
The queue can see a payment counted twice inside one run. It cannot see the
case that actually happens: this week's settlement clears on 1,500 payments,
and next week those same payments are still sitting in the pool, free to be
matched again. Nothing carried the memory across runs, so the engine's own
advice — "check these are not also being claimed by another settlement" —
was addressed to a human with no way to check.

That exposure is worst exactly where the engine is least able to help
otherwise. For a merchant selling one item at one price, WHICH payments
compose a settlement is arbitrary and the engine says so; whether one has
been paid out twice is the only thing that can really go wrong.

WHAT IS RECORDED, AND WHEN
--------------------------
Only on a CLEAR. A withheld batch has consumed nothing — its matched set is
a proposal, and writing proposals here would make the ledger a record of
guesses rather than of settlements.

RE-RUNNING A BATCH IS NOT A CONFLICT
------------------------------------
The same settlement reconciled twice must not report itself as stealing its
own payments. Claims are keyed by batch, and a batch always yields to its own
prior claim — re-running is how someone checks a result, and a tool that
punished them for it would teach them not to.

IT WARNS; IT DOES NOT EXCLUDE
-----------------------------
A contested payment is reported, not quietly removed from the pool. Removing
it would change an answer on the strength of a record that could itself be
wrong — a batch cleared in error last week would silently corrupt this week's
reconciliation, and the failure would be invisible. Surfacing beats
correcting when the correction cannot be verified.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Iterable

import audit

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_memory: dict[str, str] = {}          # txn_id -> batch_id, when no DB exists


def _db():
    """Reuse the audit database so durability is one decision, not two."""
    try:
        conn = audit._get_db()
    except Exception:
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


def check_claims(batch_id: str, txn_ids: Iterable[str]) -> dict:
    """
    Which of these payments a DIFFERENT settlement already cleared on.

    A batch never conflicts with itself: re-running a reconciliation is how a
    person checks it, and that must stay free.
    """
    ids = [t for t in (txn_ids or []) if t]
    if not ids:
        return {"count": 0, "claims": [], "summary": ""}

    found: dict[str, str] = {}
    conn = _db()
    if conn is None:
        for t in ids:
            owner = _memory.get(t)
            if owner and owner != batch_id:
                found[t] = owner
    else:
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
                    if owner != batch_id:
                        found[txn] = owner
        except sqlite3.Error as e:
            logger.warning("Could not check settled payments: %s", e)
            return {"count": 0, "claims": [], "summary": ""}

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
