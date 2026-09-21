"""
Indian Fee and Tax Audit — post-reconciliation fee verification.

WHY THIS EXISTS
---------------
Indian payment gateways charge per-method fees: UPI near 0%, cards around 2%,
netbanking often a flat fee, wallets somewhere in between. On top of the fee,
GST at 18% (CGST 9% + SGST 9%) applies to the FEE, not the principal. And
Section 194-O of the Income Tax Act requires an e-commerce operator to
deduct TDS on the gross amount it credits to a seller — 0.1% since 1 October
2024, reduced from 1% by the Finance (No. 2) Act 2024 — above an annual
threshold. Whether it applies to a given merchant at all is a question about
that merchant's arrangement, not about this engine, so the rate, the
threshold and the check itself are all configurable and the defaults are
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
  * TDS withholding errors — Section 194-O thresholds are configurable because
    the rules vary by entity type
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
from dataclasses import dataclass, field
from enum import Enum

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

    gst_rate_bps: int = 1800         # 18% GST on gateway fee (CGST 9% + SGST 9%)

    # Section 194-O TDS on gross credited above an annual threshold.
    #
    # 10 bps, not 100. The rate was 1% until the Finance (No. 2) Act 2024 cut
    # it to 0.1% with effect from 1 October 2024; a default still reading 1%
    # would over-expect TDS by a factor of ten on every settlement and raise a
    # withholding finding on merchants who were charged correctly.
    #
    # Rates and thresholds move, and whether 194-O applies at all depends on
    # the merchant's arrangement rather than on anything this engine can see.
    # Both are configurable; set either to 0 to switch the check off.
    tds_rate_bps: int = 10           # 0.1% (since 2024-10-01)
    tds_annual_threshold_cents: int = 500_000_00  # Rs 5,00,000

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
        actual_fee = extra.get("fee_amount_cents")
        if actual_fee is None:
            continue
        actual_fee = int(actual_fee)

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
        actual_gst = extra.get("gst_amount_cents")
        if actual_gst is not None:
            actual_gst = int(actual_gst)
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
        extra = getattr(txn, "extra", {}) or {}
        fee = extra.get("fee_amount_cents")
        if fee is not None:
            total_item_fees += int(fee)
            has_fees = True
            gst = extra.get("gst_amount_cents")
            if gst is not None:
                total_item_fees += int(gst)

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


def audit_tds_194o(
    matched_txns: list,
    annual_gross_cents: int = 0,
    rate_card: MethodRateCard | None = None,
) -> list[FeeAuditFinding]:
    """Check TDS under Section 194-O for e-commerce operators.

    Section 194-O requires an e-commerce operator to deduct TDS on the gross
    amount of sales it facilitates, at the time it credits the seller. The
    rate is 0.1% from 1 October 2024 (1% before that), over a Rs 5 lakh
    annual threshold for individual and HUF sellers.

    Configurable, and deliberately so:
    - the rate has changed twice in recent years (1%, 0.75% as COVID relief,
      now 0.1%), so a hardcoded figure is a future wrong answer;
    - the threshold depends on who the seller is;
    - a payment gateway is not automatically the e-commerce operator for
      194-O purposes. Whether this check should run at all is the merchant's
      call, which is why setting the rate to 0 turns it off.

    None of this is tax advice — it is arithmetic against numbers the
    merchant configures.
    """
    if rate_card is None:
        rate_card = MethodRateCard()

    if rate_card.tds_rate_bps == 0 or rate_card.tds_annual_threshold_cents == 0:
        return []  # TDS checking disabled

    findings: list[FeeAuditFinding] = []

    batch_gross = sum(abs(getattr(t, "amount_cents", 0)) for t in matched_txns)
    total_gross = annual_gross_cents + batch_gross

    if total_gross > rate_card.tds_annual_threshold_cents:
        taxable_amount = total_gross - rate_card.tds_annual_threshold_cents
        expected_tds = round(taxable_amount * rate_card.tds_rate_bps / 10_000)

        # Check if TDS was actually withheld in the batch
        actual_tds = 0
        for txn in matched_txns:
            extra = getattr(txn, "extra", {}) or {}
            tds = extra.get("tds_amount_cents")
            if tds is not None:
                actual_tds += int(tds)

        diff = expected_tds - actual_tds
        if abs(diff) > 100:  # Rs 1 tolerance
            findings.append(FeeAuditFinding(
                category=FeeAuditCategory.TDS_WITHHOLDING_ERROR,
                severity=FeeAuditSeverity.HIGH,
                txn_id="batch",
                expected_cents=expected_tds,
                actual_cents=actual_tds,
                difference_cents=diff,
                description=(
                    f"Section 194-O TDS check: annual gross {total_gross/100:.2f} exceeds "
                    f"threshold {rate_card.tds_annual_threshold_cents/100:.2f}. "
                    f"Expected TDS: {expected_tds/100:.2f}, actual withheld: {actual_tds/100:.2f}. "
                    f"Difference: {diff/100:.2f}"
                ),
                rule_basis="statutory",
            ))

    return findings


def run_fee_audit(
    matched_txns: list,
    batch_deduction_cents: int | None = None,
    annual_gross_cents: int = 0,
    rate_card: MethodRateCard | None = None,
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

    # TDS 194-O
    findings.extend(audit_tds_194o(matched_txns, annual_gross_cents, rate_card))

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
    has_shortfall = any(f.category == FeeAuditCategory.SETTLEMENT_SHORTFALL for f in findings)

    summary = {
        "total_findings": len(findings),
        "high_severity": sum(1 for f in findings if f.severity == FeeAuditSeverity.HIGH),
        "total_overcharge_cents": total_overcharge,
        "gst_issues": total_gst_issues,
        "tds_compliance": "issue" if has_tds_issue else "ok",
        "settlement_integrity": "shortfall" if has_shortfall else "ok",
    }

    if findings:
        logger.info(
            "Fee audit: %d finding(s), %d high severity, overcharge total %d cents",
            len(findings), summary["high_severity"], total_overcharge
        )

    return findings, summary
