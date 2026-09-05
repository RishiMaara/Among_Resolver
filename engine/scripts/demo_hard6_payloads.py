import sys
import os
import json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datetime import datetime, timedelta, timezone
from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence
from orchestrator import reconcile_batch
from subset_sum import SubsetSumConfig

BASE_TIME = datetime(2026, 8, 20, tzinfo=timezone.utc)

def make_txn(source: SourceType, txn_id: str, amount_cents: int, extra_keys=None) -> NormalizedTxn:
    extra = extra_keys or {}
    return NormalizedTxn(
        source=source,
        source_txn_id=txn_id,
        ref_id_canonical=f"REF{txn_id}",
        amount_cents=amount_cents,
        currency="INR",
        timestamp_utc=BASE_TIME,
        tz_confidence=TzConfidence.HIGH,
        memo_raw=f"Payment {txn_id}",
        memo_normalized=f"payment {txn_id}",
        extra=extra
    )

def test_nm_reconciliation():
    print("========================================")
    print("FEATURE 1: N:M RECONCILIATION")
    print("========================================")
    
    # 2 Bank credits covering 3 Gateway transactions
    batch = SettlementBatch(batch_id="BATCH_NM_01", net_amount_cents=10000, currency="INR", settled_at_utc=BASE_TIME)
    
    # We supply declared deductions to exactly match gross target of 10000
    batch.declared_deductions_cents = 0 

    candidates = [
        make_txn(SourceType.GATEWAY, "GW1", 4000),
        make_txn(SourceType.GATEWAY, "GW2", 3500),
        make_txn(SourceType.GATEWAY, "GW3", 2500)
    ]
    
    input_data = {
        "batch_id": batch.batch_id,
        "target_gross_cents": 10000,
        "pool": [c.source_txn_id for c in candidates]
    }
    print("INPUT DATA:")
    print(json.dumps(input_data, indent=2))
    
    report = reconcile_batch(batch, candidates, subset_config=SubsetSumConfig(tolerance_cents=0, ambiguity_probe_limit=1), settlement_window_days=5)
    print("\nRESULT CLEARED:", report.match_result.cleared)
    print("MATCHED IDs:", report.match_result.matched_txn_ids)
    print("REASONING:", report.match_result.reasoning)
    print()

def test_greedy_fallback():
    print("========================================")
    print("FEATURE 2: ANCHORLESS MATH FALLBACK (GREEDY)")
    print("========================================")
    
    batch = SettlementBatch(batch_id="BATCH_GREEDY_01", net_amount_cents=29100, currency="INR", settled_at_utc=BASE_TIME)
    batch.declared_deductions_cents = 900 # Gross target = 30000
    
    candidates = []
    # Provide a single massive noise candidate that exceeds target
    candidates.append(make_txn(SourceType.GATEWAY, "NOISE_EXCEEDS", 999999))
    
    # Provide candidates that sum to 29999 (off by 1)
    candidates.append(make_txn(SourceType.GATEWAY, "NEAR1", 10000))
    candidates.append(make_txn(SourceType.GATEWAY, "NEAR2", 10000))
    candidates.append(make_txn(SourceType.GATEWAY, "NEAR3", 9999))
    
    print(f"INPUT DATA: Target=30000, Pool Size={len(candidates)}, No Exact Match Exists")
    
    cfg = SubsetSumConfig(tolerance_cents=0, solver_time_limit_s=0.1)
    os.environ["AMONGRESOLVER_NO_LINKAGE"] = "1"
    report = reconcile_batch(batch, candidates, subset_config=cfg, settlement_window_days=5)
    del os.environ["AMONGRESOLVER_NO_LINKAGE"]
    
    print("\nRESULT CLEARED:", report.match_result.cleared)
    print("FALLBACK TRIGGERED:", "greedy approximation" in str(report.match_result.reasoning).lower())
    print("MATCHED IDs (Approximation):", report.match_result.matched_txn_ids)
    print("REASONING:", report.match_result.reasoning)
    print()

def run_all():
    test_nm_reconciliation()
    test_greedy_fallback()
    print("Test data generation complete.")

if __name__ == "__main__":
    run_all()
