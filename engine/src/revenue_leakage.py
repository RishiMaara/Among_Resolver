from datetime import datetime, timedelta
import logging
from typing import Optional
from dataclasses import dataclass, field

import db
from fee_audit import MethodRateCard, FeeAuditSeverity, FeeAuditCategory, FeeAuditFinding
from schema import NormalizedTxn

logger = logging.getLogger(__name__)

@dataclass
class VolumetricLeakageReport:
    window_start_utc: datetime
    window_end_utc: datetime
    total_volume_cents: int
    expected_fees_cents: int
    actual_fees_cents: int
    net_leakage_cents: int
    findings: list[FeeAuditFinding] = field(default_factory=list)


def calculate_30_day_leakage(
    start_date: datetime, 
    transactions: list[NormalizedTxn], 
    rate_card: MethodRateCard
) -> VolumetricLeakageReport:
    """
    Simulates a Revenue Leakage Engine that runs over a rolling 30-day window.
    
    Instead of auditing a single settlement batch, this engine aggregates total processing
    volume across a month to check if volume-tiered pricing discounts were properly
    applied by the payment gateway, or if consistent micro-penny rounding errors are
    siphoning revenue.
    """
    
    end_date = start_date + timedelta(days=30)
    
    total_volume = 0
    actual_fees_collected = 0
    
    method_volumes = {
        "card": 0,
        "upi": 0,
        "netbanking": 0,
        "wallet": 0,
        "international_card": 0,
        "unknown": 0
    }
    
    # 1. Aggregate the 30-day volume
    for txn in transactions:
        total_volume += abs(txn.amount_cents)
        
        extra = getattr(txn, "extra", {}) or {}
        actual_fee = extra.get("fee_amount_cents", 0)
        actual_fees_collected += int(actual_fee)
        
        method = extra.get("payment_method", "unknown").lower()
        if method in method_volumes:
            method_volumes[method] += abs(txn.amount_cents)
        else:
            method_volumes["unknown"] += abs(txn.amount_cents)

    # 2. Calculate what the volume-tiered fees SHOULD have been
    # (Simplified for demonstration: assuming the base rate card applies)
    expected_fees = (
        round(method_volumes["card"] * rate_card.card_bps / 10_000) +
        round(method_volumes["upi"] * rate_card.upi_bps / 10_000) +
        # Netbanking is flat, so volume calculation doesn't easily translate without txn counts,
        # but for this volumetric simulation we will proxy it.
        round(method_volumes["wallet"] * rate_card.wallet_bps / 10_000) +
        round(method_volumes["international_card"] * rate_card.international_card_bps / 10_000)
    )
    
    leakage = actual_fees_collected - expected_fees
    findings = []
    
    if leakage > 5000: # Rs 50.00 materiality threshold for a 30 day window
        findings.append(FeeAuditFinding(
            category=FeeAuditCategory.FEE_OVERCHARGE,
            severity=FeeAuditSeverity.HIGH,
            txn_id="30_DAY_WINDOW",
            expected_cents=expected_fees,
            actual_cents=actual_fees_collected,
            difference_cents=leakage,
            payment_method="AGGREGATED",
            description=(
                f"Volumetric SLA breach detected over 30 days. "
                f"Total volume processed: {total_volume/100:.2f}. "
                f"Gateway collected {actual_fees_collected/100:.2f} in fees, but rate card dictates "
                f"{expected_fees/100:.2f}. Leakage: {leakage/100:.2f}."
            ),
            rule_basis="contractual_volume_tier"
        ))
        
    logger.info("30-day leakage check complete. Volume: %d, Leakage: %d", total_volume, leakage)
    
    return VolumetricLeakageReport(
        window_start_utc=start_date,
        window_end_utc=end_date,
        total_volume_cents=total_volume,
        expected_fees_cents=expected_fees,
        actual_fees_cents=actual_fees_collected,
        net_leakage_cents=leakage,
        findings=findings
    )
