"""
Indian Fee and Tax Audit — post-reconciliation fee verification.

WHY THIS EXISTS
---------------
Indian payment gateways charge per-method fees: UPI near 0%, cards around 2%,
netbanking often a flat fee, wallets somewhere in between. On top of the fee,
GST at 18% (CGST 9% + SGST 9%) applies to the FEE, not the principal. An
e-commerce operator also deducts TDS on the gross it credits a seller —
Section 194-O until 31 March 2026, Section 393(1) Table Sl. 8(v) of the
Income-tax Act 2025 from 1 April 2026, 0.1% under both — and collects GST TCS
under Section 52 of the CGST Act on net taxable supplies, 0.5% since 10 July
2024. Rates and citations come from dated schedules in `india_tax.py`, so each
payment is checked under the law in force on its own date. Whether either
applies to a given merchant is a question about that merchant's arrangement,
not about this engine, so every check is configurable and the defaults are
starting points rather than advice.

The reconciliation engine currently reconstructs a gross target from a single
blended rate card per batch. That is the limitation the README calls "most
likely to meet a real merchant first", because a mixed-method day produces a
wrong gross target that ties out to nothing and the batch is withheld rather
than mismatched.

This module runs AFTER reconciliation, on the matched set, and audits each
transaction's actual fee against what a method-aware rate card predicts.
It catches:

  * Overcharges — the gateway charged more than the agreed rate for that method
  * Undercharges — rarer, but a fee below the contractual minimum is a
    reconciliation risk because the net deposit will be higher than expected
  * GST miscalculations — 18% must apply to the fee, not the principal
  * TDS withholding errors — 194-O / 393(1), by payment date; the threshold
    is configurable because it applies to individual and HUF sellers only
  * TCS collection errors — Section 52 CGST, checked when the data reports TCS
  * Settlement shortfalls — the sum of item-level fees does not equal the
    batch-level deduction
  * Duplicate fee deductions

WHAT IT DOES NOT DO
-------------------
This module reports. It does not adjust the reconciliation. A fee overcharge
is a revenue-leakage finding, not a matching correction — the settlement DID
clear at the net amount, and the finding is that the net amount was wrong by
the overcharge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from enum import Enum

import india_tax

logger = logging.getLogger(__name__)


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


def _detect_payment_method(txn) -> str:
    """Extract payment method from a transaction's extra dict or memo.

    Indian gateways typically include a 'method' or 'payment_method' field.
    If not present, attempt to infer from the memo.
    """
    extra = getattr(txn, "extra", {}) or {}

    # Direct field
    for key in ("payment_method", "method", "pay_method", "instrument"):
        val = str(extra.get(key, "")).strip().lower()
        if val:
            return _normalize_method(val)

    # Memo-based inference (fragile, noted as such)
    memo = getattr(txn, "memo_normalized", "") or ""
    if "upi" in memo:
        return "upi"
    if "card" in memo or "visa" in memo or "mastercard" in memo or "rupay" in memo:
        return "card"
    if "netbanking" in memo or "neft" in memo or "imps" in memo:
        return "netbanking"
    if "wallet" in memo or "paytm" in memo or "phonepe" in memo:
        return "wallet"

    return "unknown"


def _normalize_method(raw: str) -> str:
    """Map gateway-specific method names to canonical ones."""
    raw = raw.lower().strip()
    if raw in ("card", "credit_card", "debit_card", "cc", "dc", "visa",
               "mastercard", "rupay", "amex"):
        return "card"
    if raw in ("upi", "upi_collect", "upi_intent", "upi_qr"):
        return "upi"
    if raw in ("netbanking", "net_banking", "neft", "imps", "rtgs"):
        return "netbanking"
    if raw in ("wallet", "paytm", "phonepe", "mobikwik", "freecharge",
               "amazon_pay", "airtel_money"):
        return "wallet"
    if raw in ("international", "intl_card", "international_card"):
        return "international_card"
    return raw


def _paise(value) -> int | None:
    """A money cell from a file, read the way the amount column is read."""
    if value is None or str(value).strip() == "":
        return None
    from ingestion import normalize_amount_to_cents
    try:
        return normalize_amount_to_cents(value)
    except (ValueError, TypeError):
        return None


def fee_fields(extra: dict) -> tuple[int | None, int | None]:
    """
    (fee before GST, GST on it) in paise, or None where the data is silent.

    Two conventions arrive here. The engine's own keys, `fee_amount_cents`
    and `gst_amount_cents`, hold the fee EXCLUSIVE of GST. A Razorpay export
    names its columns `fee` and `tax`, and its `fee` INCLUDES the tax — its
    API reference shows a transfer of 100000 paise with fee 296, tax 46 and
    a debit of 100296. Reading Razorpay's fee as pre-GST would expect 18% GST
    on a figure that already contains it, and flag every row.
    """
    if "fee_amount_cents" in extra:
        fee = int(extra["fee_amount_cents"])
        gst = extra.get("gst_amount_cents")
        return fee, (int(gst) if gst is not None else None)
    gross_fee = _paise(extra.get("fee", extra.get("fees")))
    if gross_fee is None:
        return None, None
    tax = _paise(extra.get("tax", extra.get("gst")))
    if tax is None:
        return gross_fee, None
    return gross_fee - tax, tax


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


def _expected_fee_cents(amount_cents: int, method: str, card: MethodRateCard) -> int:
    """What the rate card says this transaction's fee should be."""
    abs_amount = abs(amount_cents)
    if method == "upi":
        return round(abs_amount * card.upi_bps / 10_000)
    if method == "card":
        return round(abs_amount * card.card_bps / 10_000)
    if method == "netbanking":
        return card.netbanking_flat_cents
    if method == "wallet":
        return round(abs_amount * card.wallet_bps / 10_000)
    if method == "international_card":
        return round(abs_amount * card.international_card_bps / 10_000)
    return round(abs_amount * card.default_bps / 10_000)


def _expected_gst_on_fee(fee_cents: int, card: MethodRateCard) -> int:
    """GST is 18% on the fee, not on the principal."""
    return round(fee_cents * card.gst_rate_bps / 10_000)


def audit_transaction_fees(
    matched_txns: list,
    rate_card: MethodRateCard | None = None,
) -> list[FeeAuditFinding]:
    """Audit each matched transaction's fee against the method-aware rate card.

    Each transaction is expected to carry fee information in its extra dict:
      - fee_amount_cents: the actual fee charged
      - gst_amount_cents: the actual GST charged (optional)
      - payment_method: card / upi / netbanking / wallet (optional, inferred if absent)

    Returns a list of findings. An empty list means the audit found nothing wrong.
    """
    if rate_card is None:
        rate_card = MethodRateCard()

    findings: list[FeeAuditFinding] = []
    seen_fee_keys: dict[str, int] = {}  # for duplicate detection

    for txn in matched_txns:
        extra = getattr(txn, "extra", {}) or {}
        txn_id = getattr(txn, "source_txn_id", "?")
        amount_cents = getattr(txn, "amount_cents", 0)

        # Skip transactions with no fee data — nothing to audit
        actual_fee, actual_gst = fee_fields(extra)
        if actual_fee is None:
            continue
        amount_cents = _gross_cents(txn)

        method = _detect_payment_method(txn)
        expected_fee = _expected_fee_cents(amount_cents, method, rate_card)

        # --- Fee overcharge / undercharge ---
        diff = actual_fee - expected_fee
        tolerance = round(abs(amount_cents) * rate_card.tolerance_bps / 10_000)

        if abs(diff) > tolerance and diff > 0:
            findings.append(FeeAuditFinding(
                category=FeeAuditCategory.FEE_OVERCHARGE,
                severity=FeeAuditSeverity.WARNING if diff < expected_fee else FeeAuditSeverity.HIGH,
                txn_id=txn_id,
                expected_cents=expected_fee,
                actual_cents=actual_fee,
                difference_cents=diff,
                payment_method=method,
                description=(
                    f"Fee overcharge on {method} transaction {txn_id}: "
                    f"expected {expected_fee/100:.2f}, charged {actual_fee/100:.2f}, "
                    f"difference {diff/100:.2f}"
                ),
                rule_basis="contractual",
            ))
        elif abs(diff) > tolerance and diff < 0:
            findings.append(FeeAuditFinding(
                category=FeeAuditCategory.FEE_UNDERCHARGE,
                severity=FeeAuditSeverity.INFO,
                txn_id=txn_id,
                expected_cents=expected_fee,
                actual_cents=actual_fee,
                difference_cents=diff,
                payment_method=method,
                description=(
                    f"Fee undercharge on {method} transaction {txn_id}: "
                    f"expected {expected_fee/100:.2f}, charged {actual_fee/100:.2f}"
                ),
                rule_basis="contractual",
            ))

        # --- GST check ---
        if actual_gst is not None:
            expected_gst = _expected_gst_on_fee(actual_fee, rate_card)
            gst_diff = abs(actual_gst - expected_gst)
            # GST tolerance: 1 cent (rounding) per transaction
            if gst_diff > 1:
                # Check if GST was calculated on principal instead of fee
                gst_on_principal = _expected_gst_on_fee(abs(amount_cents), rate_card)
                if abs(actual_gst - gst_on_principal) <= 1:
                    findings.append(FeeAuditFinding(
                        category=FeeAuditCategory.GST_MISCALCULATION,
                        severity=FeeAuditSeverity.HIGH,
                        txn_id=txn_id,
                        expected_cents=expected_gst,
                        actual_cents=actual_gst,
                        difference_cents=actual_gst - expected_gst,
                        payment_method=method,
                        description=(
                            f"GST appears to be calculated on the principal amount "
                            f"({abs(amount_cents)/100:.2f}) instead of on the fee "
                            f"({actual_fee/100:.2f}). Expected GST: {expected_gst/100:.2f}, "
                            f"actual: {actual_gst/100:.2f}"
                        ),
                        rule_basis="statutory",
                    ))
                else:
                    findings.append(FeeAuditFinding(
                        category=FeeAuditCategory.GST_MISCALCULATION,
                        severity=FeeAuditSeverity.WARNING,
                        txn_id=txn_id,
                        expected_cents=expected_gst,
                        actual_cents=actual_gst,
                        difference_cents=actual_gst - expected_gst,
                        payment_method=method,
                        description=(
                            f"GST mismatch on {txn_id}: expected {expected_gst/100:.2f} "
                            f"(18% on fee of {actual_fee/100:.2f}), "
                            f"actual {actual_gst/100:.2f}"
                        ),
                        rule_basis="statutory",
                    ))

        # --- Duplicate fee detection ---
        fee_key = f"{txn_id}:{actual_fee}"
        if fee_key in seen_fee_keys:
            findings.append(FeeAuditFinding(
                category=FeeAuditCategory.DUPLICATE_FEE,
                severity=FeeAuditSeverity.HIGH,
                txn_id=txn_id,
                expected_cents=0,
                actual_cents=actual_fee,
                difference_cents=actual_fee,
                payment_method=method,
                description=(
                    f"Duplicate fee deduction detected for {txn_id}: "
                    f"same fee of {actual_fee/100:.2f} seen {seen_fee_keys[fee_key] + 1} times"
                ),
                rule_basis="contractual",
            ))
        seen_fee_keys[fee_key] = seen_fee_keys.get(fee_key, 0) + 1

    return findings


def audit_settlement_shortfall(
    matched_txns: list,
    batch_deduction_cents: int | None,
    rate_card: MethodRateCard | None = None,
) -> FeeAuditFinding | None:
    """Check if the sum of item-level fees equals the batch-level deduction.

    A shortfall means the gateway deducted more (or less) from the settlement
    than the sum of individual transaction fees — which is either an error or
    an undisclosed charge.
    """
    if batch_deduction_cents is None:
        return None
    if rate_card is None:
        rate_card = MethodRateCard()

    total_item_fees = 0
    has_fees = False
    for txn in matched_txns:
        fee, gst = fee_fields(getattr(txn, "extra", {}) or {})
        if fee is not None:
            total_item_fees += fee
            has_fees = True
            if gst is not None:
                total_item_fees += gst

    if not has_fees:
        return None

    diff = batch_deduction_cents - total_item_fees
    if abs(diff) <= 1:  # rounding tolerance
        return None

    return FeeAuditFinding(
        category=FeeAuditCategory.SETTLEMENT_SHORTFALL,
        severity=FeeAuditSeverity.HIGH if abs(diff) > 100 else FeeAuditSeverity.WARNING,
        txn_id="batch",
        expected_cents=total_item_fees,
        actual_cents=batch_deduction_cents,
        difference_cents=diff,
        description=(
            f"Settlement deduction ({batch_deduction_cents/100:.2f}) does not match "
            f"sum of item fees ({total_item_fees/100:.2f}). "
            f"Difference: {diff/100:.2f}. "
            + ("Gateway deducted more than item fees justify — possible undisclosed charge."
               if diff > 0 else
               "Gateway deducted less than item fees — possible billing error in merchant's favour.")
        ),
        rule_basis="contractual",
    )


def _rate_on(override: int | None, schedule, on: date) -> tuple[int, str]:
    """(rate in bps, citation) for a day, from an override or the schedule."""
    provision = india_tax.in_force(schedule, on)
    citation = provision.citation if provision else ""
    if override is not None:
        return override, citation
    return (provision.rate_bps if provision else 0), citation


def _cite(parts: dict[str, int]) -> str:
    """One citation, or each with the amount it covers when a batch spans two."""
    named = {c: v for c, v in parts.items() if c}
    if len(named) <= 1:
        return next(iter(named), "")
    return "; ".join(f"{c} on {v / 100:,.2f}" for c, v in named.items())


def audit_tds_194o(
    matched_txns: list,
    annual_gross_cents: int = 0,
    rate_card: MethodRateCard | None = None,
    as_of: date | None = None,
) -> list[FeeAuditFinding]:
    """Check e-commerce TDS: Section 194-O, and 393(1) Sl. 8(v) from April 2026.

    An e-commerce operator deducts TDS on the gross amount of sales it
    facilitates, at the earlier of crediting the seller or paying them. For a
    gateway that is the payment date, so each payment is taxed under the
    provision and rate in force on its own date in India — a batch that
    straddles 1 April 2026 cites both Acts, each on its own share.

    For individual and HUF sellers the first Rs 5 lakh of a year's gross is
    exempt, and TDS is expected on what lies above it. Payments are taken in
    date order, so the exempt amount is used up by the earliest ones. Other
    sellers have no threshold (set it to 0).

    Configurable, and deliberately so: the rate has moved three times since
    2020, and a payment gateway is not automatically the e-commerce operator
    for this provision. Setting the rate to 0 turns the check off. None of
    this is tax advice — it is arithmetic against numbers the merchant
    configures, on the dates the law set them.

    The name keeps the section the check was written for; it covers both.
    """
    if rate_card is None:
        rate_card = MethodRateCard()
    if rate_card.tds_rate_bps == 0:
        return []  # TDS checking disabled

    threshold = max(rate_card.tds_annual_threshold_cents, 0)
    running = annual_gross_cents
    expected_tds = 0
    taxable_by_citation: dict[str, int] = {}

    dated = sorted(
        matched_txns,
        key=lambda t: india_tax.ist_date(getattr(t, "timestamp_utc", None), as_of),
    )
    for txn in dated:
        gross = abs(_gross_cents(txn))
        exempt_left = max(threshold - running, 0)
        taxable = max(gross - exempt_left, 0)
        running += gross
        if taxable == 0:
            continue
        on = india_tax.ist_date(getattr(txn, "timestamp_utc", None), as_of)
        rate, citation = _rate_on(rate_card.tds_rate_bps, india_tax.TDS_ECOMMERCE, on)
        expected_tds += round(taxable * rate / 10_000)
        taxable_by_citation[citation] = taxable_by_citation.get(citation, 0) + taxable

    if not taxable_by_citation:
        return []

    actual_tds = sum(
        _stated_paise(getattr(t, "extra", {}) or {}, "tds_amount_cents", "tds", "tds_amount") or 0
        for t in matched_txns
    )

    diff = expected_tds - actual_tds
    if abs(diff) <= 100:  # Rs 1 tolerance
        return []

    citation = _cite(taxable_by_citation)
    return [FeeAuditFinding(
        category=FeeAuditCategory.TDS_WITHHOLDING_ERROR,
        severity=FeeAuditSeverity.HIGH,
        txn_id="batch",
        expected_cents=expected_tds,
        actual_cents=actual_tds,
        difference_cents=diff,
        description=(
            f"E-commerce TDS ({citation}): year-to-date gross {running/100:,.2f} "
            f"against a threshold of {threshold/100:,.2f}. Expected TDS "
            f"{expected_tds/100:,.2f}, withheld {actual_tds/100:,.2f}, "
            f"difference {diff/100:,.2f}."
        ),
        rule_basis="statutory",
        citation=citation,
    )]


def audit_tcs_section52(
    matched_txns: list,
    rate_card: MethodRateCard | None = None,
    as_of: date | None = None,
) -> list[FeeAuditFinding]:
    """GST TCS under Section 52 CGST, when the data says TCS was collected.

    TCS is collected on the net value of taxable supplies — supplies less
    returns — so refunds reduce the base rather than being ignored. The rate
    is the one in force on each supply's date: 1% until 9 July 2024, 0.5%
    from 10 July 2024.

    It runs only when at least one row reports TCS. Whether a gateway is the
    operator collecting it is the merchant's arrangement; an engine that
    expected TCS on every batch would raise a finding against every merchant
    whose gateway, correctly, collects none.
    """
    if rate_card is None:
        rate_card = MethodRateCard()
    if rate_card.tcs_rate_bps == 0:
        return []

    stated = [
        _stated_paise(getattr(t, "extra", {}) or {}, "tcs_amount_cents", "tcs", "tcs_amount")
        for t in matched_txns
    ]
    if all(v is None for v in stated):
        return []
    actual = sum(v or 0 for v in stated)

    weighted = 0
    base_by_citation: dict[str, int] = {}
    for txn in matched_txns:
        on = india_tax.ist_date(getattr(txn, "timestamp_utc", None), as_of)
        rate, citation = _rate_on(rate_card.tcs_rate_bps, india_tax.TCS_GST_ECOMMERCE, on)
        value = _gross_cents(txn)          # refunds arrive negative and net off
        weighted += value * rate
        base_by_citation[citation] = base_by_citation.get(citation, 0) + value
    expected = max(round(weighted / 10_000), 0)

    diff = expected - actual
    if abs(diff) <= 100:
        return []
    citation = _cite(base_by_citation)
    net_value = sum(base_by_citation.values())
    return [FeeAuditFinding(
        category=FeeAuditCategory.TCS_COLLECTION_ERROR,
        severity=FeeAuditSeverity.WARNING,
        txn_id="batch",
        expected_cents=expected,
        actual_cents=actual,
        difference_cents=diff,
        description=(
            f"GST TCS ({citation}): net taxable value {net_value/100:,.2f} after "
            f"returns. Expected TCS {expected/100:,.2f}, reported {actual/100:,.2f}, "
            f"difference {diff/100:,.2f}."
        ),
        rule_basis="statutory",
        citation=citation,
    )]


def run_fee_audit(
    matched_txns: list,
    batch_deduction_cents: int | None = None,
    annual_gross_cents: int = 0,
    rate_card: MethodRateCard | None = None,
    as_of: date | None = None,
) -> tuple[list[FeeAuditFinding], dict]:
    """Run the complete fee audit suite on a matched set.

    Returns (findings, summary). The summary carries totals a controller
    needs at a glance: total overcharges, total GST issues, whether TDS
    obligations are met.
    """
    if rate_card is None:
        rate_card = MethodRateCard()

    findings: list[FeeAuditFinding] = []

    # Per-transaction fee and GST audit
    findings.extend(audit_transaction_fees(matched_txns, rate_card))

    # Settlement-level shortfall
    shortfall = audit_settlement_shortfall(matched_txns, batch_deduction_cents, rate_card)
    if shortfall:
        findings.append(shortfall)

    # E-commerce TDS (194-O / 393(1)) and GST TCS (Section 52)
    findings.extend(audit_tds_194o(matched_txns, annual_gross_cents, rate_card, as_of))
    findings.extend(audit_tcs_section52(matched_txns, rate_card, as_of))

    # Sort: HIGH first, then WARNING, then INFO
    severity_order = {FeeAuditSeverity.HIGH: 0, FeeAuditSeverity.WARNING: 1, FeeAuditSeverity.INFO: 2}
    findings.sort(key=lambda f: (severity_order.get(f.severity, 9), -abs(f.difference_cents)))

    # Summary
    total_overcharge = sum(
        f.difference_cents for f in findings
        if f.category == FeeAuditCategory.FEE_OVERCHARGE
    )
    total_gst_issues = sum(
        1 for f in findings if f.category == FeeAuditCategory.GST_MISCALCULATION
    )
    has_tds_issue = any(f.category == FeeAuditCategory.TDS_WITHHOLDING_ERROR for f in findings)
    has_tcs_issue = any(f.category == FeeAuditCategory.TCS_COLLECTION_ERROR for f in findings)
    has_shortfall = any(f.category == FeeAuditCategory.SETTLEMENT_SHORTFALL for f in findings)

    summary = {
        "total_findings": len(findings),
        "high_severity": sum(1 for f in findings if f.severity == FeeAuditSeverity.HIGH),
        "total_overcharge_cents": total_overcharge,
        "gst_issues": total_gst_issues,
        "tds_compliance": "issue" if has_tds_issue else "ok",
        "tcs_compliance": "issue" if has_tcs_issue else "ok",
        "settlement_integrity": "shortfall" if has_shortfall else "ok",
    }

    if findings:
        logger.info(
            "Fee audit: %d finding(s), %d high severity, overcharge total %d cents",
            len(findings), summary["high_severity"], total_overcharge
        )

    return findings, summary
