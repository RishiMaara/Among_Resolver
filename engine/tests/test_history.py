"""
A history record has to stay small enough to be read back.

record_run strips the audit trail for exactly this reason, and the module said
so — but it bounded only that one field, and the exception list turned out to
be the bigger one. A run over a pool of identical payments reports nearly all
of them as exceptions; one record on this machine reached 131 MB, 95 MB of it
exceptions. list_runs() opens and parses whole files to build summaries, so a
few of those made a listing take seconds and hold gigabytes at once.

Nothing failed. The listing came back, slowly, and disk filled up quietly.
"""

import json
import os

import pytest

import history


@pytest.fixture
def history_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", str(tmp_path))
    return tmp_path


def _result(exception_count: int) -> dict:
    return {
        "summary": {"batch_id": "B1", "cleared": False, "matched_count": 0},
        "audit_trail": [{"agent": "a", "detail": "x"} for _ in range(1200)],
        "exceptions": [
            {"txn_id": f"T{i}", "reason": "indistinguishable from the rest of the pool"}
            for i in range(exception_count)
        ],
    }


class TestWhatGetsRecorded:
    def test_a_huge_exception_list_is_capped(self, history_dir):
        path = history.record_run("B1", {}, _result(4000))
        rec = json.loads(open(path, encoding="utf-8").read())
        assert len(rec["result"]["exceptions"]) == history.MAX_RECORDED_EXCEPTIONS

    def test_the_record_says_it_was_capped_and_how_many_there_were(self, history_dir):
        path = history.record_run("B1", {}, _result(4000))
        result = json.loads(open(path, encoding="utf-8").read())["result"]
        # A truncated record that does not admit it is worse than a large one.
        assert result["exceptions_truncated"] is True
        assert result["exceptions_total_count"] == 4000
        assert "GET /audit/B1" in result["exceptions_note"]

    def test_an_ordinary_run_is_left_exactly_as_it_was(self, history_dir):
        path = history.record_run("B1", {}, _result(12))
        result = json.loads(open(path, encoding="utf-8").read())["result"]
        assert len(result["exceptions"]) == 12
        assert "exceptions_truncated" not in result
        assert "exceptions_note" not in result

    def test_the_audit_trail_is_still_left_out(self, history_dir):
        path = history.record_run("B1", {}, _result(3))
        result = json.loads(open(path, encoding="utf-8").read())["result"]
        assert "audit_trail" not in result
        assert result["audit_trail_entry_count"] == 1200

    def test_a_capped_record_stays_small_enough_to_hold_many_of(self, history_dir):
        path = history.record_run("B1", {}, _result(50_000))
        # The number that matters: list_runs holds up to 500 of these at once.
        assert os.path.getsize(path) < 2_000_000

    def test_no_exceptions_field_at_all_is_not_an_error(self, history_dir):
        path = history.record_run("B1", {}, {"summary": {"batch_id": "B1"}})
        assert json.loads(open(path, encoding="utf-8").read())["result"]["summary"]


class TestReadingBack:
    def test_a_capped_run_still_lists_and_still_names_its_batch(self, history_dir):
        history.record_run("B1", {"reviewer": "a@b.c"}, _result(4000))
        rows = history.list_runs()
        assert len(rows) == 1
        assert rows[0]["batch_id"] == "B1"
        assert rows[0]["reviewer"] == "a@b.c"

    def test_run_detail_returns_the_capped_record(self, history_dir):
        name = os.path.basename(history.record_run("B1", {}, _result(4000)))
        assert history.run_detail(name)["result"]["exceptions_truncated"] is True

    def test_a_traversal_attempt_reads_nothing(self, history_dir):
        # Batch ids come off an uploaded form; record names come in over HTTP.
        assert history.run_detail("../../../etc/passwd") is None
