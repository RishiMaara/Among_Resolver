"""
A public demo can carry a model key only if nobody can spend it freely.

What these pin: each visitor has an hourly allowance and the server a daily
one; an oversized prompt is not sent; scripts outside a web request are not
metered; the key never appears in what the engine returns; and when a call is
skipped the response says so instead of passing a rules answer off as a
model's.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import llm_provider
import main
import model_budget


@pytest.fixture(autouse=True)
def fresh_counts(monkeypatch):
    monkeypatch.setattr(model_budget, "_local", {})
    monkeypatch.setattr(model_budget, "_store", lambda: None)
    model_budget.REQUEST.set(None)


class TestTheLimits:
    def test_each_visitor_has_an_hourly_allowance(self, monkeypatch):
        monkeypatch.setenv("MODEL_CALLS_PER_CLIENT_PER_HOUR", "2")
        model_budget.begin_request("203.0.113.7")
        assert model_budget.take() is None and model_budget.take() is None
        assert "visitor" in model_budget.take()
        # Someone else still has theirs.
        model_budget.begin_request("198.51.100.9")
        assert model_budget.take() is None

    def test_the_server_has_a_daily_allowance(self, monkeypatch):
        monkeypatch.setenv("MODEL_CALLS_PER_DAY", "3")
        for ip in ("a", "b", "c"):
            model_budget.begin_request(ip)
            assert model_budget.take() is None
        model_budget.begin_request("d")
        assert "today" in model_budget.take()

    def test_an_oversized_prompt_is_not_sent(self, monkeypatch):
        monkeypatch.setenv("MODEL_MAX_PROMPT_CHARS", "100")
        model_budget.begin_request("203.0.113.7")
        assert "characters" in model_budget.take(prompt_chars=101)

    def test_scripts_outside_a_request_are_not_metered(self, monkeypatch):
        monkeypatch.setenv("MODEL_CALLS_PER_DAY", "0")
        assert model_budget.take() is None


class FakeModels:
    calls = 0

    def generate_content(self, **_kw):
        FakeModels.calls += 1
        return SimpleNamespace(text='{"results": []}',
                               candidates=[SimpleNamespace(finish_reason="STOP")])


class FakeClient:
    def __init__(self, **_kw):
        self.models = FakeModels()


class TestThroughTheApi:
    @pytest.fixture
    def live(self, monkeypatch):
        pytest.importorskip("google.genai")
        monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key-4f1c")
        monkeypatch.setattr("google.genai.Client", FakeClient)
        FakeModels.calls = 0
        return TestClient(main.app)

    def test_status_says_what_is_live_and_never_shows_the_key(self, live):
        body = live.get("/ai/status").json()
        assert body["live"] and body["decides_membership"] is False
        assert "not-a-real-key" not in str(body)

    def test_a_visitor_over_budget_gets_rules_and_is_told(self, live, monkeypatch):
        monkeypatch.setenv("MODEL_CALLS_PER_CLIENT_PER_HOUR", "1")
        ask = {"narrations": ["NEFT CR UTIB0000123456 RAZORPAY SETTLEMENT"], "use_model": True}
        first = live.post("/narrations/read", json=ask).json()
        second = live.post("/narrations/read", json=ask).json()
        assert first["ai"]["model_calls"] == 1 and first["ai"]["skipped"] is None
        assert second["ai"]["model_calls"] == 0 and "visitor" in second["ai"]["skipped"]
        assert FakeModels.calls == 1, "the second call was never made"

    def test_without_a_key_nothing_is_live(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setattr(llm_provider, "is_configured", lambda: False)
        body = TestClient(main.app).get("/ai/status").json()
        assert body["live"] is False and body["model"] is None
