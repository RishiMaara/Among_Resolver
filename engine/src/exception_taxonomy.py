"""
The categories a finance team files breaks under, with an owner and a next step.

WHY
---
The engine's exception reasons — timing_lag, duplicate, missing_entry —
describe what the MATCHER saw. A reviewer needs a different question
answered: what kind of break is this in the books, whose desk does it go
to, and what do they do first? Those are the categories reconciliation teams
already sort by (in transit, missing in bank, missing in books, duplicate,
amount mismatch, compliance hold), and exceptions filed under them can be
routed without re-reading each one.

Where the feed an unmatched record came from is known, it decides the
category: a gateway payment with no bank or ledger counterpart is "missing
in books" or "not yet paid out", a bank credit with nothing behind it is an
"unidentified receipt". The mapping is fixed rules, not a model — a
misfiled break costs a day in the wrong queue, and a rule can be read and
corrected.
"""

from __future__ import annotations

from collections import Counter

CATEGORIES = {
    "in_transit": {
        "label": "In transit (timing difference)",
        "owner": "nobody yet — wait",
        "next_action": ("Nothing to fix. The counterpart is dated just outside this "
                        "settlement; it should appear in the next payout. Re-check "
                        "after its due date."),
    },
    "missing_in_books": {
        "label": "Missing in books",
        "owner": "accounts (ledger)",
        "next_action": ("The gateway took this payment and the ledger has no entry for "
                        "it. Book the sale, or find it under another reference."),
    },
    "unidentified_receipt": {
        "label": "Unidentified receipt",
        "owner": "treasury",
        "next_action": ("Money arrived with nothing behind it. Identify the payer from "
                        "the narration or UTR before crediting any customer."),
    },
    "not_in_gateway": {
        "label": "Booked but not at the gateway",
        "owner": "accounts (ledger)",
        "next_action": ("The ledger shows a sale the gateway has no record of. Check "
                        "whether it was paid by another method or booked in error."),
    },
    "duplicate": {
        "label": "Duplicate record",
        "owner": "whoever owns the feed",
        "next_action": ("The same payment appears twice. Remove the duplicate at the "
                        "source; do not reverse it here."),
    },
    "split_or_partial": {
        "label": "Split or partial payment",
        "owner": "accounts receivable",
        "next_action": ("One leg of a payment made in parts. Find the other leg(s) "
                        "before matching either."),
    },
    "amount_mismatch": {
        "label": "Amount or reference mismatch",
        "owner": "accounts receivable",
        "next_action": ("A likely counterpart exists but does not agree exactly. "
                        "Compare the two records; the difference is usually a fee, "
                        "a rounding or a mistyped reference."),
    },
    "compliance_hold": {
        "label": "Compliance hold",
        "owner": "compliance",
        "next_action": ("Held by a screening rule. Follow the remediation on the "
                        "finding; do not release until compliance signs off."),
    },
    "unidentified": {
        "label": "Needs investigation",
        "owner": "reconciliation team",
        "next_action": ("No pattern explains it. Look at the record and its nearest "
                        "neighbours by amount and date."),
    },
}


def _source_of(ids: list[str], sources: dict[str, set[str]]) -> set[str]:
    found: set[str] = set()
    for i in ids or []:
        found |= sources.get(i, set())
    return found


def categorise(exc: dict, sources: dict[str, set[str]]) -> str:
    """One exception's category, from its reason and where its records came from."""
    reason = exc.get("reason")
    feeds = _source_of(exc.get("candidate_txn_ids") or [], sources)
    if reason == "compliance_block":
        return "compliance_hold"
    if reason == "timing_lag":
        return "in_transit"
    if reason == "duplicate":
        return "duplicate"
    if reason == "partial_payment":
        return "split_or_partial"
    if reason == "low_confidence":
        return "amount_mismatch"
    if reason == "missing_entry":
        if feeds == {"bank"}:
            return "unidentified_receipt"
        if feeds == {"erp"}:
            return "not_in_gateway"
        if "gateway" in feeds:
            return "missing_in_books"
    return "unidentified"


def annotate(exceptions: list[dict], candidates: list | None) -> tuple[list[dict], dict]:
    """Add category, label, owner and next action to each exception; count them."""
    sources: dict[str, set[str]] = {}
    for t in candidates or []:
        sources.setdefault(t.source_txn_id, set()).add(t.source.value)
    counts: Counter = Counter()
    value: Counter = Counter()
    for e in exceptions:
        cat = categorise(e, sources)
        meta = CATEGORIES[cat]
        e["category"] = cat
        e["category_label"] = meta["label"]
        e["owner"] = meta["owner"]
        e["next_action"] = meta["next_action"]
        counts[cat] += 1
        value[cat] += abs(int(e.get("amount_at_stake_cents") or 0))
    by_category = [
        {"category": c, "label": CATEGORIES[c]["label"], "owner": CATEGORIES[c]["owner"],
         "count": n, "value_cents": value[c]}
        for c, n in counts.most_common()
    ]
    return exceptions, {"by_category": by_category}
