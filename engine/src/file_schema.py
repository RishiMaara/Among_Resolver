"""
What an uploaded file must provide — the field vocabulary, what is required and
why — and the refusal when it does not. Split out of file_agent.py, unchanged;
file_agent re-exports it.
"""

from __future__ import annotations

import logging

import dateutil.parser as dateparser
from ingestion import _fast_parse

# The logger keeps the name it always had, so nothing reading it changes.
logger = logging.getLogger("file_agent")


# The six fields reconciliation needs plus the four compliance needs
# (payer/payee, cash, wire). Without the counterparty columns every uploaded
# file screened as one payer and no rule could fire.
TARGET_FIELDS = ["txn_id", "ref_id", "amount", "currency", "timestamp", "memo", "status",
                 "payer_id", "payee_id", "is_cash", "is_wire_transfer"]


# Synonyms anchor the fuzzy matcher. Single generic words are dangerous under
# token_set_ratio: "value" once matched "Value Date" as the amount and
# emptied the timestamp. Prefer specific phrases.
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
    # Widened to cover real export schemes, but deliberately NOT with "gross" or
    # "net": that flipped ReconRiver's feed to gross_amount and took it from
    # 94.59% to 8.11%. With several numeric columns, the name rules decide.
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


# Fields without which reconciliation is impossible. A missing one does not
# produce a bad answer, it produces silent absence (rows dropped, "nothing
# matched"), so the file is refused instead.
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
