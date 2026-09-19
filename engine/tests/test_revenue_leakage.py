import pytest
from datetime import datetime, timezone
from revenue_leakage import calculate_30_day_leakage, VolumetricLeakageReport
from fee_audit import MethodRateCard
from schema import NormalizedTxn, SourceType, ComplianceStatus

def test_calculate_30_day_leakage_detects_breach():
    # Simulate a gateway that systematically rounds up fees by 10 cents per transaction
    # Over a month (e.g. 600 transactions), that's a Rs 60 leakage.
    
    start_date = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rate_card = MethodRateCard(card_bps=200) # 2%
    
    txns = []
    for i in range(600):
        # 1000 cents = Rs 10. 2% fee = 20 cents.
        # But gateway charges 30 cents.
        txns.append(
            NormalizedTxn(
                source=SourceType.GATEWAY,
                source_txn_id=f"txn_{i}",
                ref_id_canonical=f"ref_{i}",
                amount_cents=1000,
                currency="INR",
                timestamp_utc=start_date,
                tz_confidence=None,
                compliance_status=ComplianceStatus.PASS,
                extra={"payment_method": "card", "fee_amount_cents": 30}
            )
        )
        
    report = calculate_30_day_leakage(start_date, txns, rate_card)
    
    assert report.total_volume_cents == 600 * 1000 # 600,000 cents (Rs 6,000)
    assert report.expected_fees_cents == 12000 # 2% of 600k = 12000 cents
    assert report.actual_fees_cents == 600 * 30 # 18000 cents
    assert report.net_leakage_cents == 6000 # 18000 - 12000 = Rs 60
    
    assert len(report.findings) == 1
    assert report.findings[0].category.value == "fee_overcharge"
    assert report.findings[0].difference_cents == 6000
