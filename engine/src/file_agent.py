"""
Agent 0 — File Understanding Agent

Sits at the very front of the pipeline. Its job is to take raw, unpredictable
data files (CSV or JSON) and map them into the predictable `TxnIn` schema.

Uses rule-based fuzzy matching to understand column headers and scrubs
messy data (like formatted numbers) so the rest of the engine never has to
deal with data quality issues.
"""

from __future__ import annotations
import csv
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Any

from rapidfuzz import process, fuzz
import dateutil.parser as dateparser

# Shared with ingestion so the two stages agree on what parses.
from ingestion import _fast_parse, clean_amount_str, AmountUnreadable

import llm_header_mapper

logger = logging.getLogger(__name__)

# The target schema fields we need to map to
# The six fields reconciliation needs, plus the four the COMPLIANCE rules
# need. Those four were missing, and the effect was not subtle: ingestion
# reads payer_id, NormalizedTxn carries it, and compliance_agent groups by it
# — but Agent 0 never passed the column through, so every uploaded file
# arrived with payer_id empty. Compliance then grouped all 41 payments of a
# real merchant day under one "payer" (the batch id, its fallback), which is
# why a file containing a deliberate duplicate charge, a four-order
# structuring pattern and a Rs 12.5 lakh CTR-threshold payment produced
# exactly zero findings.
#
# Rules that cannot see a counterparty cannot do their job. SANCTIONS_HIT is
# published as statutory and BLOCKED, and with no payer or payee to screen it
# could never fire on an uploaded file at all.
TARGET_FIELDS = ["txn_id", "ref_id", "amount", "currency", "timestamp", "memo", "status",
                 "payer_id", "payee_id", "is_cash", "is_wire_transfer"]

# Known synonyms to help the fuzzy matcher anchor correctly.
#
# NOTE: "value" was removed from amount's synonyms. fuzz.token_set_ratio
# scores ANY token-subset match as a perfect 100 -- so a column literally
# named "Value Date" (tokens {value, date}) was matching amount's "value"
# synonym at 100, tied with timestamp's "date" synonym at 100, with amount
# winning the tie purely because it's checked earlier in TARGET_FIELDS.
# This silently corrupted the amount field with a date fragment and left
# timestamp empty, which caused 100% of the affected source's rows to be
# dropped downstream (empty timestamp fails to parse). Single generic
# words like "value" are exactly the kind of synonym that's dangerous
# under a subset-based fuzzy scorer -- prefer specific multi-word phrases.
HEADER_SYNONYMS = {
    "txn_id": [
        "transaction_id", "txn_id", "id", "payment_id", "bank_ref_num", "utr",
        "voucher_no", "voucher no", "doc_no", "belnr", "entry_id", "sr no",
        "srno", "serial", "charge", "charge_id",
    ],
    # settlement_batch_id / batch_id are listed explicitly because they are the
    # single most valuable signal the engine has: linkage anchors a transaction
    # to its settlement through this field. Left to the generic "id" synonym
    # they were being claimed by txn_id and dropped.
    "ref_id": [
        "reference_id", "ref_id", "order_id", "description", "reference",
        "rzp_ref", "txnid", "settlement_batch_id", "batch_id", "merchant_order_id",
        "settlement_id",
        # Bank statements. `bank_ref_num` was missing in every spelling, so a
        # statement naming its settlement in the column banks actually use
        # reached linkage with NO reference at all — the file was accepted,
        # "no required field absent", and then withheld for lack of evidence
        # it had supplied. Found by an edge case, not by a unit test.
        "bank_ref_num", "bank_reference", "bank_ref", "bank_reference_number",
        # NOT utr/utr_number: a UTR is the transaction identifier, and
        # listing it here pulled it away from txn_id.
        "rrn", "arn", "payout_id", "settlement_ref",
        "transaction_reference", "customer_reference", "end_to_end_id",
        "transactionreferences", "transactionattributes", "transaction references", "transaction attributes",
    ],
    # Widened after measuring rejection on plausible exports: of eight
    # realistic column schemes (Razorpay, Stripe, SBI, Tally, SAP, PayPal, a
    # generic ledger, a UPI statement), four were refused outright — a coin
    # flip for anyone uploading their own file. Names are cheap to add and
    # each one removes a rejection.
    # Widened after measuring rejection on plausible exports — but NOT with
    # "gross" or "net", and that exclusion is the important part.
    #
    # Adding them looked harmless and silently flipped ReconRiver's processor
    # feed from net_amount to gross_amount, taking it from 94.59% to 8.11%.
    # A feed carrying BOTH gross_amount and net_amount poses a real modelling
    # question — which figure does the settlement credit equal? — and a
    # synonym list must not answer it by alphabetical accident. Where only one
    # numeric column exists, value inference maps it anyway; where several do,
    # the choice stays with the name rules that were tuned against real feeds.
    "amount": [
        "amount", "net_amount", "credit", "debit", "amt", "amount (inr)",
        "value", "transaction_amount", "txn_amount", "paid_amount",
        "settled_amount", "wrbtr", "amount_inr", "price",
    ],
    "currency": ["currency", "ccy", "curr", "currency_code", "waers"],
    # "occurred_at" / "booked_at" / "event_time" were added after testing
    # against the third-party ReconRiver dataset, where their absence silently
    # dropped 107 of 207 records (52%): a row whose timestamp does not map
    # ends up with an empty timestamp, which fails to parse in
    # ingestion.normalize_batch and vanishes. This is the same failure mode as
    # the "Value Date" bug above, and it is the one to watch for whenever a
    # new feed is onboarded — an unmapped timestamp column does not error, it
    # deletes the source.
    "payer_id": [
        "payer_id", "payer", "customer_id", "customer", "from_account",
        "sender", "sender_id", "originator", "debtor", "payer_name",
        "remitter", "buyer_id", "contact_id", "vpa",
        # SAP. wrbtr and budat were already mapped for amount and date, so a
        # SAP feed parsed cleanly and then reached compliance with no
        # counterparty at all — every payer-grouped rule (structuring,
        # velocity, concentration) had nothing to group on, and sanctions
        # screening had no name to screen.
        "lifnr",   # vendor number
        "kunnr",   # customer number
        "partner", "bp_number",
    ],
    "payee_id": [
        "payee_id", "payee", "beneficiary", "beneficiary_id", "to_account",
        "receiver", "receiver_id", "creditor", "payee_name", "merchant_id",
        "vendor_id", "supplier_id",
        "empfg",   # SAP payee code
        "zlsch_payee",
    ],
    "is_cash": ["is_cash", "cash", "cash_transaction", "is_cash_txn"],
    "is_wire_transfer": [
        "is_wire_transfer", "wire", "is_wire", "wire_transfer",
        "is_cross_border", "cross_border", "is_international",
    ],
    "timestamp": [
        "timestamp", "date", "created_at", "settlement_time", "transaction_date",
        "time", "occurred_at", "booked_at", "posted_at", "event_time",
        "value_date", "processor_event_time", "entry_date",
    ],
    "memo": ["memo", "notes", "remarks", "narration", "description"],
    # Whether the money actually moved. Unmapped until now, which meant a
    # FAILED payment entered the candidate pool as a spendable Rs 1,000 and
    # could be named as a settlement member.
    "status": ["status", "txn_status", "payment_status", "state",
               "transaction_status", "order_status", "result"],
}


def _clean_amount(value: str | int | float) -> float:
    """
    Read an amount, or refuse to.

    Thin wrapper over ingestion.clean_amount_str, which owns this because it
    also produces the integer paise that subset-sum matches on. Both files
    grew the same "first numeric fragment wins" bug independently; one
    implementation cannot drift from itself.

    Empty still returns 0.0: a blank cell means the field is absent — a debit
    row on a bank statement carries no credit amount — which is a fact rather
    than a defect.
    """
    if isinstance(value, bool):
        raise AmountUnreadable(f"expected an amount, got a boolean: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return 0.0
    cleaned = clean_amount_str(value)
    return float(cleaned) if cleaned else 0.0


# Tokens that disqualify a column from being THE transaction amount.
#
# A fee, a tax or a discount is a deduction, not the value of the payment.
# Reconciling against one silently produces a completely wrong answer that
# still looks arithmetically tidy, which is the worst kind of defect. Measured
# on the ReconRiver dataset: gross_amount, fee_amount and net_amount all
# scored a perfect 100 against amount's synonyms, so which one won came down
# to header order. It happened to pick net_amount — correctly, by luck. A file
# listing fee_amount last would have reconciled a settlement against its fees.
AMOUNT_DISQUALIFIERS = {
    "fee", "fees", "charge", "charges", "commission", "tax", "tds", "gst",
    "vat", "discount", "refund", "chargeback", "rate", "balance",
}

# ref_id is deliberately MULTI-VALUED. Every other target takes exactly one
# column; reference columns are concatenated instead.
#
# The reason is linkage: it tokenises the reference and matches on tokens, so
# additional reference material strictly increases the chance of anchoring a
# transaction to its settlement. Forcing merchant_order_id and
# settlement_batch_id to compete for one slot throws away the settlement id —
# which is the single most valuable signal the engine has.
MULTI_VALUED_TARGETS = {"ref_id"}

# Debit and Credit are not rivals for the amount slot, they are opposite sides
# of one value: a bank statement row populates exactly one of them and leaves
# the other blank. Treating them as competitors would map only one column and
# silently zero out every row of the other sign — on a real statement that is
# half the file. So when the amount winner is one of these, its complements are
# mapped too and the blank-skip rule in the row builders picks the populated
# one per row.
COMPLEMENTARY_AMOUNT_TOKENS = {"debit", "credit", "dr", "cr", "withdrawal", "deposit"}


# Fields without which reconciliation is not possible.
#
# The distinction matters because a missing REQUIRED field does not produce a
# bad answer, it produces a silent absence. A row with no timestamp fails to
# parse during normalisation and is discarded, so a file whose date column was
# not recognised does not error — it reconciles against nothing and reports
# that nothing matched. Measured on the ReconRiver dataset: 107 of 207 records
# disappeared exactly this way, and the run looked like a matching failure
# rather than an ingestion failure.
#
# So Agent 0 refuses the file instead. A stated rejection a human can act on
# beats a clean-looking run over data that is not there.
REQUIRED_FIELDS = ("amount", "timestamp")

# At least one of these must be present — a transaction with no identifier of
# any kind cannot be reported, audited or matched back to its source.
IDENTITY_FIELDS = ("txn_id", "ref_id")

WHY_REQUIRED = {
    "amount": (
        "every settlement is reconciled by summing transaction amounts; with "
        "no amount column there is nothing to sum"
    ),
    "timestamp": (
        "every transaction needs a date to be placed in a settlement window; "
        "without one, each row is discarded during normalisation and the file "
        "silently contributes nothing"
    ),
    "identity": (
        "a transaction with no id or reference cannot be matched back to its "
        "source system, reported in an exception, or audited"
    ),
}


# What each internal field is CALLED when we speak to the person who
# uploaded the file, and why it is needed. The engine's own names leak
# vocabulary ("timestamp", "txn_id") and the remediation used to name a
# Python constant, HEADER_SYNONYMS, which means nothing to a finance team.
FIELD_IN_PLAIN_ENGLISH = {
    "amount": ("the payment amount",
               "Every settlement is checked by adding up the payments that "
               "make it up, so without an amount column there is nothing to "
               "add."),
    "timestamp": ("the payment date",
                  "Each payment needs a date so we can tell which settlement "
                  "period it belongs to. Without one, every row is discarded."),
    "identity": ("a payment reference or ID",
                 "Each payment needs something to identify it, so we can name "
                 "it back to you, keep it from being claimed by two "
                 "settlements, and record it in the audit trail."),
    "txn_id": ("a payment reference or ID",
               "Each payment needs something to identify it."),
}


def plain_rejection(problems: list[str], headers: list[str],
                    missing: list[str]) -> str:
    """
    The refusal, written for the person holding the file.

    They cannot act on "no column could be identified as 'timestamp'" — that
    sentence names an internal field and tells them nothing about their own
    spreadsheet. They can act on "we could not find a payment date; your file
    has these columns; rename one of them to 'date'".
    """
    lines = ["We could not read this file.", "", "WHAT IS MISSING"]
    for fld in missing:
        label, why = FIELD_IN_PLAIN_ENGLISH.get(
            fld, (fld, "This column is required."))
        lines.append(f"  - We could not find {label}. {why}")
    if not missing:
        for pr in problems:
            lines.append(f"  - {pr}")

    if headers:
        shown = ", ".join(headers[:14])
        more = "" if len(headers) <= 14 else f", and {len(headers) - 14} more"
        lines += ["", "THE COLUMNS YOUR FILE HAS", f"  {shown}{more}"]

    fixes = []
    for fld in missing:
        label, _ = FIELD_IN_PLAIN_ENGLISH.get(fld, (fld, ""))
        # The label carries an article ("the payment amount") because it reads
        # correctly in the sentences above; strip it here so the instruction
        # does not come out as "Rename your the payment amount column".
        short = label.removeprefix("the ").removeprefix("a ")
        names = ", ".join(HEADER_SYNONYMS.get(fld, [])[:6])
        if names:
            fixes.append(f"  - Rename your {short} column to one of: {names}")
    if fixes:
        lines += ["", "HOW TO FIX IT"] + fixes
        lines.append("  - Or send us the file and we will add your column "
                     "names to the ones we recognise.")
    return chr(10).join(lines)


class FileRejected(ValueError):
    """
    Agent 0 refused a file it cannot safely hand downstream.

    Subclasses ValueError so existing callers that catch ValueError keep
    working. Carries the structured detail as well as the message, so an API
    layer can render it without re-parsing prose.
    """

    def __init__(self, filename: str, problems: list[str],
                 headers: list[str] | None = None,
                 mapping: dict[str, str] | None = None,
                 hint: str = "", plain: str = ""):
        self.filename = filename
        self.plain = plain
        self.problems = problems
        self.headers = headers or []
        self.mapping = mapping or {}
        self.hint = hint

        lines = [f"Cannot process '{filename or 'uploaded file'}'. "
                 f"Agent 0 rejected it before reconciliation:"]
        lines += [f"  - {p}" for p in problems]
        if self.headers:
            lines.append(f"  Columns found: {', '.join(self.headers)}")
        if self.mapping:
            mapped = ", ".join(f"{c} -> {t}" for c, t in self.mapping.items())
            lines.append(f"  Mapped: {mapped}")
        else:
            lines.append("  Mapped: nothing recognised")
        if hint:
            lines.append(f"  {hint}")
        super().__init__("\n".join(lines))

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "rejected": True,
            # What the UI should show first. The technical fields stay for
            # whoever is debugging a feed.
            "plain": self.plain or self.args[0],
            "problems": self.problems,
            "columns_found": self.headers,
            "mapping": self.mapping,
            "hint": self.hint,
        }


def _validate_schema(filename: str, headers: list[str], report: "HeaderMappingReport") -> None:
    """
    Refuse the file unless the fields reconciliation depends on are present.

    Runs on the header mapping, before a single row is built — there is no
    point scrubbing 50,000 rows into a shape that cannot be reconciled.
    """
    mapping = report.mapping
    mapped_targets = set(mapping.values())
    problems: list[str] = []

    for fld in REQUIRED_FIELDS:
        if fld not in mapped_targets:
            problems.append(
                f"no column could be identified as '{fld}' — {WHY_REQUIRED[fld]}"
            )

    if not (set(IDENTITY_FIELDS) & mapped_targets):
        problems.append(
            f"no column could be identified as an identifier "
            f"({' or '.join(IDENTITY_FIELDS)}) — {WHY_REQUIRED['identity']}"
        )

    if not problems:
        return

    missing = [f for f in REQUIRED_FIELDS if f not in mapped_targets]
    hint_parts = []
    for fld in missing:
        known = ", ".join(HEADER_SYNONYMS.get(fld, [])[:8])
        hint_parts.append(
            f"Rename the {fld} column to one of [{known}], or add its actual "
            f"name to HEADER_SYNONYMS['{fld}']."
        )
    if report.disqualified:
        for col, why in report.disqualified.items():
            hint_parts.append(f"Note: column '{col}' was {why}.")

    missing_for_plain = list(missing)
    if not (set(IDENTITY_FIELDS) & mapped_targets):
        missing_for_plain.append("identity")
    raise FileRejected(filename, problems, headers, mapping,
                       " ".join(hint_parts),
                       plain=plain_rejection(problems, headers, missing_for_plain))


def _validate_rows(filename: str, rows: list[dict], headers: list[str],
                   mapping: dict[str, str]) -> None:
    """
    Refuse the file when the columns mapped but the DATA in them is unusable.

    A schema check alone is not enough: a correctly-identified timestamp column
    full of "2026-99-99T99:99:99Z" maps cleanly and still yields nothing. Some
    corrupt rows are normal and belong in the exception queue — ALL of them
    corrupt means the feed is broken, and processing it would report a
    reconciliation failure when the real fault is upstream.
    """
    if not rows:
        raise FileRejected(
            filename,
            ["the file contains no data rows"],
            headers, mapping,
            "Check the export actually produced records.",
        )

    usable_ts = 0
    usable_amt = 0
    for r in rows:
        if r.get("timestamp"):
            # Same ISO fast path as ingestion. This loop asks a cheap question
            # of every row — "is this a date at all" — and was answering it
            # with a general-purpose tokenizer, which cost 1.4 of Agent 0's
            # 1.9 seconds on a 25,000-row export. The counts feed the >20%
            # corruption warning, so the loop cannot short-circuit; it can
            # just stop doing the expensive thing on the easy cases.
            raw = str(r["timestamp"])
            if _fast_parse(raw) is not None:
                usable_ts += 1
            else:
                try:
                    dateparser.parse(raw)
                    usable_ts += 1
                except Exception:
                    pass
        try:
            if float(r.get("amount") or 0) != 0:
                usable_amt += 1
        except (TypeError, ValueError):
            pass

    problems = []
    if usable_ts == 0:
        problems.append(
            f"none of the {len(rows)} rows has a parseable date — the column "
            f"was identified but its contents are not dates"
        )
    if usable_amt == 0:
        problems.append(
            f"none of the {len(rows)} rows has a non-zero amount — the column "
            f"was identified but carries no values"
        )

    if problems:
        raise FileRejected(
            filename, problems, headers, mapping,
            "The columns were recognised, so this is a data problem rather "
            "than a mapping one. Check the export format at source.",
        )

    # Partial corruption is expected and is the exception queue's job, not a
    # reason to refuse the file — but it should be visible rather than absorbed.
    for label, good in (("date", usable_ts), ("amount", usable_amt)):
        bad = len(rows) - good
        if bad and bad / len(rows) > 0.2:
            logger.warning(
                "Agent 0: %s of %s rows in %r have an unusable %s (%.0f%%). "
                "They will be dropped during normalisation.",
                bad, len(rows), filename, label, 100 * bad / len(rows),
            )


@dataclass
class HeaderMappingReport:
    """
    What the mapper decided and what it had to choose between.

    Competitions are surfaced rather than resolved silently. A column mapping
    is a guess about someone else's schema, and the guesses that matter — which
    column is the amount, which is the timestamp — are exactly the ones that
    destroy a reconciliation when wrong.
    """
    mapping: dict[str, str]
    competitions: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    disqualified: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    llm_assisted: bool = False
    llm_notes: list[str] = field(default_factory=list)


def _missing_required(mapping: dict[str, str]) -> list[str]:
    targets = set(mapping.values())
    missing = [f for f in REQUIRED_FIELDS if f not in targets]
    if not (set(IDENTITY_FIELDS) & targets):
        missing.append("identity")
    return missing


def _constrain(proposed: dict[str, str], headers: list[str]) -> dict[str, str]:
    """
    Apply the same structural rules to a proposed mapping, whatever proposed it.

    An LLM suggestion is a suggestion. It does not get to make a fee column the
    transaction amount, or hand two columns to a single-valued target, because
    those rules exist to prevent specific expensive mistakes and are not
    contingent on who is guessing.
    """
    clean = {h: h.lower().strip().replace("_", " ") for h in headers}
    out: dict[str, str] = {}
    used_single: set[str] = set()

    for col, target in proposed.items():
        if col not in clean or target not in TARGET_FIELDS:
            continue
        tokens = set(clean[col].split())
        if target == "amount" and tokens & AMOUNT_DISQUALIFIERS:
            logger.warning(
                "Agent 0: ignoring proposed mapping %r -> amount; it names a "
                "deduction, not a transaction value.", col,
            )
            continue
        if target not in MULTI_VALUED_TARGETS:
            if target in used_single:
                continue
            used_single.add(target)
        out[col] = target
    return out


# ── Inference from the data, when the column NAMES give nothing ───────────
#
# A synonym list cannot keep up with the world. Measured against eight
# plausible export formats, name matching alone rejected half of them — and a
# rejection is the worst outcome available, because the alternative to a
# slightly uncertain mapping is no reconciliation at all.
#
# So when a REQUIRED field has no name match, the values are inspected. A
# column of parseable dates is a timestamp whatever it is called; a column of
# decimal numbers is a candidate amount; a column of high-cardinality opaque
# strings is an identifier. This is what llm_header_mapper does, done
# deterministically and without needing a key — the model stays available for
# the genuinely ambiguous cases, but it is no longer the only thing standing
# between an unfamiliar file and a rejection.
#
# The same structural constraints still apply afterwards: a fee or tax column
# is still disqualified from becoming `amount`, single-valued targets still
# take one column, and the schema gate still runs. Inference proposes; the
# rules dispose.

_INFER_SAMPLE = 25


def _values(header: str, rows: list[dict], limit: int = _INFER_SAMPLE) -> list[str]:
    out = []
    for r in rows:
        v = str(r.get(header, "") or "").strip()
        if v:
            out.append(v)
        if len(out) >= limit:
            break
    return out


_SLASH_DATE = re.compile(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\s*$")


def resolve_day_order(values: list[str]) -> tuple[str, bool]:
    """
    Decide whether a column of d/m/y-shaped dates is day-first or month-first,
    by looking at the WHOLE column rather than one value.

    A single "09/03/2026" cannot be resolved: it is 9 March to most of the
    world and 3 September in the US. A column can be, because one row with a
    first component above 12 settles it for every other row.

    Returns (order, proven). `proven` is False when every value in the column
    happens to be ambiguous — the caller must not present a guess as a fact.

    This exists because the date was previously handed to the browser as the
    raw string and read with `new Date(...)`, which assumes US month-first.
    An Indian statement's 09/03/2026 became September 3rd — a month that was
    not in the file — and 15/03/2026 became Invalid Date, so the field
    silently never filled at all.
    """
    day_first = month_first = False
    for v in values:
        m = _SLASH_DATE.match(str(v or ""))
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12:
            day_first = True
        if b > 12:
            month_first = True
    if day_first and not month_first:
        return "day", True
    if month_first and not day_first:
        return "month", True
    # Either nothing decisive, or the column contradicts itself. Day-first is
    # the convention in the market this engine targets, and it is reported as
    # an assumption rather than a finding.
    return "day", False


def normalize_date(value: str, order: str = "day") -> str | None:
    """A d/m/y-shaped date as ISO-8601, or None if it is not that shape."""
    m = _SLASH_DATE.match(str(value or ""))
    if not m:
        return None
    first, second, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000 if year < 70 else 1900
    day, month = (first, second) if order == "day" else (second, first)
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def _looks_like_date(vals: list[str]) -> float:
    """Share of values that parse as a date."""
    if not vals:
        return 0.0
    ok = 0
    for v in vals:
        try:
            if _fast_parse(v) is not None:
                ok += 1
                continue
            dateparser.parse(v)
            ok += 1
        except Exception:
            pass
    return ok / len(vals)


def _looks_like_amount(vals: list[str]) -> float:
    """Share that parse as a number. Bare integers score lower than decimals:
    a column of 1, 2, 3 is a row counter, not money."""
    if not vals:
        return 0.0
    ok = 0
    for v in vals:
        try:
            _clean_amount(v)
            ok += 1 if ("." in v or "," in v) else 0.4
        except Exception:
            pass
    return ok / len(vals)


def _looks_like_identifier(vals: list[str]) -> float:
    """High cardinality, mostly alphanumeric, not a date and not a number."""
    if not vals:
        return 0.0
    distinct = len(set(vals)) / len(vals)
    if distinct < 0.7:
        return 0.0
    if _looks_like_date(vals) > 0.5 or _looks_like_amount(vals) > 0.8:
        return 0.0
    alnum = sum(1 for v in vals if any(c.isalnum() for c in v)) / len(vals)
    return distinct * alnum


def infer_mapping_from_values(
    headers: list[str], rows: list[dict], missing: list[str],
    already: dict[str, str],
) -> dict[str, str]:
    """Propose a column for each missing required field, from its values."""
    taken = set(already)
    proposals: dict[str, str] = {}

    for field in missing:
        scorer = {
            "timestamp": _looks_like_date,
            "amount": _looks_like_amount,
            "txn_id": _looks_like_identifier,
            "ref_id": _looks_like_identifier,
        }.get(field)
        if scorer is None:
            continue

        best, best_score = None, 0.0
        for h in headers:
            if h in taken:
                continue
            # A fee or tax column is never the transaction amount, however
            # numeric it looks. This is the same disqualifier the name-based
            # path applies, and it is the one that matters most: mapping a
            # fee column to `amount` reconciles a settlement against its own
            # charges and looks like a clean match.
            if field == "amount" and any(
                bad in h.lower() for bad in AMOUNT_DISQUALIFIERS
            ):
                continue
            score = scorer(_values(h, rows))
            if score > best_score:
                best, best_score = h, score

        if best is not None and best_score >= 0.6:
            proposals[best] = field
            taken.add(best)

    return proposals


def composite_key_partner(mapping: dict, headers: list[str]) -> str | None:
    """
    The sibling column that, with the id, actually identifies a row.

    Real ledgers key line items on a pair — (trans_id, trans_line_no) — and
    reading only the first half merges genuinely separate rows. Cincinnati's
    published feed holds 1,225 identities across 4,000 rows for exactly this
    reason.

    Detecting that and then doing nothing about it, which is what happened
    before, is the worst of both: the warning tells a reviewer their data is
    ambiguous while the engine goes on treating it as if it were not.
    """
    id_col = next((c for c, t in mapping.items() if t == "txn_id"), None)
    if id_col is None:
        id_col = next((c for c, t in mapping.items() if t == "ref_id"), None)
    if not id_col:
        return None
    lower = {h.lower(): h for h in headers}
    base = id_col.lower().rsplit("_", 1)[0]
    for suffix in ("line_no", "line", "line_number", "seq", "sequence", "item_no"):
        for cand in (f"{base}_{suffix}", suffix):
            real = lower.get(cand)
            if real and real != id_col:
                return real
    return None


def check_identity_column(mapping: dict, rows: list[dict] | None,
                          headers: list[str]) -> list[str]:
    """
    Is the column we called txn_id actually an identifier?

    Amount and timestamp are well defended: an ambiguous match warns, an
    unrecognised name falls back to reading the values, and a missing column
    refuses the file. txn_id had none of that, and on real public feeds it
    quietly accepted free text. Vermont's "description" column became the
    transaction id (88.9% of ids duplicated); Mesa's "commodity_description"
    became it too (82.3%), so thousands of distinct payments shared a handful
    of identities.

    That is not cosmetic. The id is how a matched payment is named back to the
    reviewer, how a payment is kept from being claimed by two settlements, and
    how the audit trail refers to anything at all. An id that repeats 220
    times cannot do any of those.

    Three things are checked, all from the data rather than the name:
      - how often the values repeat
      - whether they look like prose (spaces, long strings)
      - whether a sibling column suggests a COMPOSITE key, which is the
        Cincinnati case: a real trans_id, correctly mapped, but the table is
        keyed on (trans_id, trans_line_no), so 4000 rows held 1225 identities

    Returns warning strings; it never rejects. A repeating id is bad but it is
    not proof the file is unusable, and refusing here would block feeds that
    reconcile perfectly well on references and amounts.
    """
    warnings: list[str] = []
    # Check whichever column actually supplies identity, not just txn_id.
    # Vermont and Mesa map their description column to ref_id, and txn_id
    # falls back to ref_id downstream — so looking only at txn_id missed the
    # two worst real-world cases entirely.
    id_col = next((c for c, t in mapping.items() if t == "txn_id"), None)
    if id_col is None:
        id_col = next((c for c, t in mapping.items() if t == "ref_id"), None)
    if not id_col or not rows:
        return warnings

    sample = rows[:2000]
    values = [str(r.get(id_col) or "").strip() for r in sample]
    present = [v for v in values if v]
    if not present:
        return warnings

    distinct = len(set(present))
    dup_rate = 1 - distinct / len(present)
    spacey = sum(1 for v in present if " " in v) / len(present)
    avg_len = sum(len(v) for v in present) / len(present)

    if spacey > 0.4 and avg_len > 15:
        warnings.append(
            f"The column '{id_col}' was used as the payment reference, but its "
            f"values read like descriptions rather than reference numbers (for "
            f"example {present[0][:40]!r}). Payments will be hard to tell apart. "
            f"If your file has a real payment or voucher number, rename that "
            f"column to 'txn_id' or 'payment_id'."
        )
    elif dup_rate > 0.5:
        warnings.append(
            f"The column '{id_col}' was used as the payment reference, but "
            f"{dup_rate:.0%} of its values are repeats — {len(present)} rows "
            f"share only {distinct} different references. Payments that share a "
            f"reference cannot be told apart, so one may be matched to the wrong "
            f"settlement or counted twice."
        )

    # Composite key: a sibling line/sequence column means the real key is a
    # pair, and using only the first half merges genuinely separate rows.
    lower = {h.lower(): h for h in headers}
    base = id_col.lower().rsplit("_", 1)[0]
    for suffix in ("line_no", "line", "line_number", "seq", "sequence", "item_no"):
        for cand in (f"{base}_{suffix}", suffix):
            real = lower.get(cand)
            if real and real != id_col and dup_rate > 0.1:
                warnings.append(
                    f"'{id_col}' repeats across rows and the file also has "
                    f"'{real}'. This usually means one payment is split over "
                    f"several lines, and the two columns together identify a "
                    f"line. Reconciliation treats each row as its own payment, "
                    f"so a multi-line payment may be double counted."
                )
                return warnings
    return warnings


def resolve_headers(actual_headers: list[str], rows: list[dict] | None = None,
                    filename: str = "") -> HeaderMappingReport:
    """
    Deterministic mapping first; escalate to the LLM only if it falls short.

    The ordering is deliberate. The rule-based mapper is free, instant,
    reproducible and auditable — properties worth more than a marginally better
    guess — so a working mapping is never overridden. The model is asked only
    when a REQUIRED field could not be found, which is the case where the
    alternative is not "a slightly worse mapping" but "silently discard the
    file's rows".
    """
    report = map_headers_with_report(actual_headers)
    missing = _missing_required(report.mapping)
    if not missing or rows is None:
        return report

    # Try the values before the model. Deterministic, instant, needs no key,
    # and it resolves the common case where a column is simply called
    # something the synonym list has not met yet.
    # Identity is validated separately from REQUIRED_FIELDS, so ask for it
    # too when nothing has claimed it — otherwise a file whose amount and
    # timestamp were both inferred still gets refused for want of an id.
    want = list(missing)
    if not any(f in report.mapping.values() for f in IDENTITY_FIELDS):
        want.append("txn_id")

    inferred = infer_mapping_from_values(
        actual_headers, rows, want, report.mapping
    )
    if inferred:
        merged = dict(report.mapping)
        merged.update(inferred)
        constrained = _constrain(merged, actual_headers)
        remaining = _missing_required(constrained)
        if len(remaining) < len(missing):
            resolved = [f for f in missing if f not in remaining]
            logger.info(
                "Agent 0: inferred %s from column VALUES for %r (%s). "
                "Verify before relying on the reconciliation.",
                resolved, filename or "input",
                ", ".join(f"{c} -> {f}" for c, f in inferred.items()),
            )
            report = HeaderMappingReport(
                mapping=constrained,
                competitions=report.competitions,
                disqualified=report.disqualified,
                warnings=report.warnings + [
                    "Mapped " + ", ".join(f"{c} -> {f}" for c, f in inferred.items())
                    + " by inspecting the column's VALUES, because its name "
                    "matched no known synonym. Verify this is correct — an "
                    "amount or timestamp on the wrong column produces a "
                    "confidently wrong reconciliation."
                ],
            )
            missing = remaining
            if not missing:
                return report

    proposed = llm_header_mapper.propose_mapping(
        actual_headers, rows, TARGET_FIELDS, missing
    )
    if not proposed:
        return report

    mapping, notes = proposed
    constrained = _constrain(mapping, actual_headers)
    still_missing = _missing_required(constrained)

    if len(still_missing) >= len(missing):
        # No improvement — keep the deterministic result so the rejection
        # message reflects what the rules actually found.
        logger.info("Agent 0: LLM mapping did not resolve %s; keeping rules.", missing)
        return report

    logger.info(
        "Agent 0: LLM resolved %s for %r that rule-based matching missed.",
        [f for f in missing if f not in still_missing], filename or "input",
    )
    return HeaderMappingReport(
        mapping=constrained,
        competitions=report.competitions,
        disqualified=report.disqualified,
        warnings=report.warnings + [
            "Mapping was resolved with LLM assistance because rule-based "
            "matching could not identify: " + ", ".join(missing) + ". "
            "Verify the mapping before relying on the reconciliation."
        ],
        llm_assisted=True,
        llm_notes=notes,
    )


def _score_pairs(headers: list[str]) -> tuple[dict, dict, dict]:
    clean = {h: h.lower().strip().replace("_", " ") for h in headers if h and h.strip()}
    syns = {
        t: [s.replace("_", " ") for s in HEADER_SYNONYMS.get(t, [t])]
        for t in TARGET_FIELDS
    }

    scores: dict[tuple[str, str], float] = {}
    disqualified: dict[str, str] = {}

    for raw, c in clean.items():
        tokens = set(c.split())
        for target in TARGET_FIELDS:
            if target == "amount" and tokens & AMOUNT_DISQUALIFIERS:
                bad = ", ".join(sorted(tokens & AMOUNT_DISQUALIFIERS))
                disqualified[raw] = (
                    f"not eligible as 'amount': contains {bad}, which denotes a "
                    f"deduction rather than the transaction value"
                )
                continue

            if c in syns[target]:
                # Exact hit. Specificity is the full column name.
                scores[(raw, target)] = (100.0, len(c))
                continue
            m = process.extractOne(c, syns[target], scorer=fuzz.token_set_ratio)
            if m and m[1] > 75:
                # An exact synonym hit outranks a fuzzy one of the same numeric
                # score, so fuzzy matches are nudged below 100.
                #
                # The second element is SPECIFICITY: the length of the synonym
                # that matched. token_set_ratio scores any subset match as 100,
                # so a bare synonym like "id" ties with "order id" on every
                # *_id column in the file — and the tie was being broken
                # alphabetically, which is to say arbitrarily. On the ReconRiver
                # processor feed that handed merchant_order_id to txn_id and
                # dropped settlement_batch_id entirely, discarding the anchor
                # linkage depends on. Preferring the longer matched synonym
                # makes "order id" beat "id", which is the intended reading.
                scores[(raw, target)] = (min(float(m[1]), 99.0), len(m[0]))

    return clean, scores, disqualified


def map_headers_with_report(actual_headers: list[str]) -> HeaderMappingReport:
    """
    Assign columns to target fields by best score, one column per target.

    Replaces a first-past-the-post loop that let several columns claim the
    same target and left the winner to be decided by whichever happened to be
    processed last. Assignment is greedy on score: the strongest (column,
    target) pair is taken first, and both are then unavailable — so a column
    cannot be spent on a weak match when it is the best evidence for something
    else.
    """
    clean, scores, disqualified = _score_pairs(actual_headers)

    competitions: dict[str, list[tuple[str, float]]] = {}
    for (raw, target), (sc, spec) in scores.items():
        competitions.setdefault(target, []).append((raw, sc))
    for target in competitions:
        competitions[target].sort(key=lambda x: (-x[1], x[0]))

    mapping: dict[str, str] = {}
    used_targets: set[str] = set()
    used_columns: set[str] = set()

    for (raw, target), (sc, spec) in sorted(
        scores.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0][0])
    ):
        if raw in used_columns:
            continue
        if target in used_targets and target not in MULTI_VALUED_TARGETS:
            continue
        mapping[raw] = target
        used_columns.add(raw)
        if target not in MULTI_VALUED_TARGETS:
            used_targets.add(target)

    # Re-admit complementary amount columns (see COMPLEMENTARY_AMOUNT_TOKENS).
    amount_col = next((c for c, t in mapping.items() if t == "amount"), None)
    complementary: list[str] = []
    if amount_col and (set(clean[amount_col].split()) & COMPLEMENTARY_AMOUNT_TOKENS):
        for raw, _ in competitions.get("amount", []):
            if raw == amount_col or raw in used_columns:
                continue
            if set(clean[raw].split()) & COMPLEMENTARY_AMOUNT_TOKENS:
                mapping[raw] = "amount"
                used_columns.add(raw)
                complementary.append(raw)

    warnings: list[str] = []
    for target, cands in competitions.items():
        if target in MULTI_VALUED_TARGETS or len(cands) < 2:
            continue
        chosen = next((c for c, t in mapping.items() if t == target), None)
        losers = [c for c, _ in cands if c != chosen and c not in complementary]
        if chosen and losers:
            warnings.append(
                f"'{target}' matched {len(cands)} columns; chose '{chosen}' over "
                f"{', '.join(repr(l) for l in losers)}. Verify this is correct — "
                f"an amount or timestamp mapped to the wrong column produces a "
                f"confidently wrong reconciliation."
            )

    # Two columns with the SAME NAME.
    #
    # The competition check above catches 'amount' losing to 'amt' — different
    # names contending for one target. It cannot catch 'amount' twice, because
    # `mapping` is keyed by raw header name, so the duplicates collapse into a
    # single entry and there is no competition left to see. csv.DictReader
    # collapses them the same way, last column winning.
    #
    # Left alone this is quiet and wrong in the worst direction: a ledger
    # exported with a stale and a corrected amount column reconciles against
    # whichever happens to sit further right. The arithmetic then fails to
    # tie out and the batch is withheld — safe, but for a reason the reader
    # cannot see. So say which column won and which was discarded.
    seen: dict[str, int] = {}
    for h in actual_headers:
        key = str(h).strip().lower()
        seen[key] = seen.get(key, 0) + 1
    for key, count in seen.items():
        if count < 2 or not key:
            continue
        target = next((t for c, t in mapping.items() if str(c).strip().lower() == key), None)
        detail = f" It is mapped to '{target}'." if target else ""
        warnings.append(
            f"The file has {count} columns named '{key}'. Only the LAST one is "
            f"read; the earlier {count - 1} are discarded.{detail} If those "
            f"columns hold different values, the reconciliation is running on "
            f"the rightmost — check which one your ledger intends."
        )

    for target in ("amount", "timestamp"):
        if target not in mapping.values():
            warnings.append(
                f"No column mapped to '{target}'. Rows will be unusable "
                f"downstream; add the column name to HEADER_SYNONYMS."
            )

    return HeaderMappingReport(
        mapping=mapping,
        competitions=competitions,
        disqualified=disqualified,
        warnings=warnings,
    )


def _map_headers(actual_headers: list[str]) -> dict[str, str]:
    """Mapping only. See map_headers_with_report for what it chose between."""
    report = map_headers_with_report(actual_headers)
    for w in report.warnings:
        logger.warning("Agent 0 header mapping: %s", w)
    for col, why in report.disqualified.items():
        logger.info("Agent 0 header mapping: column %r %s", col, why)
    return report.mapping


def parse_csv(content: str, filename_hint: str = "CSV input",
              warnings_out: list[str] | None = None) -> list[dict[str, Any]]:
    """Parse CSV content and map to target schema."""
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        return []

    headers = [h for h in (reader.fieldnames or []) if h and h.strip()]
    raw_rows = list(reader)

    # Sample values are what disambiguate a column name, so the rows have to be
    # read before mapping rather than streamed past it.
    report = resolve_headers(headers, raw_rows, filename_hint)
    mapping = report.mapping
    # The identity check needs the rows, so it runs here rather than inside
    # the name-based mapper.
    for w in check_identity_column(mapping, raw_rows, headers):
        report.warnings.append(w)

    # These were logged and nowhere else. A warning that only reaches the
    # server log is invisible to the person whose file it is about — and
    # "two columns named amount, the rightmost won" is precisely the thing
    # they need to see, because the batch then fails to tie out for a reason
    # the screen cannot otherwise explain.
    if warnings_out is not None:
        warnings_out.extend(report.warnings)
    for w in report.warnings:
        logger.warning("Agent 0 header mapping: %s", w)

    # Gate BEFORE building rows: refuse a file that cannot be reconciled
    # rather than handing half-formed records to the next agent.
    _validate_schema(filename_hint, headers, report)

    # A composite key is USED, not just reported. Without this the two halves
    # were detected, described to the reviewer, and then discarded.
    partner = composite_key_partner(mapping, headers)
    id_source = next((c for c, t in mapping.items() if t == "txn_id"), None)         or next((c for c, t in mapping.items() if t == "ref_id"), None)

    results = []
    for _row_no, row in enumerate(raw_rows, start=1):
        mapped_row = {
            "txn_id": "",
            "ref_id": "",
            "amount": 0.0,
            "currency": "",      # blank, not "INR" — see below
            "timestamp": "",
            "memo": "",
            "status": "",
        }
        
        for raw_col, target_col in mapping.items():
            val = row.get(raw_col, "")
            raw_str = str(val).strip()
            # Skip blank sources so they don't overwrite a value already
            # set by another column mapped to the same target (e.g. a
            # statement with separate Debit/Credit columns, where each
            # row only populates one of the two). Without this, whichever
            # mapped column happens to be processed last always wins,
            # even when it's blank -- correct only by accident of header
            # order, not by design.
            if not raw_str:
                continue
            if target_col == "amount":
                # An amount this parser cannot read must stop the file, not
                # crash the request and not quietly become a number. The row
                # is named so the operator can go and look at it.
                try:
                    mapped_row[target_col] = _clean_amount(val)
                except AmountUnreadable as exc:
                    raise FileRejected(
                        filename_hint,
                        [f"row {_row_no}, column {raw_col!r}: {exc}"],
                        list(mapping.keys()) if isinstance(mapping, dict) else [],
                        {},
                        "Fix the amount in that row, or remove it. This engine "
                        "will not guess a settlement amount.",
                    ) from exc
            elif target_col in MULTI_VALUED_TARGETS:
                # Reference columns accumulate instead of overwriting. Linkage
                # tokenises this field, so carrying both the order id and the
                # settlement id gives it the settlement anchor it would
                # otherwise never see.
                existing = mapped_row[target_col]
                if raw_str not in existing.split(" "):
                    mapped_row[target_col] = f"{existing} {raw_str}".strip()
            else:
                mapped_row[target_col] = raw_str
                
        # If no txn_id but there is a ref_id, mirror it (or vice-versa) to prevent blanks
        if not mapped_row["txn_id"] and mapped_row["ref_id"]:
            mapped_row["txn_id"] = mapped_row["ref_id"]
        elif not mapped_row["ref_id"] and mapped_row["txn_id"]:
            mapped_row["ref_id"] = mapped_row["txn_id"]

        # Composite key: one payment split over several lines is keyed on the
        # PAIR, and using only the first half merges rows that are genuinely
        # separate. Cincinnati's published feed holds 1,225 identities across
        # 4,000 rows for exactly this reason. The line number is appended to
        # txn_id so each row can be named, and ref_id keeps the bare id so
        # linkage still anchors on the reference the source intended.
        if partner and id_source:
            line = str(row.get(partner, "") or "").strip()
            if line and mapped_row["txn_id"]:
                mapped_row["txn_id"] = f"{mapped_row['txn_id']}-{line}"

        results.append(mapped_row)

    _validate_rows(filename_hint, results, headers, mapping)
    return results


def parse_json(content: str, filename_hint: str = "JSON input",
               warnings_out: list[str] | None = None) -> list[dict[str, Any]]:
    """Parse JSON array of objects and map to target schema."""
    data = json.loads(content)
    if not isinstance(data, list):
        data = [data]
        
    if not data:
        return []

    # Get all unique keys across all objects to map headers
    all_keys = set()
    for _row_no, item in enumerate(data, start=1):
        if isinstance(item, dict):
            all_keys.update(item.keys())
            
    headers = [k for k in all_keys if k and str(k).strip()]
    report = resolve_headers(headers, [d for d in data if isinstance(d, dict)], filename_hint)
    mapping = report.mapping
    for w in report.warnings:
        logger.warning("Agent 0 header mapping: %s", w)

    _validate_schema(filename_hint, headers, report)

    results = []
    for item in data:
        if not isinstance(item, dict):
            continue
            
        mapped_row = {
            "txn_id": "",
            "ref_id": "",
            "amount": 0.0,
            "currency": "",      # blank, not "INR" — see below
            "timestamp": "",
            "memo": "",
            "status": "",
        }
        
        for raw_col, target_col in mapping.items():
            val = item.get(raw_col, "")
            raw_str = str(val).strip()
            # Same skip-blank-overwrite fix as parse_csv — see that
            # function's comment for why this matters.
            if not raw_str:
                continue
            if target_col == "amount":
                # An amount this parser cannot read must stop the file, not
                # crash the request and not quietly become a number. The row
                # is named so the operator can go and look at it.
                try:
                    mapped_row[target_col] = _clean_amount(val)
                except AmountUnreadable as exc:
                    raise FileRejected(
                        filename_hint,
                        [f"row {_row_no}, column {raw_col!r}: {exc}"],
                        list(mapping.keys()) if isinstance(mapping, dict) else [],
                        {},
                        "Fix the amount in that row, or remove it. This engine "
                        "will not guess a settlement amount.",
                    ) from exc
            elif target_col in MULTI_VALUED_TARGETS:
                # Reference columns accumulate instead of overwriting. Linkage
                # tokenises this field, so carrying both the order id and the
                # settlement id gives it the settlement anchor it would
                # otherwise never see.
                existing = mapped_row[target_col]
                if raw_str not in existing.split(" "):
                    mapped_row[target_col] = f"{existing} {raw_str}".strip()
            else:
                mapped_row[target_col] = raw_str
                
        if not mapped_row["txn_id"] and mapped_row["ref_id"]:
            mapped_row["txn_id"] = mapped_row["ref_id"]
        elif not mapped_row["ref_id"] and mapped_row["txn_id"]:
            mapped_row["ref_id"] = mapped_row["txn_id"]
            
        results.append(mapped_row)

    _validate_rows(filename_hint, results, headers, mapping)
    return results


# ── The settlements list itself ───────────────────────────────────────────
#
# A queue needs the settlements as data, not as form fields. This is a
# different schema from a transaction feed — a settlement has an id, a
# credited amount and a date — so it gets its own reader rather than being
# forced through the transaction mapper, which would try to make `batch_id`
# a txn_id and `credited_amount` a transaction amount.
SETTLEMENT_SYNONYMS = {
    # A real bank statement has no column called "batch_id". The settlement's
    # identity lives in the narration — "UPI/CR/609469525203/..." — because
    # that is where the bank puts the reference. Tested against an actual SBI
    # statement, which was rejected outright for want of an id it does not
    # have and never will.
    #
    # The narration names go LAST so a file that does carry an explicit
    # settlement id still wins on that instead.
    "batch_id": ["batch_id", "settlement_id", "settlement_batch_id", "utr",
                 "reference", "batch", "id", "settlement", "bank_reference",
                 "description", "narration", "particulars", "remarks",
                 "transaction_remarks", "details"],
    "net_amount": ["net_amount", "amount", "credited_amount", "credit",
                   "net", "settled_amount", "value", "credited"],
    "settled_at": ["settled_at", "settlement_date", "value_date", "date",
                   "credited_on", "booked_at", "settled_on", "txn_date"],
    "currency": ["currency", "ccy", "curr"],
    "declared_deductions": ["declared_deductions", "deductions", "fees",
                            "total_fees", "charges", "fee_amount"],
}


def _settlement_key(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


def _settlement_columns(rows: list[dict], headers: list[str]) -> dict[str, str]:
    """
    Work out which column is the credited amount and which is the date, by
    looking at the VALUES.

    Synonyms cannot carry this. Tested against five bank layouts, name
    matching alone resolved one — the one it had been tuned to. Banks differ
    from each other in column names and from themselves across products, and
    each new synonym matches the sample in hand and misses the next file.

    Two things make a bank statement harder than a transaction feed, and both
    are handled here rather than hoped away:

      A statement has BOTH debit and credit columns. A settlement is money
      ARRIVING, so the debit column is the wrong one and picking "the numeric
      column" at random gets it wrong half the time.

      A statement has a running balance, which is numeric, positive, and
      present on every single row. It is the most amount-looking column in the
      file and it is never the amount. Sparseness separates them: a credit
      column is populated only on credit rows, a balance always.
    """
    filled = {h: [str(r.get(h, "") or "").strip() for r in rows] for h in headers}

    def numeric_share(vals):
        nz = [v for v in vals if v]
        if not nz:
            return 0.0, 0.0
        ok = 0
        for v in nz:
            try:
                _clean_amount(v)
                ok += 1
            except Exception:
                pass
        return ok / len(nz), len(nz) / len(vals)

    date_col = None
    best_date = 0.0
    amount_col = None
    best_amount = (-1.0, 0.0)

    for h in headers:
        vals = filled[h]
        lc = h.strip().lower()

        d = _looks_like_date([v for v in vals if v])
        if d > best_date and d >= 0.6:
            date_col, best_date = h, d

        num, density = numeric_share(vals)
        if num < 0.8 or not any(vals):
            continue
        # A balance is numeric and present on every row. Never the amount.
        if "bal" in lc:
            continue
        # Debits are money leaving. A settlement is money arriving.
        if any(k in lc for k in ("debit", "withdraw", "dr ", " dr", "(dr)", "paid out")):
            continue
        if lc in ("dr",):
            continue
        # Prefer an explicit credit column; otherwise prefer the SPARSER
        # numeric column, since a fully-populated one is usually the balance.
        explicit = 1.0 if any(
            k in lc for k in ("credit", "deposit", "cr ", " cr", "(cr)", "amount", "value", "net")
        ) or lc in ("cr",) else 0.0
        score = (explicit, 1.0 - density)
        if score > best_amount:
            amount_col, best_amount = h, score

    found = {}
    if amount_col:
        found[amount_col] = "net_amount"
    if date_col:
        found[date_col] = "settled_at"
    return found


def parse_settlements(content: bytes, filename: str,
                      report: dict | None = None) -> list[dict]:
    """
    Read a settlements list, or the credit lines of a bank statement.

    Only the amount and the date are required. An identifier is NOT: most bank
    statements have no column that names a settlement, and requiring one meant
    refusing the most common file a finance team actually has. Where none is
    found, one is derived from the date and amount and marked as derived, and
    linkage will report that it has nothing to anchor on — which is true, and
    which now correctly withholds rather than guessing.

    Pass `report` to learn what was NOT returned. Three kinds of row are
    dropped here and only one of them is harmless: a blank amount is a debit
    line and genuinely is not a settlement, but a non-empty amount that will
    not parse is a credit this parser could not read — and silently losing
    one of those from a bank statement is the failure this whole engine
    exists to prevent. The caller cannot report what it is never told.
    """
    text = content.decode("utf-8-sig", errors="replace")
    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        loaded = json.loads(stripped)
        rows = loaded if isinstance(loaded, list) else loaded.get("settlements", [])
    else:
        rows = list(csv.DictReader(io.StringIO(text)))

    rows = [r for r in rows if isinstance(r, dict) and any(
        str(v or "").strip() for v in r.values())]
    if not rows:
        raise FileRejected(filename, ["the settlements file contains no rows"],
                           [], {}, "Check the export actually produced records.")

    headers = [h for h in rows[0].keys() if h]

    # Names first — an explicit settlement_id column should always win over
    # anything guessed from values.
    mapping: dict[str, str] = {}
    for field, names in SETTLEMENT_SYNONYMS.items():
        for h in headers:
            if h in mapping:
                continue
            if _settlement_key(h) in names:
                mapping[h] = field
                break

    # Then values, for whatever the names did not resolve.
    have = set(mapping.values())
    if "net_amount" not in have or "settled_at" not in have:
        for col, field in _settlement_columns(rows, headers).items():
            if field in have:
                continue
            # Do not steal a column already claimed for something else.
            if col in mapping and mapping[col] != field:
                continue
            mapping[col] = field
            have.add(field)

    missing = [f for f in ("net_amount", "settled_at") if f not in mapping.values()]
    if missing:
        raise FileRejected(
            filename,
            [f"no column could be identified as {f!r}" for f in missing],
            headers, mapping,
            "A settlements list needs a credited amount and a date. An "
            "identifier is optional — one is derived from the date and amount "
            "when no column supplies it.",
        )

    # Resolve day-first vs month-first once, across the whole column. Sending
    # the raw string on and letting the browser's Date() guess is how an
    # Indian statement's 09/03/2026 arrived in the form as September 3rd.
    date_src = next((c for c, f in mapping.items() if f == "settled_at"), None)
    date_order, date_order_proven = "day", False
    if date_src:
        date_order, date_order_proven = resolve_day_order(
            [str(r.get(date_src, "") or "") for r in rows])

    out = []
    unreadable = 0
    for i, r in enumerate(rows):
        rec = {field: r.get(src) for src, field in mapping.items()}
        amount_raw = str(rec.get("net_amount") or "").strip()
        if not amount_raw:
            continue                      # a debit row on a bank statement
        try:
            if _clean_amount(amount_raw) <= 0:
                continue                  # a debit, or a zero line
        except Exception:
            # A non-empty amount that will not parse. This is a credit row
            # this parser could not read, not a row that is not a credit.
            unreadable += 1
            continue
        iso = normalize_date(str(rec.get("settled_at") or ""), date_order)
        if iso:
            rec["settled_at"] = iso

        if not str(rec.get("batch_id") or "").strip():
            # Derived, and visibly so. A synthetic id must never be mistaken
            # for one the bank assigned — a reviewer seeing CREDIT-… should
            # know at a glance that nothing in the file named this settlement,
            # and therefore that no reference can anchor it.
            date_part = str(rec.get("settled_at") or "")[:10] or f"row{i}"
            rec["batch_id"] = f"CREDIT-{date_part}-{amount_raw}"
            rec["_synthetic_id"] = True
        out.append(rec)

    if report is not None:
        report["unreadable_amounts"] = unreadable
        report["date_order"] = date_order
        # False means every date in the column was <= 12/12 and the order is
        # an assumption. The caller has to say so rather than present it as
        # something the file established.
        report["date_order_proven"] = date_order_proven
        report["credit_rows"] = len(out) + unreadable

    if not out:
        raise FileRejected(
            filename, ["no row carries a positive credited amount"],
            headers, mapping,
            "Every row's amount column was empty, unparseable or not a credit.",
        )
    return out


def parse_file_content(content: bytes, filename: str,
                       warnings_out: list[str] | None = None) -> list[dict[str, Any]]:
    """Determine file type and parse accordingly."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        # Fallback for Windows files
        text = content.decode("cp1252", errors="ignore")
        
    if filename.lower().endswith(".json"):
        return parse_json(text, filename, warnings_out)
    else:
        # Default to CSV
        return parse_csv(text, filename, warnings_out)
