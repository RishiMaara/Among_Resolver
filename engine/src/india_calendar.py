"""
Working days for Indian settlement: when a payout is due, and how late it is.

WHY A CALENDAR AND NOT A DAY COUNT
----------------------------------
A gateway pays out on T+2 working days, and "working" is doing the work in
that sentence. A card payment captured on Friday 11 September 2026 is not due
on Sunday the 13th: Saturday the 12th is the second Saturday of the month,
when banks are shut under the RBI's rule since September 2015; Sunday is a
Sunday; Monday the 14th is Ganesh Chaturthi in Maharashtra. It is due on
Wednesday the 16th. Ageing that counts calendar days would call it two days
overdue on the Tuesday, and an ageing report that cries wolf every long
weekend is one people learn to ignore.

WHERE THE HOLIDAYS COME FROM
----------------------------
The `holidays` package's India calendar, for one state — Maharashtra by
default, because Mumbai is where settlement banks clear. Set BANK_HOLIDAY_STATE
to another state code to change it. Festival dates on a lunar calendar are
the package's own, some marked estimated until they are gazetted, so the
RBI's published list is the authority and the calendar can be corrected
without a release:

    EXTRA_BANK_HOLIDAYS=2026-03-19,2026-11-09   # add declared holidays
    NOT_BANK_HOLIDAYS=2026-08-26                # remove one banks worked

If the package is missing the calendar falls back to the three national
holidays, and `source()` says so — /health reports it rather than letting a
thinner calendar pass for the real one.

Sundays and the second and fourth Saturdays are closed whatever the list says.
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from functools import lru_cache

logger = logging.getLogger(__name__)

DEFAULT_STATE = "MH"
_NATIONAL = ((1, 26, "Republic Day"), (8, 15, "Independence Day"),
             (10, 2, "Mahatma Gandhi's Jayanti"))


def _state() -> str:
    return os.environ.get("BANK_HOLIDAY_STATE", DEFAULT_STATE).strip().upper()


def _env_dates(name: str) -> tuple[date, ...]:
    out = []
    for part in os.environ.get(name, "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(date.fromisoformat(part))
        except ValueError:
            logger.warning("%s: ignoring %r, not a YYYY-MM-DD date", name, part)
    return tuple(out)


@lru_cache(maxsize=32)
def _library_holidays(year: int, state: str) -> tuple[dict[date, str], bool]:
    try:
        import holidays  # pylint: disable=import-outside-toplevel
        try:
            cal = holidays.India(years=year, subdiv=state or None)
        except NotImplementedError:
            logger.warning("BANK_HOLIDAY_STATE=%s is not a state the holidays "
                           "package knows; using national holidays only.", state)
            cal = holidays.India(years=year)
        return dict(cal.items()), True
    except ImportError:
        return {date(year, m, d): n for m, d, n in _NATIONAL}, False


def holidays_in(year: int) -> dict[date, str]:
    """Bank holidays for a year, overrides applied."""
    found, _ = _library_holidays(year, _state())
    out = dict(found)
    for d in _env_dates("EXTRA_BANK_HOLIDAYS"):
        if d.year == year:
            out[d] = "Declared bank holiday"
    for d in _env_dates("NOT_BANK_HOLIDAYS"):
        out.pop(d, None)
    return out


def source() -> str:
    _, from_library = _library_holidays(date.today().year, _state())
    if from_library:
        return f"holidays package, India/{_state()}, with env overrides"
    return "national holidays only — the holidays package is not installed"


def closed_because(d: date) -> str | None:
    """Why banks are shut on `d`, or None on a working day."""
    if d.weekday() == 6:
        return "Sunday"
    if d.weekday() == 5:
        nth = (d.day - 1) // 7 + 1
        if nth in (2, 4):
            return f"{'second' if nth == 2 else 'fourth'} Saturday"
    return holidays_in(d.year).get(d)


def is_working_day(d: date) -> bool:
    return closed_because(d) is None


def add_working_days(start: date, n: int) -> date:
    """The n-th working day after `start` (start itself is not counted)."""
    d = start
    left = max(n, 0)
    while left:
        d += timedelta(days=1)
        if is_working_day(d):
            left -= 1
    return d


def working_days_between(start: date, end: date) -> int:
    """Working days in (start, end]. Zero when end is not after start."""
    if end <= start:
        return 0
    count, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if is_working_day(d):
            count += 1
    return count
