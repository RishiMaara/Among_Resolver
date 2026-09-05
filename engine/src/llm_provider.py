"""
The single place this engine talks to an LLM.

Everything provider-specific lives here — SDK, credentials, model names, retry
policy. `llm_header_mapper` and `settlement_qa` call `generate()` and know
nothing else, so changing provider means changing this file and no other.

WHY RETRY IS NOT OPTIONAL HERE
------------------------------
Measured against a live free-tier key, three distinct failures appeared within
a few minutes of each other:

    400 INVALID_ARGUMENT   response_schema rejected `additionalProperties`
    429 RESOURCE_EXHAUSTED `limit: 0` — the free tier grants Pro models zero
                           requests, so gemini-pro-latest fails every call
    503 UNAVAILABLE        "high demand", transient

Only the last two are worth retrying. A 400 is a bug in our request and will
fail identically forever; retrying it wastes the user's quota and delays an
ingestion that could have completed on rules. So retry is restricted to
transient statuses, capped tightly, and every path still ends in "return None"
rather than an exception — an optional enrichment must never take down a
reconciliation the deterministic engine can finish on its own.
"""

from __future__ import annotations

import logging
import os
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

MAX_ATTEMPTS = 3
BASE_BACKOFF_S = 1.5
RETRYABLE_STATUS = (429, 503, 500, 502, 504)


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
) -> str | None:
    """
    Ask the model. Returns the response text, or None if unavailable.

    Never raises. Callers are enrichment paths on the ingestion and reporting
    routes, and neither may fail because an optional model call did.

    temperature defaults to 0: the same question about the same recorded
    results should not produce a different answer each time it is asked. A
    reviewer comparing two runs is entitled to read a difference as the DATA
    having changed.
    """
    if not is_configured():
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
        except Exception:
            # An older SDK, or a model that will not take the field. The call
            # still works; it just thinks, so this must not be fatal.
            pass
    if system:
        config_kwargs["system_instruction"] = system
    if schema:
        # Gemini's response_schema accepts a SUBSET of JSON Schema and rejects
        # `additionalProperties` with a 400 naming a field the caller never
        # typed. Schemas passed here must already be Gemini-shaped.
        config_kwargs["response_mime_type"] = "application/json"
        config_kwargs["response_schema"] = schema

    client = genai.Client(api_key=api_key())
    last: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(
                model=model or DEFAULT_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            text = (getattr(response, "text", None) or "").strip()

            # A truncated answer must not be returned as if it were whole.
            # finish_reason MAX_TOKENS means the model was cut off, and the
            # caller has no other way to tell a complete short answer from a
            # sentence that stops halfway.
            try:
                reason = str(response.candidates[0].finish_reason or "")
            except Exception:
                reason = ""
            if text and "MAX_TOKENS" in reason:
                logger.warning(
                    "LLM hit max_output_tokens (%d) and the answer is cut off. "
                    "Returning it flagged rather than silently truncated.",
                    max_output_tokens,
                )
                return text.rstrip() + " […answer truncated]"

            if text:
                return text
            logger.warning("LLM returned an empty response; treating as unavailable.")
            return None

        except Exception as exc:
            last = exc
            status = _status_of(exc)

            if status not in RETRYABLE_STATUS or attempt == MAX_ATTEMPTS:
                # A 400 is our bug and will fail identically forever — retrying
                # it burns quota and delays the caller for nothing.
                logger.warning(
                    "LLM call failed (%s, status=%s) after %d attempt(s): %s. "
                    "Deterministic path continues.",
                    type(exc).__name__, status, attempt, str(exc)[:200],
                )
                return None

            # Jittered backoff so concurrent workers do not retry in lockstep
            # and re-trigger the same rate limit together.
            delay = BASE_BACKOFF_S * (2 ** (attempt - 1)) + random.uniform(0, 0.4)
            logger.info(
                "LLM transient failure (status=%s), retrying in %.1fs "
                "(attempt %d/%d).", status, delay, attempt, MAX_ATTEMPTS,
            )
            time.sleep(delay)

    logger.warning("LLM exhausted retries: %s", str(last)[:200])
    return None
