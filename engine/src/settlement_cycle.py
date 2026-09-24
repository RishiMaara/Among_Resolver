"""
The processor's settlement cycle, learned from settlements that cleared.

Where references are gone, timing is the evidence left: members of a payout
were captured a fixed number of days before it (Razorpay: T+2 working days);
lags are learned in calendar and in working days, and the sharper is used.
Each verified clear adds to a lag histogram, which linkage_em.py uses as the
m-probability for lag. Only clears teach it, and a profile needs
MIN_SETTLEMENTS clears before use; it also learns within a queue run. Kept
per merchant, member feed and currency (SettlementBatch.merchant, the
upload's merchant_id); no merchant keeps the deployment's single key.
SETTLEMENT_CYCLE_STORE=memory keeps it in-process (benchmarks).
"""

from __future__ import annotations

import json
import math
import logging
import os
import sqlite3
import threading
from collections import Counter
from datetime import datetime

import contextvars
from contextlib import contextmanager

import audit
import stores
from linkage_em import LAG_LEVELS, lag_level, payout_lag

logger = logging.getLogger(__name__)

MIN_SETTLEMENTS = 3
_OVERRIDE: contextvars.ContextVar = contextvars.ContextVar("settlement_cycle_override",
                                                          default=None)
_REDIS_KEY = "settlement_cycle"
_lock = threading.Lock()
_memory: dict[str, dict] = stores.local("settlement_cycle", dict)


def merchant_key(merchant: str) -> str:
    """A merchant id as stored: letters, digits, '-' and '_', at most 64."""
    return "".join(ch for ch in (merchant or "").strip().lower()
                   if ch.isalnum() or ch in "-_")[:64]


def _key(member_source: str, currency: str, merchant: str = "") -> str:
    base = f"{(member_source or 'gateway').lower()}:{(currency or '').upper()}"
    m = merchant_key(merchant)
    # The single-merchant key is unchanged, so history learned before
    # merchants existed is still found.
    return f"{m}|{base}" if m else base


def _memory_only() -> bool:
    return os.environ.get("SETTLEMENT_CYCLE_STORE", "").strip().lower() == "memory"


def _shared():
    if _memory_only() or not audit.redis_url():
        return None
    return stores.any_redis()


def _db():
    if _memory_only():
        return None
    try:
        conn = stores.durable_db()
    except Exception as exc:
        logger.debug("_db: best-effort step skipped (%s: %s)", type(exc).__name__, exc)
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
            except Exception as exc:
                logger.debug("reset: best-effort step skipped (%s: %s)", type(exc).__name__, exc)
        conn = _db()
        if conn is not None:
            try:
                conn.execute("DELETE FROM settlement_cycle")
                conn.commit()
            except sqlite3.Error:
                pass


def record_clear(batch_id: str, member_source: str, currency: str,
                 settled_at: datetime, member_times: list[datetime],
                 merchant: str = "") -> None:
    """Add one cleared settlement's capture-to-payout lags to the histogram."""
    if not member_times or os.environ.get("SETTLEMENT_CYCLE_LEARNING", "1").strip() == "0":
        return
    key = _key(member_source, currency, merchant)
    lags = Counter(lag_level((settled_at.date() - t.date()).days) for t in member_times)
    working = Counter(lag_level(payout_lag(t.date(), settled_at.date())) for t in member_times)
    with _lock:
        body = _load(key)
        seen = body.get("batches") or []
        if batch_id in seen:
            return          # re-running a batch is checking it, not new evidence
        counts = Counter(body.get("counts") or {})
        counts.update(lags)
        counts_working = Counter(body.get("counts_working") or {})
        counts_working.update(working)
        body = {"counts": dict(counts), "counts_working": dict(counts_working),
                "settlements": int(body.get("settlements", 0)) + 1,
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


def _smoothed(counts: dict) -> dict[str, float]:
    total = sum(counts.values()) + 0.5 * len(LAG_LEVELS)
    return {lv: (counts.get(lv, 0) + 0.5) / total for lv in LAG_LEVELS}


def _entropy(m: dict[str, float]) -> float:
    return -sum(p * math.log(p) for p in m.values() if p > 0)


def _sharper(calendar: dict, working: dict | None) -> tuple[dict[str, float], str]:
    """
    The lag histogram in whichever unit this processor actually keeps.

    Razorpay pays T+2 WORKING days, so in calendar days one cycle reads as 2 to
    5 around every weekend and holiday; a processor paying on calendar days is
    sharp in calendar days and smeared in working ones. Hard-coding working
    days took the third-party ReconRiver benchmark from 56.76% to 43.24%
    exact, because its generator pays on calendar days. So both are learned,
    and working days are used only where clearly sharper (lower entropy);
    otherwise calendar days, the unit used before.
    """
    m_cal = _smoothed(calendar)
    if not working:
        return m_cal, "calendar"
    m_work = _smoothed(working)
    if _entropy(m_work) < _entropy(m_cal) - 0.05:
        return m_work, "working"
    return m_cal, "calendar"


def profile_from(lags: list[int], settlements: int,
                 working_lags: list[int] | None = None) -> dict | None:
    """A profile built from raw lags, as profile() would build it."""
    if settlements < MIN_SETTLEMENTS or not lags:
        return None
    m, unit = _sharper(Counter(lag_level(d) for d in lags),
                       Counter(lag_level(d) for d in working_lags) if working_lags else None)
    return {"m": m, "unit": unit, "settlements": settlements, "members": len(lags)}


def profile(member_source: str, currency: str, merchant: str = "") -> dict | None:
    """
    The learned m-probabilities for the lag comparison, or None if too thin.

    Smoothed so a lag never seen among members is improbable, not impossible:
    a processor that settles T+1 still has the odd payment that waits a day
    longer, and an m of exactly zero would veto it outright.
    """
    override = _OVERRIDE.get()
    if override is not None:
        return override
    body = _load(_key(member_source, currency, merchant))
    if int(body.get("settlements", 0)) < MIN_SETTLEMENTS:
        return None
    m, unit = _sharper(body.get("counts") or {}, body.get("counts_working"))
    return {
        "m": m,
        "unit": unit,
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
                     batch.settled_at_utc, times, merchant=getattr(batch, "merchant", ""))
    except Exception as exc:  # pragma: no cover - learning must never fail a run
        logger.warning("Settlement cycle: not recorded (%s)", type(exc).__name__)
