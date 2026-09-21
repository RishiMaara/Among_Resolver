"""
Reading bank narrations, and the rule that keeps a model honest about them.

Every model path here is mocked. The measured comparison — regex 82.7% on
formats it was not written for, grounded model-first 97.5% — comes from
scripts/narration_eval.py against a real model and is in
docs/benchmarks/narration_eval.json; these tests pin the rules, not the model.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import llm_provider
import main
import narration_reader as nr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import narration_eval  # noqa: E402

HDFC = "NEFT CR-YESB0000123-RAZORPAY SOFTWARE PVT LTD-SETTL setl_Kx92abcd1234ef-YESBN12026090112"


@pytest.fixture
def model(monkeypatch):
    """A stand-in model that answers with whatever the test hands it."""
    replies = []
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    monkeypatch.setattr(llm_provider, "generate", lambda *a, **k: replies.pop(0) if replies else None)
    return replies


@pytest.fixture
def no_model(monkeypatch):
    monkeypatch.setattr(llm_provider, "is_configured", lambda: False)


class TestTheRegexReader:
    def test_it_reads_a_known_format(self):
        r = nr.regex_read(HDFC)
        assert r.rail == "NEFT"
        assert r.utr == "YESBN12026090112"
        assert r.settlement_ref == "setl_Kx92abcd1234ef"
        assert r.counterparty == "RAZORPAY SOFTWARE PVT LTD"

    def test_an_ifsc_is_not_a_utr(self):
        assert nr.regex_read("NEFT CR-YESB0000123-SHARMA TRADERS").utr is None

    def test_a_twelve_digit_rrn(self):
        assert nr.regex_read("UPI/624418889120/ANJALI MEHTA/anjali@okhdfc").utr == "624418889120"


class TestGrounding:
    def test_case_and_separators_do_not_matter(self):
        assert nr.grounded("setl_KX92", "neft settl setl-kx92 x")

    def test_a_value_not_in_the_text_is_not_grounded(self):
        assert not nr.grounded("YESBN99999999999", HDFC)


class TestTheModelIsHeldToTheText:
    def test_an_invented_utr_is_dropped_and_counted(self, model):
        model.append(json.dumps([{"i": 0, "utr": "YESBN99999999999", "rail": "NEFT",
                                  "counterparty": "RAZORPAY SOFTWARE PVT LTD"}]))
        stats = {}
        out = nr.llm_read([HDFC], stats)
        assert out[0].utr is None, "a UTR that is not in the narration is not evidence"
        assert out[0].counterparty == "RAZORPAY SOFTWARE PVT LTD"
        assert stats["ungrounded"] == 1

    def test_a_rail_the_narration_does_not_name_is_dropped(self, model):
        model.append(json.dumps([{"i": 0, "rail": "RTGS"}]))
        stats = {}
        assert nr.llm_read([HDFC], stats)[0].rail is None
        assert stats["ungrounded"] == 1

    def test_garbage_from_the_model_falls_back_to_rules(self, model):
        model.append("this is not json")
        rows = nr.read([HDFC], use_llm=True)
        assert rows[0]["utr"] == "YESBN12026090112"
        assert rows[0]["source"]["utr"] == "regex"

    def test_the_grounded_model_answer_comes_first(self, model):
        """Measured best: the regex's wrong counterparty leaves no gap to fill."""
        text = "NEFT INWARD/KKBKN12345678901/KAVERI ENTERPRISES/PAYMENT"
        model.append(json.dumps([{"i": 0, "counterparty": "KAVERI ENTERPRISES"}]))
        row = nr.read([text], use_llm=True)[0]
        assert row["counterparty"] == "KAVERI ENTERPRISES" and row["source"]["counterparty"] == "model"
        assert row["source"]["utr"] == "regex", "what the model left empty the regex fills"

    def test_without_a_model_it_is_the_regex(self, no_model):
        row = nr.read([HDFC], use_llm=True)[0]
        assert set(v for v in row["source"].values() if v) == {"regex"}


class TestTheEndpoint:
    def test_regex_by_default(self, no_model):
        c = TestClient(main.app)
        body = c.post("/narrations/read", json={"narrations": [HDFC]}).json()
        assert body["reader"] == "regex"
        assert body["results"][0]["utr"] == "YESBN12026090112"

    def test_asking_for_a_model_that_is_not_there_says_so(self, no_model):
        c = TestClient(main.app)
        body = c.post("/narrations/read", json={"narrations": [HDFC], "use_model": True}).json()
        assert body["model_requested_but_unavailable"] is True


class TestTheMeasuredBaseline:
    def test_the_regex_figures_in_the_docs_are_the_real_ones(self):
        """Deterministic and free: the regex half of the published comparison."""
        items = narration_eval.build(120, 20260921)
        scored = narration_eval.score(items, [nr.regex_read(it["text"]).__dict__ for it in items])
        assert scored["dev"]["mean_accuracy"] == 0.8854
        assert scored["held_out"]["mean_accuracy"] == 0.8271

    def test_the_published_model_run_beats_the_regex_on_unseen_formats(self):
        path = Path(__file__).resolve().parents[1] / "docs" / "benchmarks" / "narration_eval.json"
        d = json.loads(path.read_text(encoding="utf-8"))
        assert d["model_then_regex"]["held_out"]["mean_accuracy"] > d["regex"]["held_out"]["mean_accuracy"]


def test_statement_rows_carry_what_the_narration_says():
    import statement_parsers as sp
    body = (Path(__file__).resolve().parents[2] / "public" / "sample-data" / "statements"
            / "statement.mt940").read_bytes()
    st, _ = sp.parse(body, "statement.mt940")
    row = next(r for r in sp.to_rows(st) if r["ref_id"] == "UTR20260901001")
    assert row["narration_rail"] == "NEFT"
    assert "RAZORPAY" in row["narration_counterparty"]
