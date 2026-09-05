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


DEFAULT_RATE_CARD = FeeRateCard(
    gateway_fee_bps=200,     # 2%
    flat_fee_cents=0,
    tax_withholding_bps=100,  # 1%
)


def compute_fee_breakdown(
    batch: SettlementBatch,
    rate_card: FeeRateCard = DEFAULT_RATE_CARD,
    candidates: list[NormalizedTxn] | None = None,
) -> FeeBreakdown:
    """
    Reconstruct the deductions that must be added back to net to reach the
    gross target.

    Two paths, and which one ran is recorded on the result:

      DECLARED   `batch.declared_deductions_cents` was supplied, so the exact
                 figure from the settlement advice is used. This is the
                 preferred path — the gross target is then a fact rather than
                 an inference, and no tolerance is being spent on fee drift.

      ESTIMATED  Reconstructed from the rate card. The subset-sum tolerance
                 band absorbs the error, but that error is real: if the
                 processor's actual deductions differ from the card, the
                 target moves and the true subset stops summing to it. The
                 match then fails, or worse, a different subset fits the
                 wrong target.

    The declared path is why the target survives contact with a processor
    whose rate card you do not have exactly right.
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

    # If item-level explicit fees are available in the candidates, use them as a factual fallback
    if candidates:
        explicit_fees = [t.extra.get("fee_amount_cents", 0) for t in candidates if "fee_amount_cents" in t.extra]
        if explicit_fees and sum(explicit_fees) > 0:
            return FeeBreakdown(
                batch_id=batch.batch_id,
                gateway_fee_cents=sum(explicit_fees),
                flat_fee_cents=rate_card.flat_fee_cents,
                tax_withholding_cents=0,
                basis="declared-item-level",
                split_known=True,
            )

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
