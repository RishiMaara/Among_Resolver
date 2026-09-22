"""
Reading a settlements list, or the credit lines of a bank statement, as
settlements. Split out of file_agent.py, unchanged; file_agent re-exports it.
"""

from __future__ import annotations

import csv
import io
import json
import re

from file_values import _clean_amount, _looks_like_date, normalize_date, resolve_day_order
from file_schema import FileRejected


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
