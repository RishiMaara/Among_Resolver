"""
The open-items ledger and the working-day calendar behind its ageing.

`GET /open-items` is what is still waiting to be paid out across every
reconciliation so far, aged in working days. `GET /calendar/due` shows the
arithmetic for one payment — which days were skipped and why — because an
ageing figure nobody can check is an ageing figure nobody trusts.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query

import india_calendar
import open_items

router = APIRouter()


def _day(value: str, name: str) -> date | None:
    if not (value or "").strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(status_code=422, detail={
            "message": f"{name} must be YYYY-MM-DD",
            "plain": f"Could not read {value!r} as a date.",
        })


@router.get("/open-items", summary="Unreconciled money across runs, aged in working days")
def list_open_items(as_of: str = Query("", description="YYYY-MM-DD; default today in India"),
                    include_closed: bool = False, limit: int = Query(500, ge=1, le=5000)):
    return open_items.report(as_of=_day(as_of, "as_of"),
                             include_closed=include_closed, limit=limit)


@router.get("/calendar/due", summary="When a payment is due to settle, and why")
def due_date(captured_on: str = Query(..., description="YYYY-MM-DD, in India"),
             t_plus: int = Query(open_items.T_PLUS_WORKING_DAYS, ge=0, le=30)):
    start = _day(captured_on, "captured_on")
    due = india_calendar.add_working_days(start, t_plus)
    skipped = []
    d = start
    while d < due:
        d += timedelta(days=1)
        why = india_calendar.closed_because(d)
        if why:
            skipped.append({"date": d.isoformat(), "closed_because": why})
    return {
        "captured_on": start.isoformat(),
        "t_plus_working_days": t_plus,
        "due_on": due.isoformat(),
        "calendar_days": (due - start).days,
        "skipped": skipped,
        "calendar": india_calendar.source(),
    }
