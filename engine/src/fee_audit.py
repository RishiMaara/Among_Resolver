"""
Indian fee and tax audit, run after reconciliation on the matched set.

Checks each transaction's fee against a method-aware card (UPI, card,
netbanking, wallet), GST at 18% on the FEE, e-commerce TDS (194-O until
31 March 2026, 393(1) Sl. 8(v) after, 0.1%), GST TCS under Section 52 CGST
(0.5% since 10 July 2024), batch-level fee totals and duplicate fees. Rates
come from dated schedules in india_tax.py, by each payment's date; every
check is configurable. It reports; it never changes the reconciliation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from enum import Enum

import india_tax

# The shared types live in fee_model.py and the tax checks in tax_audit.py;
# every name is still importable from here.
from fee_model import (  # noqa: E402,F401 - re-exported; callers import them here
    FeeAuditSeverity,
    FeeAuditCategory,
    FeeAuditFinding,
    MethodRateCard,
    _paise,
    _stated_paise,
    _gross_cents,
)
from tax_audit import (  # noqa: E402,F401 - re-exported; callers import them here
    _rate_on,
    _cite,
    audit_tds_194o,
    audit_tcs_section52,
)


logger = logging.getLogger(__name__)


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

        # Skip transactions with no fee data — nothing to audit. A refund is
        # money going back, not a sale, so no rate card applies to it: read
        # as a sale with a zero fee it was flagged as an undercharge.
        actual_fee, actual_gst = fee_fields(extra)
        if actual_fee is None or amount_cents <= 0 or str(extra.get("type", "")).lower() == "refund":
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
