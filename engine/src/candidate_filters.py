"""
Which records can be members of a settlement at all: the settlement's
currency only (amounts carry no unit), and only payments whose status
says the money moved.
"""

from __future__ import annotations

from schema import NormalizedTxn, SettlementBatch
import audit


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
    """Drop candidates denominated in a currency the settlement is not in."""
    # Currency first: amounts are integer minor units with no unit attached, so
    # the solver once cleared INR 300 from two INR legs and a USD leg at 0.97.
    # Foreign-currency candidates are excluded until FX conversion exists.
    settlement_ccy = (batch.currency or "").strip().upper()
    if settlement_ccy:
        same_ccy = [
            t for t in candidates
            if (t.currency or "").strip().upper() == settlement_ccy
        ]
        dropped_ccy = len(candidates) - len(same_ccy)
        if dropped_ccy:
            others = sorted({
                (t.currency or "?").strip().upper() for t in candidates
                if (t.currency or "").strip().upper() != settlement_ccy
            })
            audit.log_decision(
                batch_id=batch.batch_id,
                agent="currency_filter",
                detail=(
                    f"Excluded {dropped_ccy} candidate(s) denominated in "
                    f"{', '.join(others)} from this {settlement_ccy} "
                    f"settlement. Amounts are integer minor units with no unit "
                    f"attached, so summing across currencies produces an exact "
                    f"total that is meaningless. No FX conversion is applied."
                ),
            )
        return same_ccy

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
    dropped_status: dict[str, int] = {}
    settling = []
    for _t in candidates:
        _state = str((_t.extra or {}).get("status") or "").strip().lower().replace("-", "_")
        if _state in NON_SETTLING_STATUSES:
            dropped_status[_state] = dropped_status.get(_state, 0) + 1
        else:
            settling.append(_t)
    if dropped_status:
        _detail = ", ".join(f"{n} {k}" for k, n in sorted(dropped_status.items()))
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="status_filter",
            detail=(
                f"Excluded {sum(dropped_status.values())} candidate(s) whose "
                f"status says the money never moved ({_detail}). A failed or "
                f"uncaptured payment cannot be part of a settlement, and "
                f"leaving it in the pool invents subsets that cannot happen."
            ),
        )
    return settling
