"""
The single place this engine talks to a model (SDK, credentials, model
names, retries). Only transient failures (429, 503) are retried, tightly; a
400 is our bug and would fail forever. Every path ends in None, never an
exception: an optional enrichment must not take down a reconciliation.
"""

from __future__ import annotations

from typing import Any

import logging
import hashlib
import json
import os
from datetime import datetime, timezone
import random
import time

logger = logging.getLogger(__name__)

# Flash, not Pro. The free tier reports `limit: 0` for Pro models, so they 429
# on every call rather than being merely slower or costlier.
#
# `-latest` rather than a pinned version: dated Gemini models retire and start
# returning 404 to new keys (gemini-2.5-flash already does), which would
# disable these features silently at exactly the wrong moment.
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

# Tried in order when the model before is overloaded or rate-limited (503,
# 429). The free tier's `-latest` aliases return "high demand" for minutes at a
# time, and every model use then fell back to rules in silence; a second model
# answering now beats three backed-off retries of one that will not.
FALLBACK_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_FALLBACK_MODELS",
    "gemini-3-flash-preview,gemini-3.6-flash,gemini-2.5-flash,gemini-flash-lite-latest",
).split(",") if m.strip()]

# Statuses that say "not this model, not now" rather than "your request is
# wrong": overloaded, rate-limited, timed out, or retired for this key (a new
# key gets 404 for gemini-2.5-flash). The next model is tried at once. 400,
# 401 and 403 stop: another model would refuse the same request.
MOVE_ON_STATUS = (404, 408, 429, 499, 500, 502, 503, 504)

# A slow model must not hold a web request past the host's limit (Vercel: 60s).
# Each call gets MODEL_TIMEOUT_S, and one request gets MODEL_REQUEST_BUDGET_S of
# model time in all; past it the deterministic path answers.
CALL_TIMEOUT_S = float(os.environ.get("MODEL_TIMEOUT_S", "12"))

# When every model is busy, the answer a model gave to the IDENTICAL request
# (same system, prompt, schema and attachments) is replayed, and the response
# says so with the time it was given (model_budget.report: replayed_from).
# Whatever uses it still checks it as if it were new. MODEL_REPLAY=0 turns it
# off.
REPLAY = os.environ.get("MODEL_REPLAY", "1").strip() != "0"
_REPLAY_TTL_S = 30 * 24 * 3600
_replay_local: dict[str, dict] = {}


def _replay_key(system, prompt, schema, attachments) -> str:
    h = hashlib.sha256()
    h.update(json.dumps([system or "", prompt or "", schema or {}], sort_keys=True,
                        default=str).encode("utf-8"))
    for data, mime in attachments or []:
        h.update(mime.encode("utf-8"))
        h.update(hashlib.sha256(data).digest())
    return "model:answer:" + h.hexdigest()


def _replay_store():
    import stores  # pylint: disable=import-outside-toplevel
    try:
        return stores.shared_redis()
    except Exception as exc:
        logger.debug("_replay_store: best-effort step skipped (%s: %s)", type(exc).__name__, exc)
        return None


def _remember(key: str, text: str, name: str) -> None:
    if not REPLAY:
        return
    rec = {"text": text, "model": name,
           "at": datetime.now(timezone.utc).isoformat(timespec="minutes")}
    store = _replay_store()
    if store is not None:
        try:
            store.set(key, json.dumps(rec), ex=_REPLAY_TTL_S)
            return
        except Exception as exc:
            logger.debug("_remember: best-effort step skipped (%s: %s)", type(exc).__name__, exc)
    if len(_replay_local) > 500:
        _replay_local.pop(next(iter(_replay_local)))
    _replay_local[key] = rec


def _replay(key: str) -> str | None:
    if not REPLAY:
        return None
    rec = None
    store = _replay_store()
    if store is not None:
        try:
            raw = store.get(key)
            rec = json.loads(raw) if raw else None
        except Exception as exc:
            logger.debug("_replay: best-effort step skipped (%s: %s)", type(exc).__name__, exc)
            rec = None
    rec = rec or _replay_local.get(key)
    if not rec:
        return None
    import model_budget  # pylint: disable=import-outside-toplevel
    state = model_budget.REQUEST.get()
    if state is not None:
        state["model"] = rec.get("model")
        state["replayed_from"] = rec.get("at")
    logger.info("Every model busy; replaying %s's answer to this identical request from %s.",
                rec.get("model"), rec.get("at"))
    return rec.get("text")
REQUEST_BUDGET_S = float(os.environ.get("MODEL_REQUEST_BUDGET_S", "30"))

MAX_ATTEMPTS = 3
BASE_BACKOFF_S = 1.5
RETRYABLE_STATUS = (429, 503, 500, 502, 504)


def answered_model() -> str:
    """The model that answered in this request, else the configured primary."""
    import model_budget  # pylint: disable=import-outside-toplevel
    return (model_budget.REQUEST.get() or {}).get("model") or DEFAULT_MODEL


def _answered_by(name: str) -> None:
    """Record which model answered, so a response never names the wrong one."""
    import model_budget  # pylint: disable=import-outside-toplevel
    state = model_budget.REQUEST.get()
    if state is not None:
        state["model"] = name


def api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def is_configured() -> bool:
    return bool(api_key())


def _status_of(exc: Exception) -> int | None:
    """Best-effort HTTP status from a google-genai error."""
    for attr in ("code", "status_code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    text = str(exc)
    for status in RETRYABLE_STATUS:
        if text.startswith(f"{status} ") or f" {status} " in text[:40]:
            return status
    return None


def generate(
    prompt: str,
    *,
    system: str | None = None,
    schema: dict | None = None,
    model: str | None = None,
    max_output_tokens: int = 4000,
    temperature: float = 0.0,
    thinking_budget: int | None = 0,
    attachments: list[tuple[bytes, str]] | None = None,
) -> str | None:
    """
    Ask the model; returns the text, or None. Never raises. temperature 0 by
    default, so a changed answer means changed data. `attachments` are (bytes,
    mime) pairs such as a scanned statement; nothing returned about them is used
    until code has checked it.
    """
    if not is_configured():
        return None

    # A public demo carries a key, so every call from a web request is
    # metered first; over budget, the caller's deterministic path answers.
    import model_budget  # pylint: disable=import-outside-toplevel
    refused = model_budget.take(len(prompt or "") + len(system or ""))
    if refused:
        logger.info("Model call not made: %s. Deterministic path continues.", refused)
        return None

    try:
        from google import genai
        from google.genai import types
    except Exception as exc:
        # Broader than ImportError deliberately: an import can fail for reasons
        # other than absence (broken install, a transitive dep raising at
        # import time), and none of them justify failing the caller.
        logger.info(
            "LLM unavailable (%s: %s). Deterministic path continues.",
            type(exc).__name__, exc,
        )
        return None

    config_kwargs: dict = {
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
    }

    # Thinking tokens are charged against max_output_tokens, and on a 2.5-series
    # flash model they dominate it: measured 639 thinking tokens to produce an
    # 85-token answer, 88% of the budget spent before a word was written. With a
    # whole settlement report as grounding the model thinks harder still, runs
    # out mid-sentence, and the answer arrives cut off — which is exactly how
    # this surfaced, as Q&A replies that stopped in the middle of a word.
    #
    # Nothing this module does needs a reasoning trace. Agent 0b maps a column
    # name; Agent 9 restates figures it was handed. Both are recall, not
    # deduction. Spend the budget on the answer.
    if thinking_budget is not None and hasattr(types, "ThinkingConfig"):
        try:
            config_kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_budget=thinking_budget
            )
        except Exception as exc:
            # An older SDK, or a model that will not take the field. The call
            # still works; it just thinks, so this must not be fatal.
            logger.debug("generate: best-effort step skipped (%s: %s)", type(exc).__name__, exc)
    if system:
        config_kwargs["system_instruction"] = system
    if schema:
        # Gemini's response_schema accepts a SUBSET of JSON Schema and rejects
        # `additionalProperties` with a 400 naming a field the caller never
        # typed. Schemas passed here must already be Gemini-shaped.
        config_kwargs["response_mime_type"] = "application/json"
        config_kwargs["response_schema"] = schema

    import model_budget as _mb  # pylint: disable=import-outside-toplevel
    state = _mb.REQUEST.get()
    deadline = None
    if state is not None:
        deadline = state.setdefault("model_deadline", time.monotonic() + REQUEST_BUDGET_S)
        if time.monotonic() >= deadline:
            logger.info("Model time for this request is spent. Deterministic path continues.")
            return None
    try:
        client = genai.Client(api_key=api_key(),
                              http_options=types.HttpOptions(timeout=int(CALL_TIMEOUT_S * 1000)))
    except Exception:
        client = genai.Client(api_key=api_key())
    last: Exception | None = None

    primary = model or DEFAULT_MODEL
    chain = [primary] + [m for m in FALLBACK_MODELS if m != primary]
    key = _replay_key(system, prompt, schema, attachments)
    for i, name in enumerate(chain):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if deadline is not None and time.monotonic() >= deadline:
                logger.info("Model time for this request is spent at %s. "
                            "Deterministic path continues.", name)
                return _replay(key)
            try:
                contents: Any = ([types.Part.from_bytes(data=data, mime_type=mime)
                                  for data, mime in attachments] + [prompt]
                                 if attachments else prompt)
                response = client.models.generate_content(
                    model=name,
                    contents=contents,
                    config=types.GenerateContentConfig(**config_kwargs),
                )
                text = (getattr(response, "text", None) or "").strip()

                # A truncated answer must not be returned as if it were whole.
                # finish_reason MAX_TOKENS means the model was cut off, and the
                # caller has no other way to tell a complete short answer from a
                # sentence that stops halfway.
                try:
                    reason = str((response.candidates or [])[0].finish_reason or "")
                except Exception as exc:
                    logger.debug("generate: best-effort step skipped (%s: %s)", type(exc).__name__, exc)
                    reason = ""
                if text and "MAX_TOKENS" in reason:
                    _answered_by(name)
                    _remember(key, text.rstrip() + " […answer truncated]", name)
                    logger.warning(
                        "LLM hit max_output_tokens (%d) and the answer is cut off. "
                        "Returning it flagged rather than silently truncated.",
                        max_output_tokens,
                    )
                    return text.rstrip() + " […answer truncated]"

                if text:
                    _answered_by(name)
                    _remember(key, text, name)
                    return text
                logger.warning("LLM returned an empty response; treating as unavailable.")
                return None

            except Exception as exc:
                last = exc
                status = _status_of(exc)
                timed_out = status is None and any(
                    w in str(exc).lower() for w in ("timed out", "timeout", "deadline"))
                # A 400 from a fallback is that model refusing a parameter it
                # does not take (thinking budget, schema); from the primary it
                # is our request, and every model would refuse it.
                move_on = status in MOVE_ON_STATUS or timed_out or (status == 400 and i > 0)
                if move_on and i + 1 < len(chain):
                    logger.info("LLM %s unavailable (status=%s); trying %s.",
                                name, status, chain[i + 1])
                    break

                if status not in RETRYABLE_STATUS or attempt == MAX_ATTEMPTS:
                    # A 400 is our bug and will fail identically forever — retrying
                    # it burns quota and delays the caller for nothing.
                    logger.warning(
                        "LLM call failed (%s, status=%s) after %d attempt(s): %s. "
                        "Deterministic path continues.",
                        type(exc).__name__, status, attempt, str(exc)[:200],
                    )
                    return _replay(key)

                # Jittered backoff so concurrent workers do not retry in lockstep
                # and re-trigger the same rate limit together.
                delay = BASE_BACKOFF_S * (2 ** (attempt - 1)) + random.uniform(0, 0.4)
                logger.info(
                    "LLM transient failure (status=%s), retrying in %.1fs "
                    "(attempt %d/%d).", status, delay, attempt, MAX_ATTEMPTS,
                )
                time.sleep(delay)

    logger.warning("LLM exhausted retries: %s", str(last)[:200])
    return _replay(key)
