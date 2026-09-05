"""
Agent 1 — Ingestion & Normalization.

Turns disparate source formats (gateway export, bank feed, ERP export)
into the unified NormalizedTxn schema. Deterministic code only — no LLM.
Everything downstream assumes this has already run.

Key jobs:
  1. Amount -> integer cents (never trust source float formatting)
  2. Timestamp -> UTC, with an explicit confidence flag
  3. Reference ID -> canonical truncation-tolerant key
  4. Memo -> normalized string for the fuzzy/semantic pass later
  5. payer_id / payee_id -> canonicalized counterparty keys
  6. AML flags -> is_cash, is_wire_transfer extracted from raw record

TIMEZONE HANDLING
-----------------
When a timestamp carries an explicit tz offset it is converted to UTC and
confidence is HIGH. When it does not, the per-source timezone from
SOURCE_TZ_MAP is applied via zoneinfo and confidence is INFERRED — this is
a best-effort assumption, not a fact, and downstream can filter on it.

If the source is not in SOURCE_TZ_MAP at all (e.g. a new feed not yet
registered), confidence is LOW and UTC is used as a conservative fallback.
LOW-confidence timestamps MUST route to the exception queue, not be
silently matched — a 5.5-hour error (IST vs UTC) will misplace a
transaction outside its settlement window on real Indian bank statements.

DROPPED RECORDS
---------------
normalize_batch previously silently swallowed all failures with a bare
`continue`. This made a file with a broken timestamp column look like a
successful ingestion of an empty pool — no error, no count, no diagnosis.

The new behaviour:
  - Every failure is logged at WARNING with the txn_id (or record index)
    and the exception message.
  - Callers that need structured drop counts use normalize_batch_with_report
    which returns a NormalizationReport alongside the good records.
  - normalize_batch keeps the original list[NormalizedTxn] return type for
    backward compatibility; callers that already count drops by comparing
    before/after lengths keep working unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

import dateutil.parser as dp

from schema import NormalizedTxn, SourceType, TzConfidence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-source timezone registry.
#
# In production this is a versioned config table keyed by actual source
# system ID and effective date — the same feed can change timezone conventions
# across an export format upgrade. For now, one entry per source type covers
# the common case.
#
# IANA tz names are used (not UTC offsets) because India's +05:30 offset is
# not uniquely named and zoneinfo handles DST transitions correctly for feeds
# whose history predates or spans a DST change.
# ---------------------------------------------------------------------------
SOURCE_TZ_MAP: dict[SourceType, str] = {
    SourceType.GATEWAY: "UTC",         # payment gateways typically export UTC
    SourceType.BANK: "Asia/Kolkata",   # IST — standard for Indian bank exports
    SourceType.ERP: "Asia/Kolkata",    # ERP systems in India normally use IST
}

# Below this length, a canonical ref ID is too short to trust as a unique
# identifier. It may still carry signal for linkage tokenisation, but it
# CANNOT be used as the sole basis for an exact-match assertion.
REF_ID_MIN_LEN = 8

# Counterparty ID canonicalization: strip to alphanumeric and uppercase,
# same as ref_id. Keeps "CORP_A" == "CORPA" == "corp a" from looking like
# three different counterparties to the AML scanner.
_COUNTERPARTY_RE = re.compile(r"[^A-Za-z0-9]")


# ---------------------------------------------------------------------------
# Drop report
# ---------------------------------------------------------------------------

@dataclass
class DroppedRecord:
    """Metadata for a record that could not be normalized."""
    record_index: int
    txn_id: str           # best-effort: raw["txn_id"] if present, else ""
    reason: str           # the exception message
    raw_keys: list[str]   # column names present, for diagnosis


@dataclass
class NormalizationReport:
    """
    Returned by normalize_batch_with_report. Keeps the same list of good
    records that normalize_batch returns, plus a structured account of
    everything that was dropped and why.

    Use this when you need to route drops to the exception queue, surface
    ingestion notes to a UI, or compute an honest drop rate rather than
    inferring it from a before/after count comparison.
    """
    normalized: list[NormalizedTxn]
    dropped: list[DroppedRecord] = field(default_factory=list)

    @property
    def drop_count(self) -> int:
        return len(self.dropped)

    @property
    def total_input(self) -> int:
        return len(self.normalized) + self.drop_count

    @property
    def drop_rate(self) -> float:
        if self.total_input == 0:
            return 0.0
        return self.drop_count / self.total_input


# ---------------------------------------------------------------------------
# Amount normalization
# ---------------------------------------------------------------------------

class AmountUnreadable(ValueError):
    """An amount string that cannot be read without guessing."""


# Decoration a real export legitimately carries around a number. None of it
# can change the value, so all of it may be removed.
_AMOUNT_NOISE = re.compile(
    r"""^\s*
        (?:(?:rs|inr|usd|eur|gbp|aud|sgd|aed)\.?\s*)?
        [₹$€£¥]?\s*
        (?P<body>.*?)
        \s*(?:(?:cr|dr)\.?)?\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# What may remain once the decoration is gone. Grouping is allowed in both
# western (1,234,567) and Indian (12,34,567) shapes.
_AMOUNT_STRICT = re.compile(
    r"^[+-]?\d{1,3}(?:,\d{2,3})*(?:\.\d+)?$|^[+-]?\d+(?:\.\d+)?$"
)


def clean_amount_str(value) -> str:
    """
    Reduce an amount to a bare numeric string, or refuse.

    The previous rule here deleted every character that was not a digit, dot,
    comma or minus, then dropped the commas. It is the same "strip it and hope" shape that let file_agent
    read "4,2OO.OO" as a number, and on this path it decides the integer paise
    that subset-sum matches on. Measured against its own docstring:

        "Rs. 1,234.50"  -> RAISED    the '.' in "Rs." survived into ".1234.50"
        "1 234,50"      -> 12345000  100x too large; the docstring claimed
                                     European format was supported
        "4,2OO.OO"      -> 4200      OCR damage read as a number
        "(1,500.00)"    -> 150000    accounting negative, sign dropped

    The first is the function failing the example written directly above it.
    The last turns a debit into a credit.

    A string is returned rather than a float so the caller can hand it to
    Decimal untouched — going through float would reintroduce exactly the
    representation error this module exists to avoid.
    """
    raw = str(value).strip()
    if not raw:
        return ""

    negative_paren = False
    if raw.startswith("(") and raw.endswith(")"):
        negative_paren = True
        raw = raw[1:-1].strip()

    m = _AMOUNT_NOISE.match(raw)
    body = (m.group("body") if m else raw).strip()
    if not body:
        return ""

    # Both separators present and the comma last: "1.234,56" is European for
    # 1234.56 and a typo'd "1,234.56" looks identical. Wrong by 1000x either
    # way it is guessed, so it is refused.
    if "," in body and "." in body and body.rindex(",") > body.rindex("."):
        raise AmountUnreadable(
            f"ambiguous decimal separator in {value!r} — write it as 1234.56"
        )
    if not _AMOUNT_STRICT.match(body):
        raise AmountUnreadable(f"{value!r} is not a plain amount")

    body = body.replace(",", "")
    return f"-{body}" if negative_paren and not body.startswith("-") else body


def normalize_amount_to_cents(raw_amount) -> int:
    """
    Accepts str, int, or float from source data; returns exact integer cents.

    Rules:
      - Parse via Decimal, never via float — float("1234.56") is not exactly
        1234.56 and the rounding error compounds across thousands of records.
      - str inputs may carry currency symbols, thousands separators (western
        or Indian grouping), whitespace and a trailing CR/DR marker, and may
        use accounting parentheses for a negative. Anything else raises rather
        than being coerced into a plausible number — see clean_amount_str.
      - ROUND_HALF_UP: the standard accounting rounding rule. int() truncates
        toward zero, which introduces a systematic -1 cent bias on positive
        amounts and is not acceptable for financial data.
    """
    if isinstance(raw_amount, str):
        raw_amount = clean_amount_str(raw_amount)

    if raw_amount == "" or raw_amount is None:
        raise ValueError(f"Empty amount field after stripping: {raw_amount!r}")

    try:
        d = Decimal(str(raw_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception as exc:
        raise ValueError(f"Cannot parse amount {raw_amount!r} as Decimal: {exc}") from exc

    return int(d * 100)


# ---------------------------------------------------------------------------
# Timestamp normalization
# ---------------------------------------------------------------------------

# ISO-8601 covers the overwhelming majority of what payment feeds emit, and
# `datetime.fromisoformat` parses it in C. `dateutil.parser.parse` is a
# general-purpose tokenizer that walks the string character by character, and
# on a 25,000-row gateway export it accounted for 84% of all normalization
# time — 3.6 of 4.3 seconds — for strings a stricter parser reads instantly.
#
# So: try the strict parser first, fall back to dateutil for anything it does
# not recognise. dateutil still handles every format it handled before; it is
# simply no longer asked about the easy ones. The fallback is what keeps this
# an optimisation rather than a narrowing of what the engine accepts.
_ISO_TRAILING_Z = ("Z", "z")


def _fast_parse(raw_ts: str) -> datetime | None:
    """ISO-8601 via the C parser, or None to defer to dateutil."""
    try:
        # fromisoformat rejects the 'Z' suffix before 3.11; normalising it
        # here keeps the fast path working on every supported version.
        if raw_ts.endswith(_ISO_TRAILING_Z):
            return datetime.fromisoformat(raw_ts[:-1] + "+00:00")
        return datetime.fromisoformat(raw_ts)
    except ValueError:
        return None


def normalize_timestamp(raw_ts: str, source: SourceType) -> tuple[datetime, TzConfidence]:
    """
    Parse a timestamp string and convert to UTC, returning an explicit
    confidence flag so downstream agents know how much to trust the result.

    Confidence levels:
      HIGH     — the source string carried an explicit UTC offset or 'Z'.
                 The conversion is exact; no assumption was made.
      INFERRED — no offset in the source string, but SOURCE_TZ_MAP has a
                 known timezone for this source type. The conversion is a
                 best-effort assumption (could be wrong if the feed's export
                 convention changed). Downstream should not reject these
                 outright but should be aware that a 5.5-hour IST/UTC
                 difference can move a transaction outside its settlement
                 window.
      LOW      — no offset in the source string AND no entry in SOURCE_TZ_MAP
                 for this source. UTC is used as a conservative fallback.
                 These MUST route to the exception queue; do not auto-match
                 them — a wrong timezone is a wrong answer.

    Raises ValueError for timestamps that cannot be parsed at all. The caller
    (normalize_batch) catches this and logs it as a dropped record.
    """
    raw_ts = (raw_ts or "").strip()
    if not raw_ts:
        raise ValueError("Timestamp field is empty or missing.")

    parsed = _fast_parse(raw_ts)
    if parsed is None:
        try:
            parsed = dp.parse(raw_ts)
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"Unparseable timestamp {raw_ts!r}: {exc}") from exc

    # Case 1: explicit tz offset in the source string — just convert.
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc), TzConfidence.HIGH

    # Case 2: no offset — apply the known per-source timezone.
    tz_name = SOURCE_TZ_MAP.get(source)
    if tz_name:
        try:
            from zoneinfo import ZoneInfo  # stdlib ≥ 3.9
        except ImportError:
            # Python 3.8 fallback — install backports.zoneinfo if needed.
            from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

        try:
            tz = ZoneInfo(tz_name)
            localized = parsed.replace(tzinfo=tz)
            return localized.astimezone(timezone.utc), TzConfidence.INFERRED
        except Exception as exc:
            # Bad IANA name in SOURCE_TZ_MAP — log and fall through to LOW.
            logger.error(
                "Bad timezone %r for source %s in SOURCE_TZ_MAP: %s. "
                "Falling back to UTC with LOW confidence.",
                tz_name, source.value, exc,
            )

    # Case 3: source not in SOURCE_TZ_MAP (or ZoneInfo failed).
    # UTC is the conservative fallback; confidence is LOW so the record can
    # be routed to the exception queue rather than silently matched.
    logger.warning(
        "No timezone known for source %s; treating naive timestamp %r as UTC "
        "(LOW confidence). Add this source to SOURCE_TZ_MAP or ensure "
        "the feed exports explicit offsets.",
        source.value, raw_ts,
    )
    return parsed.replace(tzinfo=timezone.utc), TzConfidence.LOW


# ---------------------------------------------------------------------------
# Reference ID normalization
# ---------------------------------------------------------------------------

def normalize_ref_id(raw_ref: str) -> str:
    """
    Canonical key tolerant of bank truncation (e.g. 20-char limits) and
    separator differences (hyphens, underscores, spaces).

    The canonical form is alphanumeric-only and uppercased. This means
    "STL-2026-001", "STL_2026_001", and "STL2026001" all canonicalize to
    "STL2026001" and compare equal — which is the intended behaviour, because
    the same identifier is written differently by different systems.

    SHORT IDs: if the result is shorter than REF_ID_MIN_LEN, the reference
    is probably truncated by the source system. The returned value is still
    usable as a prefix-match key for linkage tokenisation, but it CANNOT
    support an exact-match assertion on its own. The caller records this via
    the `ref_truncated` flag in `extra` (set in normalize_record).
    """
    cleaned = re.sub(r"[^A-Za-z0-9]", "", raw_ref or "").upper()
    return cleaned


def _canonicalize_counterparty(raw: str) -> str:
    """
    Strip non-alphanumeric characters and uppercase, same as normalize_ref_id.

    Ensures "CORP_A", "Corp A", and "corpa" all resolve to "CORPA" so the
    AML scanner and duplicate detector are not confused by formatting noise.
    Empty string is returned as-is; the caller decides whether to treat a
    missing counterparty as LOW confidence or an error.
    """
    return _COUNTERPARTY_RE.sub("", raw or "").upper()


# ---------------------------------------------------------------------------
# Memo normalization
# ---------------------------------------------------------------------------

def normalize_memo(raw_memo: str) -> str:
    """
    Lowercase, whitespace-collapsed form for fuzzy/semantic matching.

    Preserves content — does not remove stop words or punctuation — because
    memo text is thin signal already, and removing words reduces the chance
    of a semantic match without adding precision.
    """
    return re.sub(r"\s+", " ", (raw_memo or "").strip().lower())


# ---------------------------------------------------------------------------
# Single-record normalization
# ---------------------------------------------------------------------------

def normalize_record(raw: dict, source: SourceType) -> NormalizedTxn:
    """
    Normalize one raw source record into the unified NormalizedTxn schema.

    Required fields: txn_id (or ref_id as fallback), amount, timestamp.
    Optional but extracted if present: currency, memo, payer_id, payee_id,
    is_cash, is_wire_transfer.

    All other source-specific fields go into `extra` untouched, so the full
    raw record is preserved in the audit trail even if the engine never uses
    those fields.

    Raises ValueError for any field that cannot be parsed — callers should
    catch this and route the record to the exception queue.
    """
    # ── Identity ──────────────────────────────────────────────────────────
    # txn_id is the primary identifier; ref_id is the settlement-linkage key.
    # File Agent may have already mirrored one from the other for rows that
    # only have one of the two — accept either as a fallback.
    txn_id = str(raw.get("txn_id") or raw.get("ref_id") or "").strip()
    if not txn_id:
        raise ValueError(
            "Record has no txn_id or ref_id — cannot be identified or audited."
        )

    # ── Reference ID ──────────────────────────────────────────────────────
    raw_ref = str(raw.get("ref_id") or raw.get("txn_id") or "").strip()
    ref_canonical = normalize_ref_id(raw_ref)
    ref_truncated = 0 < len(ref_canonical) < REF_ID_MIN_LEN

    # ── Amount ────────────────────────────────────────────────────────────
    amount_cents = normalize_amount_to_cents(raw["amount"])

    # ── Timestamp ─────────────────────────────────────────────────────────
    ts_utc, tz_conf = normalize_timestamp(str(raw["timestamp"]), source)

    # ── AML / Compliance fields ───────────────────────────────────────────
    # Canonicalize counterparty IDs so "CORP_A" and "corp a" don't look
    # like two different payers to the AML scanner.
    payer_id = _canonicalize_counterparty(str(raw.get("payer_id") or ""))
    payee_id = _canonicalize_counterparty(str(raw.get("payee_id") or ""))

    # is_cash / is_wire_transfer: accept bool, int (1/0), or strings
    # ("true"/"false", "yes"/"no", "1"/"0").
    def _bool_field(val) -> bool:
        if isinstance(val, bool):
            return val
        if isinstance(val, int):
            return bool(val)
        return str(val).strip().lower() in {"true", "yes", "1"}

    is_cash = _bool_field(raw.get("is_cash", False))
    is_wire = _bool_field(raw.get("is_wire_transfer", False))

    # ── Extra (audit trail preservation) ─────────────────────────────────
    # Everything not consumed above goes into extra, so the full source
    # record is preserved. ref_truncated is added here — downstream agents
    # (especially linkage) can check it before using the ref as an anchor.
    known_keys = {
        "txn_id", "ref_id", "amount", "currency", "timestamp", "memo",
        "payer_id", "payee_id", "is_cash", "is_wire_transfer",
    }
    extra: dict = {k: v for k, v in raw.items() if k not in known_keys}
    if ref_truncated:
        extra["ref_truncated"] = True
        extra["ref_canonical_len"] = len(ref_canonical)

    return NormalizedTxn(
        source=source,
        source_txn_id=txn_id,
        ref_id_canonical=ref_canonical,
        amount_cents=amount_cents,
        currency=str(raw.get("currency") or "INR").strip().upper(),
        currency_stated=bool(str(raw.get("currency") or "").strip()),
        timestamp_utc=ts_utc,
        tz_confidence=tz_conf,
        memo_raw=str(raw.get("memo") or ""),
        memo_normalized=normalize_memo(str(raw.get("memo") or "")),
        payer_id=payer_id,
        payee_id=payee_id,
        is_cash=is_cash,
        is_wire_transfer=is_wire,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Batch normalization
# ---------------------------------------------------------------------------

def normalize_batch(
    raw_records: list[dict],
    source: SourceType,
) -> list[NormalizedTxn]:
    """
    Normalize a list of raw records, logging every failure.

    Returns only the successfully normalized records — the same contract as
    before. Every record that could not be normalized is logged at WARNING
    with its index, best-effort txn_id, and the failure reason, so a broken
    timestamp column no longer produces a silent empty result.

    For structured drop counts or routing drops to the exception queue, use
    normalize_batch_with_report instead. This function is kept for backward
    compatibility with all existing callers.
    """
    report = normalize_batch_with_report(raw_records, source)
    return report.normalized


def normalize_batch_with_report(
    raw_records: list[dict],
    source: SourceType,
) -> NormalizationReport:
    """
    Normalize a list of raw records and return a full NormalizationReport.

    Use this when you need:
      - A structured list of every dropped record with its reason (for routing
        to the exception queue rather than just counting the loss).
      - An honest drop rate to surface in audit notes or a UI warning.
      - Diagnosis of systemic failures (e.g. all records dropped on timestamp
        parse → the timestamp column is broken, not individual records).

    The returned NormalizationReport.normalized is identical to what
    normalize_batch returns.
    """
    normalized: list[NormalizedTxn] = []
    dropped: list[DroppedRecord] = []

    for i, raw in enumerate(raw_records):
        # Best-effort txn_id for the log message — the record may not have one.
        txn_id_hint = str(raw.get("txn_id") or raw.get("ref_id") or "")

        try:
            normalized.append(normalize_record(raw, source))
        except (ValueError, KeyError, TypeError) as exc:
            reason = str(exc)
            logger.warning(
                "Agent 1 [%s] dropped record #%d (txn_id=%r): %s",
                source.value, i, txn_id_hint or "<no id>", reason,
            )
            dropped.append(DroppedRecord(
                record_index=i,
                txn_id=txn_id_hint,
                reason=reason,
                raw_keys=list(raw.keys()),
            ))

    if dropped:
        drop_rate = len(dropped) / len(raw_records) if raw_records else 0.0
        logger.warning(
            "Agent 1 [%s]: %d of %d records dropped (%.1f%%). "
            "If the drop rate is high, check whether the timestamp or amount "
            "column was correctly mapped — an unmapped column silently drops "
            "every row rather than surfacing a parse error.",
            source.value, len(dropped), len(raw_records), drop_rate * 100,
        )

    return NormalizationReport(normalized=normalized, dropped=dropped)
