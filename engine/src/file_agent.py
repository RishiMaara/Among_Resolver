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
from typing import Any


# Shared with ingestion so the two stages agree on what parses.
from ingestion import AmountUnreadable  # noqa: F401 - re-exported


logger = logging.getLogger(__name__)

# Split by layer — values, schema, header mapping, settlements lists — with
# every name still importable from here.
from file_values import (  # noqa: E402,F401 - re-exported; file_agent is the facade
    _clean_amount,
    _SLASH_DATE,
    resolve_day_order,
    normalize_date,
    _looks_like_date,
    _looks_like_amount,
    _looks_like_identifier,
)
from file_schema import (  # noqa: E402,F401 - re-exported; file_agent is the facade
    TARGET_FIELDS,
    HEADER_SYNONYMS,
    AMOUNT_DISQUALIFIERS,
    MULTI_VALUED_TARGETS,
    COMPLEMENTARY_AMOUNT_TOKENS,
    REQUIRED_FIELDS,
    IDENTITY_FIELDS,
    WHY_REQUIRED,
    FIELD_IN_PLAIN_ENGLISH,
    plain_rejection,
    FileRejected,
    _validate_schema,
    _validate_rows,
)
from header_mapping import (  # noqa: E402,F401 - re-exported; file_agent is the facade
    HeaderMappingReport,
    _missing_required,
    _constrain,
    _INFER_SAMPLE,
    _values,
    infer_mapping_from_values,
    composite_key_partner,
    check_identity_column,
    resolve_headers,
    _score_pairs,
    map_headers_with_report,
    _map_headers,
)
from settlement_list import (  # noqa: E402,F401 - re-exported; file_agent is the facade
    SETTLEMENT_SYNONYMS,
    _settlement_key,
    _settlement_columns,
    parse_settlements,
)


def _set_aside_unreadable(filename: str, unreadable: list[str], readable: int,
                          mapping: dict, warnings_out: list[str] | None) -> None:
    """
    Rows whose amount cannot be read are set aside and named, never read as a
    number. One garbled cell used to refuse the whole export, which a blind
    test showed cost 9 of 237 settlements that rule-based tools reconcile by
    skipping the row. The rest still clears only if it ties exactly, so a
    set-aside row that belongs to the settlement withholds it rather than
    being guessed. More than a tenth unreadable is a broken column or format,
    and the file is refused, naming the first rows.
    """
    if not unreadable:
        return
    if readable == 0 or len(unreadable) > max(1, (readable + len(unreadable)) // 10):
        raise FileRejected(
            filename, unreadable[:5],
            list(mapping.keys()) if isinstance(mapping, dict) else [], {},
            "Fix the amounts in those rows, or remove them. This engine will "
            "not guess a settlement amount.",
        )
    if warnings_out is not None:
        warnings_out.extend(
            f"{r} — set aside, not read as any amount. If that payment belongs to "
            f"this settlement the total will not tie, and nothing clears."
            for r in unreadable)


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
    unreadable: list[str] = []
    for _row_no, row in enumerate(raw_rows, start=1):
        mapped_row: dict[str, Any] = {
            "txn_id": "",
            "ref_id": "",
            "amount": 0.0,
            "currency": "",      # blank, not "INR" — see below
            "timestamp": "",
            "memo": "",
            "status": "",
        }
        
        set_aside = False
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
                # An amount this parser cannot read never becomes a number and
                # never crashes the request: the row is set aside and named
                # (_set_aside_unreadable), or the file refused if many are.
                try:
                    mapped_row[target_col] = _clean_amount(val)
                except AmountUnreadable as exc:
                    unreadable.append(f"row {_row_no}, column {raw_col!r}: {exc}")
                    set_aside = True
                    break
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
                
        if set_aside:
            continue
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

    _set_aside_unreadable(filename_hint, unreadable, len(results), mapping, warnings_out)
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
    all_keys: set[str] = set()
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
    unreadable: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
            
        mapped_row: dict[str, Any] = {
            "txn_id": "",
            "ref_id": "",
            "amount": 0.0,
            "currency": "",      # blank, not "INR" — see below
            "timestamp": "",
            "memo": "",
            "status": "",
        }
        
        set_aside = False
        for raw_col, target_col in mapping.items():
            val = item.get(raw_col, "")
            raw_str = str(val).strip()
            # Same skip-blank-overwrite fix as parse_csv — see that
            # function's comment for why this matters.
            if not raw_str:
                continue
            if target_col == "amount":
                # An amount this parser cannot read never becomes a number and
                # never crashes the request: the row is set aside and named
                # (_set_aside_unreadable), or the file refused if many are.
                try:
                    mapped_row[target_col] = _clean_amount(val)
                except AmountUnreadable as exc:
                    unreadable.append(f"row {_row_no}, column {raw_col!r}: {exc}")
                    set_aside = True
                    break
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
                
        if set_aside:
            continue
        if not mapped_row["txn_id"] and mapped_row["ref_id"]:
            mapped_row["txn_id"] = mapped_row["ref_id"]
        elif not mapped_row["ref_id"] and mapped_row["txn_id"]:
            mapped_row["ref_id"] = mapped_row["txn_id"]
            
        results.append(mapped_row)

    _set_aside_unreadable(filename_hint, unreadable, len(results), mapping, warnings_out)
    _validate_rows(filename_hint, results, headers, mapping)
    return results


def parse_file_content(content: bytes, filename: str,
                       warnings_out: list[str] | None = None,
                       scan_text: str = "") -> list[dict[str, Any]]:
    """Determine file type and parse accordingly.

    scan_text: what OCR in the browser read, if the file is a scanned statement.
    """
    import statement_parsers  # pylint: disable=import-outside-toplevel
    if statement_parsers.detect(content, filename):
        return _parse_statement(content, filename, warnings_out, scan_text)
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


def _parse_statement(content: bytes, filename: str,
                     warnings_out: list[str] | None,
                     scan_text: str = "") -> list[dict[str, Any]]:
    """
    MT940, CAMT.053, OFX or PDF, read and then proved.

    A statement that does not balance is refused whole: a misread line means
    the reconciliation would be wrong in a way nothing downstream can see.
    """
    import statement_parsers  # pylint: disable=import-outside-toplevel
    try:
        st, check = statement_parsers.parse(content, filename, scan_text=scan_text)
    except statement_parsers.StatementUnreadable as exc:
        raise FileRejected(filename, [str(exc)], plain=(
            f"'{filename}' looks like a bank statement but could not be read: {exc}"))
    rule = check.get("golden_rule")
    if rule and rule["checkable"] and not check["holds"]:
        where = check.get("running_balance") or {}
        extra = (f" The first line that breaks the running balance is line "
                 f"{where['first_bad_line'] + 1}." if where.get("first_bad_line") is not None else "")
        raise FileRejected(filename, ["the statement does not balance"], plain=(
            f"'{filename}' was read as {st.format.upper()} but it does not balance. "
            f"{check['plain']}{extra} Either a line was misread or the statement is "
            f"incomplete, so nothing from it was used."))
    rows = statement_parsers.to_rows(st)
    if warnings_out is not None:
        debits = sum(1 for ln in st.lines if ln.amount_cents < 0)
        warnings_out.append(
            f"Read as a {st.format.upper()} bank statement: {len(st.lines)} line(s). "
            f"{check['plain']} {len(rows)} credit(s) go to reconciliation; {debits} "
            f"debit(s) stay out, since money leaving cannot make up a settlement credit."
            + (" " + " ".join(st.notes) if st.notes else ""))
    if not rows:
        raise FileRejected(filename, ["the statement has no credits"], plain=(
            f"'{filename}' balances but has no credits, so there is nothing a "
            f"settlement could have arrived as."))
    return rows
