"""
The payout cycle is learned in the unit the processor keeps.

Razorpay pays T+2 WORKING days: a Friday capture before a second Saturday,
a Sunday and a bank holiday is paid five calendar days later. Learned in
calendar days that one cycle smears over 2-5 days; learned in working days
it is 2 every time. Both are learned and the sharper is used, so a processor
that pays on calendar days keeps calendar days (the ReconRiver benchmark
stays at 56.76%).
"""
from datetime import date, datetime, time, timezone

import india_calendar
import linkage_em
import settlement_cycle


def _at(d: date) -> datetime:
    return datetime.combine(d, time(10, 0), tzinfo=timezone.utc)


def _learn(pairs):
    settlement_cycle.reset()
    for n, (captured, paid) in enumerate(pairs):
        settlement_cycle.record_clear(f"cyc{n}", "gateway", "INR", _at(paid), [_at(c) for c in captured])
    return settlement_cycle.profile("gateway", "INR")


def test_a_t_plus_2_working_day_processor_is_learned_in_working_days():
    captures = [date(2026, 9, d) for d in (7, 8, 9, 10, 11, 16, 17, 18)]
    pairs = [([c], india_calendar.add_working_days(c, 2)) for c in captures]
    prof = _learn(pairs)
    assert prof["unit"] == "working"
    assert max(prof["m"], key=prof["m"].get) == "2"
    # Friday 11 Sep: second Saturday, Sunday and Ganesh Chaturthi in between.
    assert linkage_em.lag_days(date(2026, 9, 11), date(2026, 9, 16), prof["unit"]) == 2


def test_a_calendar_day_processor_keeps_calendar_days():
    captures = [date(2026, 9, d) for d in (7, 8, 9, 10, 11, 12, 13, 14)]
    pairs = [([c], date(2026, 9, c.day + 2)) for c in captures]
    prof = _learn(pairs)
    assert prof["unit"] == "calendar"
    assert max(prof["m"], key=prof["m"].get) == "2"


def test_a_payout_before_its_capture_is_never_read_as_same_day():
    assert linkage_em.lag_level(linkage_em.lag_days(date(2026, 9, 12), date(2026, 9, 10), "working")) == "other"
