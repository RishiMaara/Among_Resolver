"""
Which records can be members of a settlement at all: the settlement's
currency only (amounts carry no unit), and only payments whose status
says the money moved.
"""

from __future__ import annotations

from schema import NormalizedTxn, SettlementBatch
import audit
import fx


# States that mean the money did not move. Deliberately a closed list of
# things that are unambiguous: anything unrecognised is treated as settling,
# because most feeds carry no status at all and guessing would discard real
# payments.
NON_SETTLING_STATUSES = {
    # Never went through.
    "failed", "failure", "fail", "declined", "rejected", "error",
    "cancelled", "canceled", "voided", "void", "expired", "timeout",
    "reversed", "rolled_back", "rollback",

    # Money authorised but not taken. The merchant can still walk away, so
    # nothing has settled — Stripe splits this across several names.
    "authorized", "authorised", "requires_capture", "uncaptured",
    "requires_payment_method", "requires_confirmation", "requires_action",

    # Started, not finished.
    "created", "initiated", "pending", "processing", "in_transit",
    "incomplete", "awaiting_payment",

    # The bank took it back or never posted it. A returned credit is money
    # that visibly arrived and then left, which is the case most likely to be
    # reconciled by mistake.
    "returned", "return", "bounced", "unposted", "not_posted",
    "on_hold", "held", "blocked", "frozen",
}


def _filter_to_settlement_currency(
    batch: SettlementBatch, candidates: list[NormalizedTxn]
) -> list[NormalizedTxn]:
    """Keep candidates in the settlement's currency; convert those with a declared rate."""
    # Currency first: amounts are integer minor units with no unit attached, so
    # the solver once cleared INR 300 from two INR legs and a USD leg at 0.97.
    # A foreign candidate enters only through a rate the settlement declares
    # (fx.py), converted exactly and carrying the rate as evidence.
    settlement_ccy = (batch.currency or "").strip().upper()
    if settlement_ccy:
        rates = {k.strip().upper(): v for k, v in (batch.fx_rates or {}).items()}
        kept: list[NormalizedTxn] = []
        converted: dict[str, int] = {}
        dropped: dict[str, int] = {}
        for t in candidates:
            ccy = (t.currency or "").strip().upper()
            if ccy == settlement_ccy:
                kept.append(t)
            elif ccy in rates:
                kept.append(fx.convert(t, settlement_ccy, rates[ccy]))
                converted[ccy] = converted.get(ccy, 0) + 1
            else:
                dropped[ccy or "?"] = dropped.get(ccy or "?", 0) + 1
        for ccy, n in sorted(converted.items()):
            audit.log_decision(
                batch_id=batch.batch_id, agent="currency_filter",
                detail=(f"Converted {n} {ccy} candidate(s) into {settlement_ccy} at the "
                        f"declared rate 1 {ccy} = {rates[ccy]} {settlement_ccy}, each rounded "
                        f"half-up to the minor unit. The original amount and the rate stay "
                        f"on the record; their fee fields are set aside, not audited."),
            )
        if dropped:
            audit.log_decision(
                batch_id=batch.batch_id,
                agent="currency_filter",
                detail=(
                    f"Excluded {sum(dropped.values())} candidate(s) denominated in "
                    f"{', '.join(sorted(dropped))} from this {settlement_ccy} "
                    f"settlement. Amounts are integer minor units with no unit "
                    f"attached, so summing across currencies produces an exact "
                    f"total that is meaningless. No rate was declared for "
                    f"{'this currency' if len(dropped) == 1 else 'these currencies'}, "
                    f"so none is converted."
                ),
            )
        return kept

    # No settlement currency declared: nothing to compare against, so nothing
    # is excluded. Guessing one would be worse than not filtering.
    return candidates


def _filter_out_non_settling(
    batch: SettlementBatch, candidates: list[NormalizedTxn]
) -> list[NormalizedTxn]:
    """Drop candidates whose status says the money never moved."""
    # Money that never moved is not a settlement member. Failed payments once
    # entered the pool as spendable and created impossible alternate subsets. Only
    # explicitly non-settling states are dropped; blank or unknown stays.
    # A zero amount moves no money either, and every subset ties with or
    # without it: a blank amount cell (read as 0) naming the settlement made
    # three blind-test settlements "ambiguous" that had one answer.
    dropped_status: dict[str, int] = {}
    settling = []
    for _t in candidates:
        _state = str((_t.extra or {}).get("status") or "").strip().lower().replace("-", "_")
        if _t.amount_cents == 0:
            dropped_status["zero amount"] = dropped_status.get("zero amount", 0) + 1
        elif _state in NON_SETTLING_STATUSES:
            dropped_status[_state] = dropped_status.get(_state, 0) + 1
        else:
            settling.append(_t)
    if dropped_status:
        _detail = ", ".join(f"{n} {k}" for k, n in sorted(dropped_status.items()))
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="status_filter",
            detail=(
                f"Excluded {sum(dropped_status.values())} candidate(s) whose status or amount "
                f"says no money moved ({_detail}). A failed or "
                f"uncaptured payment cannot be part of a settlement, and "
                f"leaving it in the pool invents subsets that cannot happen."
            ),
        )
    return settling
