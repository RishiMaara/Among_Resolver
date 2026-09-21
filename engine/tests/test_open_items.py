"""
Open items and the working-day calendar they are aged against.

A reconciliation says which payments are in one payout. The ledger says what
is still waiting across all of them, and how late — in working days, because
a payment captured before a second Saturday and a festival is not late on
the Monday.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from fastapi.testclient import TestClient

import india_calendar
import main
import open_items
import settled_ledger
from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence

SAMPLES = Path(__file__).resolve().parents[2] / "public" / "sample-data"


@pytest.fixture(autouse=True)
def clean_ledger():
    open_items._reset_for_tests()
    yield
    open_items._reset_for_tests()


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def pay(txn_id, amount, when=utc(2026, 9, 11, 6), source=SourceType.GATEWAY, status=""):
    return NormalizedTxn(
        source=source, source_txn_id=txn_id, ref_id_canonical=txn_id.upper(),
        amount_cents=amount, currency="INR", timestamp_utc=when,
        tz_confidence=TzConfidence.HIGH, extra={"status": status} if status else {},
    )


def batch(bid, net=100_000, settled=utc(2026, 9, 16, 6)):
    return SettlementBatch(batch_id=bid, net_amount_cents=net, currency="INR",
                           settled_at_utc=settled, member_source=SourceType.GATEWAY)


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class TestTheCalendar:
    def test_second_and_fourth_saturdays_are_closed(self):
        assert india_calendar.closed_because(date(2026, 9, 12)) == "second Saturday"
        assert india_calendar.closed_because(date(2026, 9, 26)) == "fourth Saturday"

    def test_first_and_third_saturdays_are_working(self):
        assert india_calendar.is_working_day(date(2026, 9, 5))
        assert india_calendar.is_working_day(date(2026, 9, 19))

    def test_the_friday_before_a_long_weekend(self):
        """
        Captured Friday 11 September 2026. Saturday is the second Saturday,
        Sunday is Sunday, Monday is Ganesh Chaturthi in Maharashtra. T+2
        working days is Wednesday the 16th, five calendar days later.
        """
        assert india_calendar.add_working_days(date(2026, 9, 11), 2) == date(2026, 9, 16)

    def test_counting_working_days(self):
        assert india_calendar.working_days_between(date(2026, 9, 11), date(2026, 9, 16)) == 2
        assert india_calendar.working_days_between(date(2026, 9, 16), date(2026, 9, 11)) == 0

    def test_a_declared_holiday_can_be_added_without_a_release(self, monkeypatch):
        monkeypatch.setenv("EXTRA_BANK_HOLIDAYS", "2026-09-15")
        assert india_calendar.closed_because(date(2026, 9, 15)) == "Declared bank holiday"
        assert india_calendar.add_working_days(date(2026, 9, 11), 2) == date(2026, 9, 17)

    def test_a_holiday_banks_worked_can_be_removed(self, monkeypatch):
        monkeypatch.setenv("NOT_BANK_HOLIDAYS", "2026-09-14")
        assert india_calendar.is_working_day(date(2026, 9, 14))

    def test_the_calendar_says_where_it_came_from(self):
        assert "India/MH" in india_calendar.source()


class TestOpening:
    def test_a_withheld_run_opens_the_payout_and_its_payments(self):
        b = batch(uid("OI-W"))
        p1, p2 = uid("pay"), uid("pay")
        delta = open_items.update_from_run(b, [pay(p1, 60_000), pay(p2, 40_000)],
                                           matched_ids=[p1, p2], cleared=False)
        assert delta["opened"] == 2, "a proposal settles nothing"
        rep = open_items.report(as_of=date(2026, 9, 16))
        kinds = {i["kind"] for i in rep["items"]}
        assert kinds == {"withheld_settlement", "unsettled_payment"}

    def test_a_cleared_run_opens_only_what_it_did_not_take(self):
        b = batch(uid("OI-C"))
        member, other = uid("pay"), uid("pay")
        open_items.update_from_run(b, [pay(member, 60_000), pay(other, 40_000)],
                                   matched_ids=[member], cleared=True)
        refs = {i["ref"] for i in open_items.report()["items"]}
        assert refs == {other}

    def test_money_that_never_moved_is_not_waiting(self):
        b = batch(uid("OI-F"))
        failed = uid("pay")
        open_items.update_from_run(b, [pay(failed, 50_000, status="failed")], [], cleared=True)
        assert open_items.report()["summary"]["open_count"] == 0

    def test_only_the_member_feed_is_tracked(self):
        """The bank and ledger rows are mirrors of the same payments."""
        b = batch(uid("OI-M"))
        open_items.update_from_run(b, [pay(uid("bank"), 50_000, source=SourceType.BANK)],
                                   [], cleared=True)
        assert open_items.report()["summary"]["open_count"] == 0

    def test_a_payment_an_earlier_payout_took_is_not_open(self):
        taken = uid("pay")
        settled_ledger.record_settled(uid("EARLIER"), [taken])
        open_items.update_from_run(batch(uid("OI-T")), [pay(taken, 50_000)], [], cleared=True)
        assert open_items.report()["summary"]["open_count"] == 0

    def test_a_refund_waits_to_be_deducted(self):
        r = uid("rfnd")
        open_items.update_from_run(batch(uid("OI-R")), [pay(r, -20_000)], [], cleared=True)
        assert open_items.report()["items"][0]["kind"] == "refund_not_deducted"


class TestClosing:
    def test_a_later_clear_closes_the_payments_and_the_payout(self):
        bid = uid("OI-LATER")
        p1, p2 = uid("pay"), uid("pay")
        pool = [pay(p1, 60_000), pay(p2, 40_000)]
        open_items.update_from_run(batch(bid), pool, [p1, p2], cleared=False)
        delta = open_items.update_from_run(batch(bid), pool, [p1, p2], cleared=True)
        assert delta["closed"] == 3
        assert open_items.report()["summary"]["open_count"] == 0
        closed = open_items.report(include_closed=True)["items"]
        assert all(i["closed_by"] == bid for i in closed), "which payout took it is kept"

    def test_a_cleared_payment_is_not_reopened_by_a_later_file(self):
        # As /reconcile/upload does it: a clear is recorded in the settled
        # ledger first, and that record is what keeps it out of later pools.
        p1, first = uid("pay"), uid("OI-A")
        settled_ledger.record_settled(first, [p1])
        open_items.update_from_run(batch(first), [pay(p1, 50_000)], [p1], cleared=True)
        open_items.update_from_run(batch(uid("OI-B")), [pay(p1, 50_000)], [], cleared=True)
        assert open_items.report()["summary"]["open_count"] == 0

    def test_a_person_accepting_the_convention_closes_them(self):
        p1 = uid("pay")
        open_items.update_from_run(batch(uid("OI-P")), [pay(p1, 50_000)], [], cleared=True)
        assert open_items.close(uid("FIFO"), [p1]) == 1
        assert open_items.report()["summary"]["open_count"] == 0

    def test_a_queue_folds_once(self):
        """Payments the second payout cleared are never opened by the first."""
        a, b = uid("pay"), uid("pay")
        pool = [pay(a, 50_000), pay(b, 70_000)]
        delta = open_items.update_from_runs(
            [(batch(uid("Q1")), [a], True), (batch(uid("Q2")), [b], True)], pool)
        assert delta == {"opened": 0, "closed": 0, "not_tracked": 0}


class TestAgeing:
    def test_overdue_is_counted_in_working_days_after_the_due_date(self):
        p1 = uid("pay")
        open_items.update_from_run(batch(uid("OI-AGE")), [pay(p1, 50_000)], [], cleared=True)
        item = open_items.report(as_of=date(2026, 9, 18))["items"][0]
        assert item["due_on"] == "2026-09-16"
        assert item["overdue_working_days"] == 2
        assert item["age_working_days"] == 4

    def test_not_late_on_the_monday_holiday(self):
        p1 = uid("pay")
        open_items.update_from_run(batch(uid("OI-HOL")), [pay(p1, 50_000)], [], cleared=True)
        rep = open_items.report(as_of=date(2026, 9, 14))
        assert rep["summary"]["overdue_count"] == 0, "calendar days would say late"

    def test_buckets_add_up(self):
        pool = [pay(uid("pay"), 10_000 * (i + 1), when=utc(2026, 8, 1 + i, 6)) for i in range(5)]
        open_items.update_from_run(batch(uid("OI-B")), pool, [], cleared=True)
        s = open_items.report(as_of=date(2026, 9, 21))["summary"]
        assert sum(b["count"] for b in s["buckets"]) == s["open_count"] == 5
        assert sum(b["value_cents"] for b in s["buckets"]) == s["open_value_cents"]

    def test_the_cap_is_reported_not_hidden(self, monkeypatch):
        monkeypatch.setattr(open_items, "MAX_ITEMS_PER_RUN", 2)
        pool = [pay(uid("pay"), 1_000 * (i + 1)) for i in range(5)]
        delta = open_items.update_from_run(batch(uid("OI-CAP")), pool, [], cleared=True)
        assert delta["not_tracked"] == 3
        amounts = sorted(i["amount_cents"] for i in open_items.report()["items"])
        assert amounts == [4_000, 5_000], "the largest are the ones kept"


class TestTheEndpoints:
    @pytest.fixture
    def client(self):
        return TestClient(main.app)

    def test_due_date_shows_its_working(self, client):
        body = client.get("/calendar/due", params={"captured_on": "2026-09-11"}).json()
        assert body["due_on"] == "2026-09-16"
        reasons = [s["closed_because"] for s in body["skipped"]]
        assert reasons[0] == "second Saturday" and reasons[1] == "Sunday"
        assert "Ganesh" in reasons[2]

    def test_open_items_endpoint(self, client):
        open_items.update_from_run(batch(uid("OI-E")), [pay(uid("pay"), 50_000)], [], cleared=True)
        body = client.get("/open-items", params={"as_of": "2026-09-21"}).json()
        assert body["summary"]["open_count"] == 1
        assert body["settlement_cycle"] == "T+2 working days"

    def test_a_bad_date_is_refused(self, client):
        assert client.get("/open-items", params={"as_of": "yesterday"}).status_code == 422

    def test_an_upload_folds_into_the_ledger(self, client):
        files = {
            "gateway_file": ("g.csv", (SAMPLES / "gateway_report.csv").read_bytes(), "text/csv"),
            "bank_file": ("b.csv", (SAMPLES / "bank_statement.csv").read_bytes(), "text/csv"),
            "erp_file": ("e.json", (SAMPLES / "erp_ledger.json").read_bytes(), "application/json"),
        }
        r = client.post("/reconcile/upload", data={
            "batch_id": uid("OI-UP"), "net_amount": "66466.36",
            "settled_at": "2026-09-02T00:00:00Z", "settlement_window_days": "5",
            "currency": "INR", "declared_deductions": "2055.66",
            "member_source": "gateway"}, files=files)
        assert r.status_code == 200
        assert "opened" in r.json()["open_items"]

    def test_health_names_the_calendar(self, client):
        assert "India" in client.get("/health").json()["bank_calendar"]
