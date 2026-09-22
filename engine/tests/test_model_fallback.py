"""
An overloaded model hands the call to the next one, and says who answered.

The free tier's `-latest` aliases return 503 "high demand" for minutes at a
time. Every model use then fell back to rules without a word, which on a live
demo looks like the AI doing nothing. These pin the fallback: overloaded moves
on at once, a 400 does not (it is our bug), and the response names the model
that actually answered.
"""
import sys
import types as pytypes

import llm_provider
import model_budget


class _Err(Exception):
    def __init__(self, code):
        super().__init__(f"{code} ERROR")
        self.code = code


def _install(monkeypatch, answers):
    calls = []

    class Models:
        def generate_content(self, model, contents, config):
            calls.append(model)
            a = answers[model]
            if isinstance(a, Exception):
                raise a
            return pytypes.SimpleNamespace(
                text=a, candidates=[pytypes.SimpleNamespace(finish_reason="STOP")])

    class Client:
        def __init__(self, api_key):
            self.models = Models()

    types_mod = pytypes.SimpleNamespace(
        GenerateContentConfig=lambda **kw: kw,
        ThinkingConfig=lambda **kw: kw,
        Part=pytypes.SimpleNamespace(from_bytes=lambda **kw: kw),
    )
    genai = pytypes.SimpleNamespace(Client=Client, types=types_mod)
    google = pytypes.ModuleType("google")
    google.genai = genai
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm_provider.time, "sleep", lambda s: None)
    return calls


def _chain():
    primary = llm_provider.DEFAULT_MODEL
    return [primary] + [m for m in llm_provider.FALLBACK_MODELS if m != primary]


def test_an_overloaded_model_hands_the_call_to_the_next(monkeypatch):
    primary, fallback = _chain()[:2]
    calls = _install(monkeypatch, {primary: _Err(503), fallback: '{"ok": true}'})
    token = model_budget.REQUEST.set({"client": "t", "calls": 0, "skipped": ""})
    try:
        assert llm_provider.generate("q") == '{"ok": true}'
        # Straight to the next model: no backed-off retries of an overloaded one.
        assert calls == [primary, fallback]
        assert model_budget.report()["model"] == fallback
    finally:
        model_budget.REQUEST.reset(token)


def test_a_bad_request_is_not_handed_on(monkeypatch):
    primary = _chain()[0]
    calls = _install(monkeypatch, {m: _Err(400) for m in _chain()})
    assert llm_provider.generate("q") is None
    assert calls == [primary]


def test_every_model_overloaded_ends_on_the_rules(monkeypatch):
    chain = _chain()
    calls = _install(monkeypatch, {m: _Err(503) for m in chain})
    assert llm_provider.generate("q") is None
    assert calls[:len(chain)] == chain
