"""
The same verdict, in the language of the person who has to act on it.

WHY
---
The engine's `reasoning` field is written for whoever has to debug the engine,
and it shows:

    "Withheld from auto-clear: linkage found no reference, cluster or
     cross-source evidence anywhere in this pool, so the match rests on the
     arithmetic alone. Routed for human review. Linkage: no_linkage_signal,
     structural confidence 0.22 over 3 linked candidate(s)."

Every load-bearing word in that sentence — linkage, cluster, cross-source,
no_linkage_signal, structural confidence — is this codebase's vocabulary, not
a finance team's. The amounts are worse: they are integer paise, so a reader
sees "5000000c" where they think in rupees, and "-4999900c" for a shortfall
that is really about fifty thousand rupees.

The technical text is not wrong and is not removed. It is the record, and an
engineer reading an audit trail needs exactly those words. What changes is
which one a reviewer meets first: they get a plain statement of what happened
and what to do, and the technical line stays one click away.

WHAT THIS IS NOT
----------------
Not a second opinion. Every number here is read off the same report the
technical line describes, so the two cannot disagree — if this text says the
match is short by fifty thousand rupees, that is the same residual, divided
by a hundred.
"""

from __future__ import annotations


def rupees(cents: int | float | None) -> str:
    """Integer paise as a figure a person reads. 5000000 -> 'Rs 50,000.00'."""
    if cents is None:
        return "an unknown amount"
    whole = abs(int(cents)) / 100
    sign = "-" if int(cents) < 0 else ""
    # Indian grouping: the last three digits, then pairs.
    units, paise = f"{whole:.2f}".split(".")
    if len(units) > 3:
        head, tail = units[:-3], units[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        units = ",".join(parts) + "," + tail
    return f"{sign}Rs {units}.{paise}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def plain_summary(summary: dict, reasoning: str = "",
                  interchangeable: dict | None = None) -> str:
    """
    One paragraph a finance reviewer can act on, built from the report.

    The four outcomes a reviewer actually distinguishes are: it cleared, it
    added up but nothing corroborates it, two answers both fit, and nothing
    adds up. Each needs a different action, and the technical text made them
    look alike because they all begin with jargon.
    """
    matched = summary.get("matched_count") or 0
    total = summary.get("total_candidates") or 0
    target = summary.get("target_cents")
    residual = summary.get("tie_out_residual_cents") or 0
    confidence = summary.get("confidence") or 0.0
    exceptions = summary.get("exception_count") or 0
    fee_basis = summary.get("fee_basis")

    tail = ""
    if exceptions:
        tail += (
            f" {_plural(exceptions, 'payment')} in this batch also need "
            f"looking at separately."
        )
    if fee_basis == "estimated":
        tail += (
            " Note the fees were estimated rather than taken from your figures,"
            " so the amount being searched for may be slightly off."
        )

    # 1. Cleared.
    if summary.get("cleared"):
        return (
            f"Matched. We identified the {_plural(matched, 'payment')} that make "
            f"up this settlement, and they add up to the expected "
            f"{rupees(target)} exactly, with nothing left over. Confidence "
            f"{confidence:.0%}. No action needed." + tail
        )

    # 2-4. Withheld. `ambiguous` alone cannot say why — it is set from three
    # different situations that call for three different actions — so the
    # engine records which, and this reads it rather than guessing. Guessing
    # here previously told a reviewer "more than one set adds up" for a batch
    # where exactly one did.
    reason = summary.get("withheld_reason")

    if reason == "alternate_subset":
        # Two very different situations arrive here. Sometimes the alternates
        # are genuinely different payments that happen to sum alike, and the
        # choice is real. Sometimes the payments are identical in every field
        # a reviewer can see — same amount, same second, same reference — and
        # there is nothing to choose between them. Telling someone to "look at
        # the candidates and choose" in the second case is asking them to
        # produce an arbitrary answer and call it a decision.
        if interchangeable and interchangeable.get("wholly_interchangeable"):
            groups = interchangeable["groups"]
            picked_total = sum(g["picked"] for g in groups)
            detail = " and ".join(
                f"{_plural(g['picked'], 'payment')} of "
                f"{rupees(g['amount_cents'])} (there are "
                f"{g['identical_available']} identical ones in the file)"
                for g in groups[:2]
            )
            return (
                f"Nothing to choose between. This settlement of {rupees(target)} "
                f"is {detail} — same amount, same reference, and nothing else "
                f"in the file tells them apart. Any "
                f"{picked_total} of them give the same correct total, so which "
                f"ones we name is arbitrary and picking differently would not "
                f"change the answer. What is worth checking is not which we "
                f"chose, but that these are not also being claimed by another "
                f"settlement." + tail
            )

        return (
            f"Needs your decision. More than one different set of payments adds "
            f"up to exactly {rupees(target)}, so the arithmetic alone cannot say "
            f"which set is the real one. We have not guessed. Someone needs to "
            f"look at the candidates and choose." + tail
        )

    if reason == "already_settled_elsewhere":
        return (
            f"Needs review — these payments look already paid out. The "
            f"arithmetic works: {_plural(matched, 'payment')} add up to "
            f"{rupees(target)}. But an earlier settlement already cleared on "
            f"most of them, and a payment can only be paid out once. Either "
            f"this settlement or the earlier one is wrong, and nothing has "
            f"been changed automatically because deciding which needs facts "
            f"we do not have." + tail
        )

    if reason == "no_corroborating_evidence":
        return (
            f"Needs review. We found {_plural(matched, 'payment')} that add up to "
            f"{rupees(target)} exactly — but nothing in your files ties them to "
            f"this settlement: no matching reference number, and no second "
            f"source agreeing. Amounts can line up by coincidence, so we have "
            f"not cleared it on the arithmetic alone. Please confirm these are "
            f"the right payments." + tail
        )

    if reason == "below_confidence_gate":
        return (
            f"Needs review. We found {_plural(matched, 'payment')} adding up to "
            f"{rupees(target)}, but the supporting evidence is thinner than our "
            f"threshold for clearing without a person ({confidence:.0%} against "
            f"a bar of 85%). The arithmetic is right; how sure we are that these "
            f"are the correct payments is the question. Please confirm." + tail
        )

    if summary.get("ambiguous") and matched and residual == 0:
        # Withheld, but the reason was not recorded — an older run, or a path
        # that has not been taught to say why. Describe only what is certain.
        return (
            f"Needs review. We found {_plural(matched, 'payment')} that add up to "
            f"{rupees(target)} exactly, but this batch was held back for a "
            f"person to confirm rather than cleared automatically. See the "
            f"technical detail for why." + tail
        )

    # 4. Nothing adds up.
    if matched:
        return (
            f"No match. Nothing in the {_plural(total, 'payment')} we looked at "
            f"adds up to {rupees(target)}. The closest we can offer is "
            f"{_plural(matched, 'payment')} that look related, but they come to "
            f"{rupees(abs(residual))} {'over' if residual > 0 else 'short of'} "
            f"the figure. Usually that means a payment is missing from the file, "
            f"or the settlement amount or the fees are not what we were told." + tail
        )

    return (
        f"No match. None of the {_plural(total, 'payment')} in this window can be "
        f"combined to reach {rupees(target)}. Either the payments that make up "
        f"this settlement are not in the file, or they fall outside the date "
        f"range being searched." + tail
    )
