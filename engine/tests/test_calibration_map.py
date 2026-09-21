"""
Calibrated confidence: what a figure has been worth, and what reviewers say.

The map is isotonic (monotone by construction), pools tied confidences,
smooths small blocks, and never touches the auto-clear gate. Reviewer
verdicts are recorded as outcomes, and a band where they disagree with the
shipped map by more than 15 points over 20+ decisions is reported as drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import calibration_map as cm
import main

SAMPLES = Path(__file__).resolve().parents[2] / "public" / "sample-data"
FIT = Path(__file__).resolve().parents[1] / "docs" / "benchmarks" / "calibration_fit.json"


@pytest.fixture(autouse=True)
def fresh_outcomes():
    cm._reset_for_tests()
    yield
    cm._reset_for_tests()


class TestTheFit:
    def test_it_is_monotone(self):
        pairs = [(0.2, False), (0.3, True), (0.4, False), (0.6, True), (0.7, False), (0.9, True)]
        m = cm.fit(pairs)
        assert m.ys == sorted(m.ys)

    def test_tied_confidences_are_pooled(self):
        m = cm.fit([(0.05, True)] + [(0.05, False)] * 9)
        assert len(m.xs) == 1
        assert m.ys[0] == pytest.approx(2 / 12, abs=1e-4), "1 right of 10, smoothed"

    def test_seven_of_seven_is_not_certainty(self):
        m = cm.fit([(0.8, True)] * 7)
        assert m(0.8) == pytest.approx(8 / 9, abs=1e-4)

    def test_between_blocks_it_interpolates(self):
        m = cm.IsotonicMap(xs=[0.2, 0.8], ys=[0.1, 0.9])
        assert m(0.5) == pytest.approx(0.5)
        assert m(0.0) == 0.1 and m(1.0) == 0.9

    def test_ece_of_a_perfect_calibration_is_zero(self):
        pairs = [(0.25, True)] + [(0.25, False)] * 3
        assert cm.ece(pairs) == 0.0


class TestTheShippedMap:
    def test_it_is_there_and_says_the_low_band_is_low(self):
        m = cm.shipped()
        assert m is not None and m.fitted_on >= 200
        assert m(0.14) < 0.1, "out-of-sample the low band was right 3.1% of the time"

    def test_the_published_cross_check_improves_out_of_sample(self):
        d = json.loads(FIT.read_text(encoding="utf-8"))
        fwd = next(c for c in d["cross"] if c["fit"] == "benchmark -> ReconRiver")
        assert fwd["ece_calibrated"] < fwd["ece_raw"]

    def test_every_report_carries_it_beside_the_raw_figure(self):
        c = TestClient(main.app)
        files = {
            "gateway_file": ("g.csv", (SAMPLES / "gateway_report.csv").read_bytes(), "text/csv"),
            "bank_file": ("b.csv", (SAMPLES / "bank_statement.csv").read_bytes(), "text/csv"),
            "erp_file": ("e.json", (SAMPLES / "erp_ledger.json").read_bytes(), "application/json"),
        }
        body = c.post("/reconcile/upload", data={
            "batch_id": "CAL-SAMPLE", "net_amount": "66466.36",
            "settled_at": "2026-09-02T00:00:00Z", "settlement_window_days": "5",
            "currency": "INR", "declared_deductions": "2055.66",
            "member_source": "gateway"}, files=files).json()
        s = body["summary"]
        assert s["calibrated_confidence"] == cm.calibrated(s["confidence"])


class TestReviewersTeachIt:
    def test_a_decision_on_a_proposed_set_is_an_outcome(self):
        c = TestClient(main.app)
        files = {
            "gateway_file": ("g.csv", (SAMPLES / "gateway_report.csv").read_bytes(), "text/csv"),
            "bank_file": ("b.csv", (SAMPLES / "bank_statement.csv").read_bytes(), "text/csv"),
            "erp_file": ("e.json", (SAMPLES / "erp_ledger.json").read_bytes(), "application/json"),
        }
        c.post("/reconcile/upload", data={
            "batch_id": "CAL-DECIDE", "net_amount": "66466.36",
            "settled_at": "2026-09-02T00:00:00Z", "settlement_window_days": "5",
            "currency": "INR", "declared_deductions": "2055.66",
            "member_source": "gateway"}, files=files)
        c.post("/settlement/CAL-DECIDE/decision", json={"decision": "rejected", "reviewer": "Priya"})
        rows = cm.outcomes()
        assert len(rows) == 1 and rows[0]["confirmed"] is False
        report = c.get("/calibration").json()
        assert report["reviewer_decisions"] == 1

    def test_twenty_disagreeing_decisions_are_reported_as_drift(self):
        for i in range(20):
            cm.record_outcome(f"B{i}", 0.95, confirmed=(i < 10), reviewer="R")
        report = cm.report()
        assert report["drift"], "reviewers confirming half of the 0.95 band is drift"
        assert "out of date" in report["plain"]

    def test_the_latest_verdict_per_batch_counts(self):
        cm.record_outcome("B1", 0.9, confirmed=True, reviewer="R")
        cm.record_outcome("B1", 0.9, confirmed=False, reviewer="R")
        assert [r["confirmed"] for r in cm.outcomes()] == [False]
