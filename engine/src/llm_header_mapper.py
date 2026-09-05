"""
Agent 0b — LLM-assisted header mapping.

WHY THIS EXISTS
---------------
Agent 0's fuzzy matcher works off a hand-maintained synonym list, and a list
cannot keep up with the world. Every bank, processor and ERP invents its own
column names, and the failure is silent: an unrecognised timestamp column
does not error, it empties the field, and the rows are discarded downstream.
Measured on the ReconRiver dataset, `occurred_at` and `booked_at` were not in
the list and 107 of 207 records disappeared. The fix at the time was to add
two more synonyms — which fixes that dataset and not the next one.

Semantic understanding is the right tool for this specific problem, and only
this one. Deciding that "booked_at" is a date is a language question. Deciding
which transactions make up a settlement is a money question, and stays
deterministic.

THE SAFETY PROPERTY
-------------------
The model PROPOSES; the deterministic layer DECIDES. Specifically:

  * It only runs when the deterministic mapper has already failed to find a
    required field. A working rule-based mapping is never overridden — it is
    free, instant, reproducible and auditable, and those are worth more than a
    marginally better guess.

  * Its output is filtered through the same structural constraints as any
    other mapping: fee/tax/discount columns still cannot become `amount`,
    single-valued targets still take exactly one column, unknown field names
    are dropped. The model cannot talk its way past a rule.

  * Its output then goes through the SAME `_validate_schema` gate. If the
    model's mapping is still missing a required field, the file is rejected
    exactly as it would have been.

  * Every LLM-derived mapping is flagged `llm_assisted=True` so it is visible
    in the audit trail rather than indistinguishable from a deterministic one.

So the worst case is that this module wastes a few cents and changes nothing.
It cannot cause a wrong reconciliation on its own.

PRIVACY
-------
Column names alone are often ambiguous; sample VALUES are what disambiguate
"2026-01-02T23:27:38Z" from "16775.23". That means sending a few real cell
values to an external API, which is a genuine consideration for financial
data. So: at most 3 values per column, each truncated, and the whole feature
is off unless GEMINI_API_KEY is configured. Set LLM_HEADER_MAPPING=0 to
disable it even when it is.

PROVIDER
--------
Google Gemini via the google-genai SDK. The provider is an implementation
detail of this module: everything outside it sees only propose_mapping, and
swapping providers means changing this file alone.
"""

from __future__ import annotations

import json
import logging
import os
import difflib
from typing import Iterable

import llm_provider

logger = logging.getLogger(__name__)

# Flash, not Pro. The free tier grants `limit: 0` requests for Pro models, so
# gemini-pro-latest returns 429 RESOURCE_EXHAUSTED on every call rather than
# being merely slower or costlier. `-latest` rather than a pinned version:
# and start returning 404 to new keys (gemini-2.5-flash already does), which
# would silently disable this fallback exactly when someone relied on it.
MODEL = os.environ.get("GEMINI_HEADER_MODEL", "gemini-flash-latest")
MAX_SAMPLE_ROWS = 3
MAX_VALUE_CHARS = 40


def is_enabled() -> bool:
    """
    On only when credentials exist and it has not been explicitly disabled.

    Off-by-default-without-credentials is the right posture for a finance
    system: the module sends sample cell values to an external service, and
    that should never happen as a silent side effect of running the pipeline.
    """
    if os.environ.get("LLM_HEADER_MAPPING", "").strip() == "0":
        return False
    return llm_provider.is_configured()


def _sample_values(headers: list[str], rows: Iterable[dict], limit: int = MAX_SAMPLE_ROWS):
    """A few real values per column — truncated, because the model needs the
    shape of the data, not the data."""
    samples: dict[str, list[str]] = {h: [] for h in headers}
    for row in rows:
        if all(len(v) >= limit for v in samples.values()):
            break
        for h in headers:
            if len(samples[h]) >= limit:
                continue
            val = str(row.get(h, "") or "").strip()
            if val:
                samples[h].append(val[:MAX_VALUE_CHARS])
    return samples


def _build_prompt(headers: list[str], samples: dict[str, list[str]],
                  target_fields: list[str], missing: list[str]) -> str:
    lines = [
        "Map each column of this financial data file to one of our schema fields.",
        "",
        "Schema fields:",
        "  txn_id    — the transaction's own unique identifier in its source system",
        "  ref_id    — a reference to something else: an order id, a settlement or",
        "              batch id, a UTR, a customer reference. MULTIPLE columns may map",
        "              here; settlement/batch identifiers are especially valuable.",
        "  amount    — the VALUE of the transaction. Never a fee, tax, commission,",
        "              discount or running balance; those are deductions or derived",
        "              figures, not the transaction value.",
        "  currency  — ISO currency code",
        "  timestamp — when the transaction occurred, was booked, or was posted",
        "  memo      — free-text description or narration",
        "  ignore    — anything that fits none of the above",
        "",
        f"Our rule-based matcher could not identify: {', '.join(missing)}.",
        "That is why you are being asked. Look at the sample VALUES, not just the",
        "column names — a column called 'value_date' holding '2026-01-02' is a",
        "timestamp, not an amount.",
        "",
        "Columns:",
    ]
    for h in headers:
        vals = samples.get(h) or []
        shown = ", ".join(repr(v) for v in vals) if vals else "(no non-empty values)"
        lines.append(f"  {h}  ->  samples: {shown}")
    lines += [
        "",
        "Map every column. Use 'ignore' rather than forcing a poor fit.",
        "Exactly one column for txn_id, amount, currency and timestamp; ref_id may",
        "take several. If genuinely no column fits a field, simply do not use it —",
        "the file will be rejected downstream, which is the correct outcome.",
    ]
    return "\n".join(lines)


# Gemini's response_schema accepts a SUBSET of JSON Schema. `additionalProperties`
# is rejected outright with a 400 — worth knowing, because that keyword is
# reflexive when writing a strict schema and the error names a field you never
# typed ("generation_config.response_schema").
_SCHEMA = {
    "type": "object",
    "properties": {
        "mappings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "column": {"type": "string"},
                    "field": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["column", "field", "reason"],
            },
        }
    },
    "required": ["mappings"],
}


def propose_mapping(
    headers: list[str],
    rows: Iterable[dict],
    target_fields: list[str],
    missing_fields: list[str],
) -> tuple[dict[str, str], list[str]] | None:
    """
    Ask the model to map these columns. Returns (mapping, notes) or None.

    None means "unavailable or failed" — never an exception. This runs on the
    ingestion path, and a network hiccup or a missing SDK must degrade to the
    deterministic result, not take down a reconciliation.
    """
    
    # 1. Local Fallback bypass: check if we can resolve it with difflib
    # before burning an API call.
    local_mapping = {}
    local_notes = []
    for missing in missing_fields:
        matches = difflib.get_close_matches(missing, headers, n=1, cutoff=0.7)
        if matches:
            local_mapping[matches[0]] = missing
            local_notes.append(f"{matches[0]} -> {missing}: Resolved via local string similarity bypass")
    
    if local_mapping and len(local_mapping) == len(missing_fields):
        # We resolved all missing fields locally! Bypass the LLM entirely.
        logger.info("LLM bypass triggered: resolved all missing headers locally.")
        return local_mapping, local_notes

    if not is_enabled():
        # If disabled, return whatever local matches we found even if partial
        return (local_mapping, local_notes) if local_mapping else None

    samples = _sample_values(headers, rows)
    prompt = _build_prompt(headers, samples, target_fields, missing_fields)

    text = llm_provider.generate(prompt, schema=_SCHEMA, model=MODEL, max_output_tokens=4000)
    if text is None:
        return None

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("LLM header mapping returned unusable output: %s", e)
        return None

    mapping: dict[str, str] = {}
    notes: list[str] = []
    header_set = set(headers)
    valid = set(target_fields)

    for item in payload.get("mappings", []):
        col = item.get("column")
        fld = item.get("field")
        if col not in header_set or fld in (None, "ignore"):
            continue
        if fld not in valid:
            notes.append(f"model proposed unknown field {fld!r} for {col!r}; ignored")
            continue
        mapping[col] = fld
        notes.append(f"{col} -> {fld}: {item.get('reason', '')}".strip())

    return mapping, notes
