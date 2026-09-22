"""
Open items: money not yet reconciled, carried from run to run and aged in
working days (india_calendar.py).

    unsettled_payment       member-feed payment not in a cleared settlement
    refund_not_deducted     refund not yet taken out of a payout
    withheld_settlement     a payout that arrived and did not clear

Only a CLEARED settlement closes items (a withheld set is a proposal).
Closed items are kept, marked closed, with the payout that closed them.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone

import audit
import india_calendar
import india_tax
import settled_ledger
from schema import SourceType

logger = logging.getLogger(__name__)

T_PLUS_WORKING_DAYS = 2
_REDIS_KEY = "open_items"
# One run can hold a 200,000-row pool. The ledger keeps the largest items
# rather than every row, so a shared store on a free tier cannot be filled by
# one upload; the report says when it is holding back.
MAX_ITEMS_PER_RUN = 2_000
AGE_BUCKETS = ((0, 2, "0-2"), (3, 5, "3-5"), (6, 10, "6-10"),
               (11, 30, "11-30"), (31, 10**9, "over 30"))

_lock = threading.Lock()
_memory: dict[str, dict] = {}


@dataclass
class OpenItem:
    item_id: str
    kind: str
    ref: str
    amount_cents: int
    currency: str
    occurred_on: str
    due_on: str
    first_seen_batch: str
    last_seen_batch: str
    first_seen_at: str
    last_seen_at: str
    status: str = "open"
    closed_by: str = ""
    closed_at: str = ""


# ── storage: Redis when shared, else the audit database, else memory ──────

def _shared():
    return audit._get_redis() if audit.redis_url() else None


def _db():
    try:
        conn = audit._get_db()
    except Exception:
        return None
    if conn is None:
        return None
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS open_items ("
                     " item_id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        conn.commit()
    except sqlite3.Error as exc:
        logger.warning("Open items: could not prepare table (%s)", exc)
        return None
    return conn


def _load_all() -> dict[str, dict]:
    shared = _shared()
    if shared is not None:
        try:
            return {k if isinstance(k, str) else k.decode(): json.loads(v)
                    for k, v in shared.hgetall(_REDIS_KEY).items()}
        except Exception as exc:
            logger.warning("Open items: shared read failed (%s)", type(exc).__name__)
    conn = _db()
    if conn is not None:
        try:
            return {r[0]: json.loads(r[1]) for r in
                    conn.execute("SELECT item_id, body FROM open_items")}
        except sqlite3.Error as exc:
            logger.warning("Open items: read failed (%s)", exc)
    return dict(_memory)


def _save(items: dict[str, dict]) -> None:
    if not items:
        return
    shared = _shared()
    if shared is not None:
        try:
            shared.hset(_REDIS_KEY, mapping={k: json.dumps(v) for k, v in items.items()})
            return
        except Exception as exc:
            logger.warning("Open items: shared write failed (%s)", type(exc).__name__)
    conn = _db()
    if conn is not None:
        try:
            conn.executemany("INSERT OR REPLACE INTO open_items (item_id, body) VALUES (?, ?)",
                             [(k, json.dumps(v)) for k, v in items.items()])
            conn.commit()
            return
        except sqlite3.Error as exc:
            logger.warning("Open items: write failed (%s)", exc)
    _memory.update(items)


def _reset_for_tests() -> None:
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
            conn.execute("DELETE FROM open_items")
            conn.commit()
        except sqlite3.Error:
            pass


# ── the rules ─────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _settles(txn) -> bool:
    # Same rule the orchestrator applies: money that did not move cannot
    # be waiting to be paid out.
    from orchestrator import NON_SETTLING_STATUSES  # pylint: disable=import-outside-toplevel
    state = str((txn.extra or {}).get("status") or "").strip().lower()
    return state not in NON_SETTLING_STATUSES


def update_from_run(batch, candidates: list, matched_ids: list[str],
                    cleared: bool) -> dict:
    """Fold one reconciliation into the ledger. Returns what changed."""
    return update_from_runs([(batch, matched_ids, cleared)], candidates)


def update_from_runs(outcomes: list, candidates: list) -> dict:
    """
    Fold reconciliations over one pool into the ledger: (batch, matched, cleared).

    The queue reconciles many payouts against one pool, so it is folded once:
    a payment the third payout cleared is not "opened" by the first and
    closed again two batches later.

    Never raises: the ledger is a view over reconciliations, and a failure to
    write it must not fail the reconciliation it is a view of.
    """
    if not outcomes:
        return {"opened": 0, "closed": 0, "not_tracked": 0}
    try:
        return _update(outcomes, candidates or [])
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Open items: update skipped (%s: %s)", type(exc).__name__, exc)
        return {"opened": 0, "closed": 0, "not_tracked": 0, "error": type(exc).__name__}


def close(batch_id: str, txn_ids: list[str]) -> int:
    """Close items a person settled by decision (the oldest-first convention)."""
    now = _now_iso()
    ids = set(txn_ids or [])
    with _lock:
        changed = {}
        for key, item in _load_all().items():
            if item["status"] == "open" and key.startswith("txn:") and item["ref"] in ids:
                item.update(status="closed", closed_by=batch_id, closed_at=now)
                changed[key] = item
        _save(changed)
    return len(changed)


def _update(outcomes: list, candidates: list) -> dict:
    now = _now_iso()
    first_batch = outcomes[0][0]
    member_source = first_batch.member_source or SourceType.GATEWAY

    settled_by: dict[str, str] = {}          # txn id -> the batch that cleared it
    for batch, matched, cleared in outcomes:
        if cleared:
            for t in matched or []:
                settled_by.setdefault(t, batch.batch_id)
    cleared_batches = {b.batch_id for b, _, c in outcomes if c}

    with _lock:
        existing = _load_all()
        changed: dict[str, dict] = {}
        closed = 0

        for key, item in existing.items():
            if item["status"] != "open":
                continue
            by = None
            if key.startswith("txn:"):
                by = settled_by.get(item["ref"])
            elif key.startswith("batch:") and item["ref"] in cleared_batches:
                by = item["ref"]
            if by:
                item.update(status="closed", closed_by=by, closed_at=now)
                changed[key] = item
                closed += 1

        for batch, _, cleared in outcomes:
            if cleared:
                continue
            key = f"batch:{batch.batch_id}"
            item = changed.get(key) or existing.get(key)
            if item is None or item["status"] != "open":
                settled_on = india_tax.ist_date(batch.settled_at_utc)
                changed[key] = asdict(OpenItem(
                    item_id=key, kind="withheld_settlement", ref=batch.batch_id,
                    amount_cents=batch.net_amount_cents, currency=batch.currency,
                    occurred_on=settled_on.isoformat(), due_on=settled_on.isoformat(),
                    first_seen_batch=batch.batch_id, last_seen_batch=batch.batch_id,
                    first_seen_at=now, last_seen_at=now))
            else:
                item.update(last_seen_at=now)
                changed[key] = item

        # Which of this pool's member-feed payments are still waiting.
        waiting = [t for t in candidates
                   if t.source == member_source and t.amount_cents != 0 and _settles(t)
                   and t.source_txn_id not in settled_by]
        # A payment an earlier cleared settlement already took is not open,
        # even if it is still sitting in this week's file.
        taken = settled_ledger.owners([t.source_txn_id for t in waiting])
        waiting = [t for t in waiting if t.source_txn_id not in taken]
        waiting.sort(key=lambda t: abs(t.amount_cents), reverse=True)
        held_back = max(len(waiting) - MAX_ITEMS_PER_RUN, 0)

        opened = 0
        seen_in = first_batch.batch_id
        for t in waiting[:MAX_ITEMS_PER_RUN]:
            key = f"txn:{t.source_txn_id}"
            item = existing.get(key)
            if item is not None and item["status"] == "closed":
                continue
            if item is None:
                occurred = india_tax.ist_date(t.timestamp_utc)
                due = india_calendar.add_working_days(occurred, T_PLUS_WORKING_DAYS)
                item = asdict(OpenItem(
                    item_id=key,
                    kind="unsettled_payment" if t.amount_cents > 0 else "refund_not_deducted",
                    ref=t.source_txn_id, amount_cents=t.amount_cents, currency=t.currency,
                    occurred_on=occurred.isoformat(), due_on=due.isoformat(),
                    first_seen_batch=seen_in, last_seen_batch=seen_in,
                    first_seen_at=now, last_seen_at=now))
                opened += 1
            else:
                item.update(last_seen_batch=seen_in, last_seen_at=now)
            changed[key] = item

        _save(changed)

    if opened or closed:
        scope = "this queue run" if len(outcomes) > 1 else "this run"
        cap_note = (f"; {held_back} smaller item(s) not tracked (cap "
                    f"{MAX_ITEMS_PER_RUN} per run)" if held_back else "")
        for batch, _, _ in outcomes:
            audit.log_decision(
                batch_id=batch.batch_id, agent="open_items",
                detail=f"Open items: {opened} opened, {closed} closed by {scope}{cap_note}.",
            )
    return {"opened": opened, "closed": closed, "not_tracked": held_back}


def _bucket(age: int) -> str:
    for lo, hi, label in AGE_BUCKETS:
        if lo <= age <= hi:
            return label
    return AGE_BUCKETS[-1][2]


def report(as_of: date | None = None, include_closed: bool = False,
           limit: int = 500) -> dict:
    """The ledger aged against `as_of` (today in India by default)."""
    as_of = as_of or india_tax.ist_date(None)
    rows = []
    for item in _load_all().values():
        if item["status"] != "open" and not include_closed:
            continue
        occurred = date.fromisoformat(item["occurred_on"])
        due = date.fromisoformat(item["due_on"])
        age = india_calendar.working_days_between(occurred, as_of)
        overdue = india_calendar.working_days_between(due, as_of)
        rows.append({**item, "age_working_days": age,
                     "overdue_working_days": overdue if item["status"] == "open" else 0,
                     "bucket": _bucket(age)})

    open_rows = [r for r in rows if r["status"] == "open"]
    buckets = []
    for _, _, label in AGE_BUCKETS:
        inb = [r for r in open_rows if r["bucket"] == label]
        buckets.append({"label": f"{label} working days", "count": len(inb),
                        "value_cents": sum(abs(r["amount_cents"]) for r in inb)})
    overdue = [r for r in open_rows if r["overdue_working_days"] > 0]
    by_kind: dict[str, dict] = {}
    for r in open_rows:
        k = by_kind.setdefault(r["kind"], {"count": 0, "value_cents": 0})
        k["count"] += 1
        k["value_cents"] += abs(r["amount_cents"])

    rows.sort(key=lambda r: (r["status"] != "open", -r["overdue_working_days"],
                             -abs(r["amount_cents"])))
    return {
        "as_of": as_of.isoformat(),
        "calendar": india_calendar.source(),
        "settlement_cycle": f"T+{T_PLUS_WORKING_DAYS} working days",
        "summary": {
            "open_count": len(open_rows),
            "open_value_cents": sum(abs(r["amount_cents"]) for r in open_rows),
            "overdue_count": len(overdue),
            "overdue_value_cents": sum(abs(r["amount_cents"]) for r in overdue),
            "by_kind": by_kind,
            "buckets": buckets,
        },
        "items": rows[:limit],
        "truncated": max(len(rows) - limit, 0),
    }
