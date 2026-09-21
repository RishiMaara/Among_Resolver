"""
The processor's settlement cycle, learned from settlements that verifiably cleared.

WHY
---
When a settlement's references are gone — a bank statement that drops them, a
feed that never had them — the strongest evidence left is timing. A gateway
pays out on a cycle: Razorpay T+2 working days, others T+1, some weekly. The
members of a payout were captured a fixed number of days before it.

Which number is a property of the processor, and it can be read off the
settlements this engine has already CLEARED: each exact, verified clear says
"these members were captured N days before their payout". This module keeps
that histogram. linkage_em.py then uses it as the m-probability of the lag
comparison — the Fellegi-Sunter weight for timing learned from verified
outcomes, never typed in — for pools where nothing names the settlement.

WHAT TEACHES IT
---------------
Only clears: an exact sum that passed every gate, or a person's acceptance.
A withheld proposal teaches nothing, for the same reason it settles nothing
in settled_ledger — learning from guesses would make the cycle a record of
guesses. A profile needs MIN_SETTLEMENTS clears before it is used, so one
unusual payout cannot set the cycle for everything after it.

It learns inside a run too. The queue reconciles settlements in order, so a
run where some payouts carry references and some do not learns the cycle
from the first and applies it to the second, with no history at all.

SCOPE
-----
Kept per member feed and currency — a USD processor and an INR one may run
different cycles. SETTLEMENT_CYCLE_STORE=memory keeps it in-process only,
which is what the benchmarks use so that one run cannot leak into the next.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from collections import Counter
from datetime import datetime

import contextvars
from contextlib import contextmanager

import audit
from linkage_em import LAG_LEVELS, lag_level

logger = logging.getLogger(__name__)

MIN_SETTLEMENTS = 3
_OVERRIDE: contextvars.ContextVar = contextvars.ContextVar("settlement_cycle_override",
                                                          default=None)
_REDIS_KEY = "settlement_cycle"
_lock = threading.Lock()
_memory: dict[str, dict] = {}


def _key(member_source: str, currency: str) -> str:
    return f"{(member_source or 'gateway').lower()}:{(currency or '').upper()}"


def _memory_only() -> bool:
    return os.environ.get("SETTLEMENT_CYCLE_STORE", "").strip().lower() == "memory"


def _shared():
    if _memory_only() or not audit.redis_url():
        return None
    return audit._get_redis()


def _db():
    if _memory_only():
        return None
    try:
        conn = audit._get_db()
    except Exception:
        return None
    if conn is None:
        return None
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS settlement_cycle ("
                     " scope TEXT PRIMARY KEY, body TEXT NOT NULL)")
        conn.commit()
    except sqlite3.Error:
        return None
    return conn


def _load(key: str) -> dict:
    shared = _shared()
    if shared is not None:
        try:
            raw = shared.hget(_REDIS_KEY, key)
            if raw:
                return json.loads(raw)
        except Exception as exc:
            logger.warning("Settlement cycle: shared read failed (%s)", type(exc).__name__)
    conn = _db()
    if conn is not None:
        try:
            row = conn.execute("SELECT body FROM settlement_cycle WHERE scope = ?", (key,)).fetchone()
            if row:
                return json.loads(row[0])
        except sqlite3.Error:
            pass
    return dict(_memory.get(key) or {})


def _save(key: str, body: dict) -> None:
    shared = _shared()
    if shared is not None:
        try:
            shared.hset(_REDIS_KEY, key, json.dumps(body))
            return
        except Exception as exc:
            logger.warning("Settlement cycle: shared write failed (%s)", type(exc).__name__)
    conn = _db()
    if conn is not None:
        try:
            conn.execute("INSERT OR REPLACE INTO settlement_cycle (scope, body) VALUES (?, ?)",
                         (key, json.dumps(body)))
            conn.commit()
            return
        except sqlite3.Error:
            pass
    _memory[key] = body


def reset() -> None:
    """Forget every learned cycle (tests and benchmarks)."""
    with _lock:
        _memory.clear()
        shared = _shared()
        if shared is not None:
            try:
                shared.delete(_REDIS_KEY)
            except Exception:
                pass
        conn = _db()
        if conn is not None:
            try:
                conn.execute("DELETE FROM settlement_cycle")
                conn.commit()
            except sqlite3.Error:
                pass


def record_clear(batch_id: str, member_source: str, currency: str,
                 settled_at: datetime, member_times: list[datetime]) -> None:
    """Add one cleared settlement's capture-to-payout lags to the histogram."""
    if not member_times or os.environ.get("SETTLEMENT_CYCLE_LEARNING", "1").strip() == "0":
        return
    key = _key(member_source, currency)
    lags = Counter(lag_level((settled_at.date() - t.date()).days) for t in member_times)
    with _lock:
        body = _load(key)
        seen = body.get("batches") or []
        if batch_id in seen:
            return          # re-running a batch is checking it, not new evidence
        counts = Counter(body.get("counts") or {})
        counts.update(lags)
        body = {"counts": dict(counts), "settlements": int(body.get("settlements", 0)) + 1,
                "members": int(body.get("members", 0)) + len(member_times),
                "batches": (seen + [batch_id])[-500:]}
        _save(key, body)


@contextmanager
def using(profile_body: dict | None):
    """
    Use this profile instead of the stored one, for the calls inside.

    The Razorpay blind check needs the cycle learned from every OTHER
    settlement in the batch — never the one being checked, whose own lags
    would hand it the answer — and must not write that into the store.
    """
    token = _OVERRIDE.set(profile_body)
    try:
        yield
    finally:
        _OVERRIDE.reset(token)


def profile_from(lags: list[int], settlements: int) -> dict | None:
    """A profile built from raw lags, as profile() would build it."""
    if settlements < MIN_SETTLEMENTS or not lags:
        return None
    counts = Counter(lag_level(d) for d in lags)
    total = sum(counts.values()) + 0.5 * len(LAG_LEVELS)
    return {"m": {lv: (counts.get(lv, 0) + 0.5) / total for lv in LAG_LEVELS},
            "settlements": settlements, "members": len(lags)}


def profile(member_source: str, currency: str) -> dict | None:
    """
    The learned m-probabilities for the lag comparison, or None if too thin.

    Smoothed so a lag never seen among members is improbable, not impossible:
    a processor that settles T+1 still has the odd payment that waits a day
    longer, and an m of exactly zero would veto it outright.
    """
    override = _OVERRIDE.get()
    if override is not None:
        return override
    body = _load(_key(member_source, currency))
    if int(body.get("settlements", 0)) < MIN_SETTLEMENTS:
        return None
    counts = body.get("counts") or {}
    total = sum(counts.values()) + 0.5 * len(LAG_LEVELS)
    return {
        "m": {lv: (counts.get(lv, 0) + 0.5) / total for lv in LAG_LEVELS},
        "settlements": int(body["settlements"]),
        "members": int(body.get("members", 0)),
    }


def learn_from(batch, candidates: list, matched_ids: list[str]) -> None:
    """Record a cleared batch's members, read from the member feed."""
    from schema import SourceType  # pylint: disable=import-outside-toplevel
    feed = batch.member_source or SourceType.GATEWAY
    ids = set(matched_ids or [])
    times = [t.timestamp_utc for t in candidates
             if t.source == feed and t.source_txn_id in ids]
    try:
        record_clear(batch.batch_id, feed.value, batch.currency,
                     batch.settled_at_utc, times)
    except Exception as exc:  # pragma: no cover - learning must never fail a run
        logger.warning("Settlement cycle: not recorded (%s)", type(exc).__name__)
