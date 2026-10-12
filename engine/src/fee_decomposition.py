"""
Agent 2 — Fee/Charge Decomposition.

A bank settlement shows the NET amount (after gateway fees, flat fees,
tax withholding). The subset-sum engine (Agent 3) needs the GROSS target
— what the underlying transactions actually summed to before deductions.

This module reconstructs that target. Deterministic lookup + arithmetic,
not an LLM. Memo-based charge-type identification (e.g. "Strp_py_99" ->
Stripe payment) is semantic and belongs in fuzzy_match.py, not here.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass

from schema import FeeBreakdown, SettlementBatch, NormalizedTxn
from fee_model import MethodRateCard, stated_method
from linkage import txn_key

logger = logging.getLogger(__name__)


@dataclass
class FeeRateCard:
    """Per-source/region fee structure. Hackathon version: flat config.
    Production: this is a versioned table keyed by source + effective date,
    since fee rates change over time and you must reconstruct historical
    rates correctly for old settlements, not just current ones."""
    gateway_fee_bps: int          # basis points, e.g. 200 = 2.00%
    flat_fee_cents: int           # per-batch flat fee, if any
    tax_withholding_bps: int      # e.g. TDS-style withholding
    method_rates: MethodRateCard | None = None
    """Where a payment's method changes its fee: UPI, netbanking's flat
    charge, international cards. Cards, wallets and anything else pay
    gateway_fee_bps. None, or a card with no percentage fee (a feed already
    net of fees), estimates one rate for every payment."""


DEFAULT_RATE_CARD = FeeRateCard(
    gateway_fee_bps=200,     # 2%
    flat_fee_cents=0,
    tax_withholding_bps=100,  # 1%
    method_rates=MethodRateCard(),
)

def _method_fee(card: FeeRateCard, method: str | None, amount_cents: int) -> tuple[tuple, int]:
    """(the rule that applies, the fee in paise) for one payment."""
    rates = card.method_rates
    if rates is not None and method == "upi":
        rule: tuple = ("bps", rates.upi_bps)
    elif rates is not None and method == "netbanking":
        return ("flat", rates.netbanking_flat_cents), rates.netbanking_flat_cents
    elif rates is not None and method == "international_card":
        rule = ("bps", rates.international_card_bps)
    else:
        rule = ("bps", card.gateway_fee_bps)
    return rule, round(amount_cents * rule[1] / 10000)


def per_method_weights(card: FeeRateCard, candidates: list[NormalizedTxn]) -> dict[str, int] | None:
    """
    Each record's net contribution, when the pool mixes payment methods whose
    fees differ; None otherwise, and the single gross target stands.

    One rate card turns a net payout into one gross target, which is right
    only if every member paid the same rate. On a day of UPI at 0%, cards at
    2% and netbanking at Rs 5 a payment, the members' gross does not sum to
    it and the settlement was withheld. A payment's net, its amount less its
    own method's fee and the withholding, sums to the payout whichever
    payments are members, so the solver is given that and the net payout.
    Refunds and reversals contribute their amount. Records that state no
    method pay the card rate, as they did before.
    """
    if card.method_rates is None or card.gateway_fee_bps <= 0:
        return None
    weights: dict[str, int] = {}
    rules: set = set()
    for t in candidates:
        if t.amount_cents <= 0:
            weights[txn_key(t)] = t.amount_cents
            continue
        method = stated_method(t)
        rule, fee = _method_fee(card, method, t.amount_cents)
        tax = round(t.amount_cents * card.tax_withholding_bps / 10000)
        weights[txn_key(t)] = t.amount_cents - fee - tax
        if method is not None:
            rules.add(rule)
    return weights if len(rules) >= 2 else None


def per_method_breakdown(batch: SettlementBatch, card: FeeRateCard,
                         members: list[NormalizedTxn]) -> FeeBreakdown:
    """The deductions for the matched members, each at its own method's fee."""
    fees = taxes = 0
    for t in members:
        if t.amount_cents <= 0:
            continue
        fees += _method_fee(card, stated_method(t), t.amount_cents)[1]
        taxes += round(t.amount_cents * card.tax_withholding_bps / 10000)
    return FeeBreakdown(
        batch_id=batch.batch_id,
        gateway_fee_cents=fees,
        flat_fee_cents=card.flat_fee_cents,
        tax_withholding_cents=taxes,
        basis="estimated per payment method",
    )


def compute_fee_breakdown(
    batch: SettlementBatch,
    rate_card: FeeRateCard = DEFAULT_RATE_CARD,
    candidates: list[NormalizedTxn] | None = None,
) -> FeeBreakdown:
    """
    The deductions to add back to net to reach the gross target.

    DECLARED   batch.declared_deductions_cents from the settlement advice: the
               target is a fact. Preferred.
    ESTIMATED  from the rate card: if the processor's real deductions differ,
               the target moves and the true set stops summing to it.
    The basis is recorded on the result.
    """
    # A stated figure always beats a reconstructed one. The split between fee
    # and withheld tax is NOT inferred here — attributing a lump sum to one
    # component would misstate a recoverable tax receivable as an expense in
    # the posting proposal, so it is reported as unsplit instead.
    if batch.declared_deductions_cents is not None:
        return FeeBreakdown(
            batch_id=batch.batch_id,
            gateway_fee_cents=batch.declared_deductions_cents,
            flat_fee_cents=0,
            tax_withholding_cents=0,
            basis="declared",
            split_known=False,
        )

    # There was a third path here: sum `fee_amount_cents` over the candidates
    # and call it declared. The candidates are the whole windowed pool — other
    # settlements' payments and decoys included — and which of them are
    # members is exactly what has not been solved yet, so that sum was the
    # fees of the wrong set. It stayed invisible only because no source filled
    # the key; the moment the Razorpay feed did, a clean settlement stopped
    # tying out. Per-row fees are audited after the match (fee_audit.py), on
    # the members, which is the only set they can honestly be summed over.

    # Reverse-engineer approximate gross from net + rates.
    # gross - gross*(gw+tax) - flat = net  =>  gross = (net + flat) / (1 - gw - tax)
    #
    # NOTE: round(), not int()/truncation. int() truncates toward zero and
    # introduces a systematic -1 cent bias that can push the reconstructed
    # target just outside where the true subset actually sums — exactly the
    # kind of silent 1-cent drift that fails an audit. round() centers the
    # estimate instead of biasing it low.
    total_bps = rate_card.gateway_fee_bps + rate_card.tax_withholding_bps

    # A rate card with no percentage components is a real configuration, not a
    # degenerate one: some processors charge only a flat fee, and a feed that
    # already carries NET amounts needs a zero card because there is nothing
    # left to add back. Both previously divided by zero here.
    if total_bps == 0:
        return FeeBreakdown(
            batch_id=batch.batch_id,
            gateway_fee_cents=0,
            flat_fee_cents=rate_card.flat_fee_cents,
            tax_withholding_cents=0,
        )

    approx_gross = round(
        (batch.net_amount_cents + rate_card.flat_fee_cents) / (1 - total_bps / 10000)
    )
    deductions = approx_gross - batch.net_amount_cents

    gateway_fee = round(deductions * (rate_card.gateway_fee_bps / total_bps))
    tax_withholding = deductions - gateway_fee - rate_card.flat_fee_cents

    if tax_withholding < 0:
        # This means the flat_fee_cents on the rate card exceeds the total
        # deductions we computed from the net amount — the rate card is
        # internally inconsistent for this batch size. Clamping to zero
        # silently produces a wrong FeeBreakdown rather than a visible
        # problem. Log it so the operator can investigate.
        logger.warning(
            "Agent 2 [%s]: computed tax_withholding is negative (%d cents). "
            "The flat_fee_cents (%d) on the rate card exceeds the total "
            "deductions (%d) for this batch net amount (%d cents). "
            "Clamping to 0 — verify the rate card is correct for this batch size.",
            batch.batch_id,
            tax_withholding,
            rate_card.flat_fee_cents,
            deductions,
            batch.net_amount_cents,
        )
        tax_withholding = 0

    return FeeBreakdown(
        batch_id=batch.batch_id,
        gateway_fee_cents=gateway_fee,
        flat_fee_cents=rate_card.flat_fee_cents,
        tax_withholding_cents=tax_withholding,
    )
