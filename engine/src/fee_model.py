"""
What a fee or tax finding is, and reading a row's fee fields: the types and
helpers the fee checks (fee_audit.py) and the tax checks (tax_audit.py) share.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import india_tax


class FeeAuditSeverity(str, Enum):
    INFO = "info"          # within tolerance, noted for completeness
    WARNING = "warning"    # outside tolerance, review recommended
    HIGH = "high"          # material discrepancy, action required


class FeeAuditCategory(str, Enum):
    FEE_OVERCHARGE = "fee_overcharge"
    FEE_UNDERCHARGE = "fee_undercharge"
    GST_MISCALCULATION = "gst_miscalculation"
    TDS_WITHHOLDING_ERROR = "tds_withholding_error"
    TCS_COLLECTION_ERROR = "tcs_collection_error"
    SETTLEMENT_SHORTFALL = "settlement_shortfall"
    DUPLICATE_FEE = "duplicate_fee"


@dataclass
class FeeAuditFinding:
    """A single finding from the fee audit."""
    category: FeeAuditCategory
    severity: FeeAuditSeverity
    txn_id: str                      # which transaction, or "batch" for batch-level
    expected_cents: int              # what the rate card says
    actual_cents: int                # what the gateway charged
    difference_cents: int            # actual - expected (positive = overcharge)
    description: str                 # human-readable explanation
    payment_method: str = ""         # card / upi / netbanking / wallet / unknown
    rule_basis: str = ""             # statutory / contractual / internal
    citation: str = ""               # the provision, for statutory findings


@dataclass
class MethodRateCard:
    """Per-payment-method fee structure for Indian gateways.

    All rates in basis points (1 bp = 0.01%) unless marked as flat (cents).
    This models the actual Razorpay/Stripe India pricing structure.
    """
    card_bps: int = 200              # 2.00% — typical for domestic cards
    upi_bps: int = 0                 # 0% — MDR waived since Jan 2020
    netbanking_flat_cents: int = 500 # Rs 5 flat per transaction
    wallet_bps: int = 200            # ~2%
    international_card_bps: int = 300  # 3% — higher for cross-border
    default_bps: int = 200           # fallback for unknown methods

    gst_rate_bps: int = india_tax.GST_ON_SERVICES_BPS  # 18% on the fee (CGST 9% + SGST 9%)

    # E-commerce TDS (194-O, then 393(1) Sl. 8(v)) on gross credited.
    #
    # None means "the statutory rate on each payment's date", read from
    # india_tax.TDS_ECOMMERCE. This used to be one number, 1%, and it was
    # wrong for two years after the Finance (No. 2) Act 2024 cut the rate to
    # 0.1% from 1 October 2024 — ten times the correct withholding expected on
    # every settlement. A number here overrides the schedule for every date;
    # 0 switches the check off, because whether it applies at all depends on
    # the merchant's arrangement rather than anything this engine can see.
    tds_rate_bps: int | None = None
    # Rs 5,00,000 a year, for individual and HUF sellers only. Other sellers
    # have no threshold: set 0, which means "none" rather than "off".
    tds_annual_threshold_cents: int = 500_000_00

    # GST TCS under Section 52 CGST. None means the statutory rate on each
    # payment's date (0.5% since 10 July 2024). Checked only when the data
    # reports TCS, because whether a gateway is the operator collecting it is
    # not something the engine can know; 0 switches it off.
    tcs_rate_bps: int | None = None

    # How far actual can deviate from expected before it is flagged (in bps of txn amount)
    tolerance_bps: int = 10          # 0.10% tolerance


def _paise(value) -> int | None:
    """A money cell from a file, read the way the amount column is read."""
    if value is None or str(value).strip() == "":
        return None
    from ingestion import normalize_amount_to_cents
    try:
        return normalize_amount_to_cents(value)
    except (ValueError, TypeError):
        return None


def _stated_paise(extra: dict, cents_key: str, *file_keys: str) -> int | None:
    if cents_key in extra and extra[cents_key] is not None:
        return int(extra[cents_key])
    for k in file_keys:
        v = _paise(extra.get(k))
        if v is not None:
            return v
    return None


def _gross_cents(txn) -> int:
    """The amount a fee is charged on: gross where the source states it."""
    extra = getattr(txn, "extra", {}) or {}
    stated = extra.get("gross_amount_cents")
    if stated is not None:
        return int(stated)
    return getattr(txn, "amount_cents", 0)
