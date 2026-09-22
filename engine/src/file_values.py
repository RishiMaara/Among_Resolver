"""
Reading one value out of an uploaded file: amounts, dates, and what a column of
values looks like. Split out of file_agent.py, unchanged; file_agent re-exports it.
"""

from __future__ import annotations

from datetime import datetime
import re
import dateutil.parser as dateparser
from ingestion import _fast_parse, clean_amount_str, AmountUnreadable


def _clean_amount(value: str | int | float) -> float:
    """
    Read an amount, or refuse to.

    Thin wrapper over ingestion.clean_amount_str, which owns this because it
    also produces the integer paise that subset-sum matches on. Both files
    grew the same "first numeric fragment wins" bug independently; one
    implementation cannot drift from itself.

    Empty still returns 0.0: a blank cell means the field is absent — a debit
    row on a bank statement carries no credit amount — which is a fact rather
    than a defect.
    """
    if isinstance(value, bool):
        raise AmountUnreadable(f"expected an amount, got a boolean: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return 0.0
    cleaned = clean_amount_str(value)
    return float(cleaned) if cleaned else 0.0


_SLASH_DATE = re.compile(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\s*$")


def resolve_day_order(values: list[str]) -> tuple[str, bool]:
    """
    Decide whether a column of d/m/y-shaped dates is day-first or month-first,
    by looking at the WHOLE column rather than one value.

    A single "09/03/2026" cannot be resolved: it is 9 March to most of the
    world and 3 September in the US. A column can be, because one row with a
    first component above 12 settles it for every other row.

    Returns (order, proven). `proven` is False when every value in the column
    happens to be ambiguous — the caller must not present a guess as a fact.

    This exists because the date was previously handed to the browser as the
    raw string and read with `new Date(...)`, which assumes US month-first.
    An Indian statement's 09/03/2026 became September 3rd — a month that was
    not in the file — and 15/03/2026 became Invalid Date, so the field
    silently never filled at all.
    """
    day_first = month_first = False
    for v in values:
        m = _SLASH_DATE.match(str(v or ""))
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12:
            day_first = True
        if b > 12:
            month_first = True
    if day_first and not month_first:
        return "day", True
    if month_first and not day_first:
        return "month", True
    # Either nothing decisive, or the column contradicts itself. Day-first is
    # the convention in the market this engine targets, and it is reported as
    # an assumption rather than a finding.
    return "day", False


def normalize_date(value: str, order: str = "day") -> str | None:
    """A d/m/y-shaped date as ISO-8601, or None if it is not that shape."""
    m = _SLASH_DATE.match(str(value or ""))
    if not m:
        return None
    first, second, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000 if year < 70 else 1900
    day, month = (first, second) if order == "day" else (second, first)
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def _looks_like_date(vals: list[str]) -> float:
    """Share of values that parse as a date."""
    if not vals:
        return 0.0
    ok = 0
    for v in vals:
        try:
            if _fast_parse(v) is not None:
                ok += 1
                continue
            dateparser.parse(v)
            ok += 1
        except Exception:
            pass
    return ok / len(vals)


def _looks_like_amount(vals: list[str]) -> float:
    """Share that parse as a number. Bare integers score lower than decimals:
    a column of 1, 2, 3 is a row counter, not money."""
    if not vals:
        return 0.0
    ok = 0
    for v in vals:
        try:
            _clean_amount(v)
            ok += 1 if ("." in v or "," in v) else 0.4
        except Exception:
            pass
    return ok / len(vals)


def _looks_like_identifier(vals: list[str]) -> float:
    """High cardinality, mostly alphanumeric, not a date and not a number."""
    if not vals:
        return 0.0
    distinct = len(set(vals)) / len(vals)
    if distinct < 0.7:
        return 0.0
    if _looks_like_date(vals) > 0.5 or _looks_like_amount(vals) > 0.8:
        return 0.0
    alnum = sum(1 for v in vals if any(c.isalnum() for c in v)) / len(vals)
    return distinct * alnum
