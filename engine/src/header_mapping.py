"""
Mapping a file's column headers onto the engine's fields: synonyms, fuzzy
scores, inference from the values themselves, and the model-assisted pass
when those fail. Split out of file_agent.py, unchanged; file_agent re-exports it.
"""

from __future__ import annotations

import logging

from dataclasses import dataclass, field
from rapidfuzz import process, fuzz
import llm_header_mapper

from file_values import _looks_like_amount, _looks_like_date, _looks_like_identifier
from file_schema import (AMOUNT_DISQUALIFIERS, COMPLEMENTARY_AMOUNT_TOKENS, HEADER_SYNONYMS,
                         IDENTITY_FIELDS, MULTI_VALUED_TARGETS, REQUIRED_FIELDS, TARGET_FIELDS)

# The logger keeps the name it always had, so nothing reading it changes.
logger = logging.getLogger("file_agent")


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
