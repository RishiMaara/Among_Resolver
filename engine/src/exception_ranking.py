"""
Exceptions ranked by the money waiting on them.

WHY ORDER IS A DECISION, NOT A DISPLAY DETAIL
---------------------------------------------
An exception list is a work queue. A reviewer with twenty minutes works it
top to bottom, the results screen shows the first ten, run history keeps the
first five hundred, and settlement Q&A grounds its answers on the first few.
So whatever sits at the top is what gets looked at — and the list used to be
in whatever order the pipeline happened to raise them, which put a ₹40 memo
mismatch above a ₹4,00,000 unresolved leg as often as not.

Now each exception carries the rupees at stake in it, and the list is sorted
by that, largest first. A batch-level summary states the total value waiting
on a human and what share of the settlement it is — the number a controller
asks for first ("how much is stuck?") and the one a raw count hides.

HOW "AT STAKE" IS COUNTED
-------------------------
The absolute amount of every transaction an exception names — a refund at
stake is as much money as a payment. Two things would inflate it, and both
are prevented:

  * one payment named by two exceptions. The per-exception figures honestly
    each include it; the batch total counts it once.
  * one id appearing in two feeds (a gateway payment and an unrelated ERP
    line both called "1001"). An exception names bare ids, so it cannot say
    which record it meant; the larger of the two is used rather than their
    sum, so a collision can never double a figure.

An exception whose transactions are not in the pool at all is kept and marked
unpriced rather than given a zero that would read as "nothing at stake".
"""

from __future__ import annotations

# Tie-break after amount: a legal block outranks an arithmetic one of equal
# value, because it is the one with a deadline attached.
_REASON_PRIORITY = {"compliance_block": 0}


def amount_index(candidates) -> dict[str, int]:
    """source_txn_id -> the largest absolute amount any record with that id carries."""
    index: dict[str, int] = {}
    for t in candidates or []:
        amount = abs(int(getattr(t, "amount_cents", 0) or 0))
        tid = getattr(t, "source_txn_id", None)
        if tid is not None and amount > index.get(tid, -1):
            index[tid] = amount
    return index


def rank(exceptions: list[dict], candidates, target_cents: int | None = None
         ) -> tuple[list[dict], dict]:
    """
    Annotate each exception with what is at stake, sort by it, and summarise.

    Returns (ranked_exceptions, summary). The input dicts are copied, not
    mutated, so a caller holding the original list is not surprised.
    """
    index = amount_index(candidates)
    annotated = []
    for position, exc in enumerate(exceptions):
        ids = list(dict.fromkeys(exc.get("candidate_txn_ids") or []))
        priced = [i for i in ids if i in index]
        e = dict(exc)
        e["amount_at_stake_cents"] = sum(index[i] for i in priced)
        e["amount_known"] = bool(ids) and len(priced) == len(ids)
        e["_position"] = position
        annotated.append(e)

    annotated.sort(key=lambda e: (
        -e["amount_at_stake_cents"],
        _REASON_PRIORITY.get(e.get("reason"), 1),
        -len(e.get("candidate_txn_ids") or []),
        e["_position"],                       # stable: equal cases keep order
    ))
    for n, e in enumerate(annotated, 1):
        e["rank"] = n
        del e["_position"]

    at_stake_ids = {
        i for e in annotated for i in (e.get("candidate_txn_ids") or []) if i in index
    }
    total = sum(index[i] for i in at_stake_ids)
    summary = {
        "count": len(annotated),
        "total_at_stake_cents": total,
        "unpriced_count": sum(1 for e in annotated if not e["amount_known"]),
        "share_of_target": (round(total / target_cents, 4)
                            if target_cents and target_cents > 0 else None),
        "ordering": "amount at stake, largest first",
    }
    return annotated, summary
