"""
End-to-end 50K-scale stress test for the reconciliation pipeline.

Loads all three real source files under data/50k_stress/ (bank_statement.csv,
gateway_report.csv, erp_ledger.json), runs them through the *same* path a real
upload hits -- file_agent.parse_file_content -> ingestion.normalize_batch --
merges them into one candidate pool, then runs the full compliance +
linkage +
reconciliation pipeline (pipeline.reconcile_settlement) against the
settlement batch described in batch_config.json.

This is the scenario that originally hung/OOM'd before the O(n^2) time and
memory fixes: ~15K bank + ~25K gateway + ~10K ERP rows merged into a single
~50K-row candidate pool, reconciling down to a 55-transaction settlement.

Accuracy is checked by IDENTITY against the member ids recorded in
batch_config.json. The earlier version compared only counts, against a
dataset whose target was so small that just 5 of 50,100 records could
participate — it proved throughput and nothing about matching.

Run from engine/:
    python scripts/run_50k_stress_test.py
"""

import ctypes
import json
import logging
import os
import sys
import time
from datetime import timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("stress_test")

import dateutil.parser as dp

import file_agent
from ingestion import normalize_batch, normalize_amount_to_cents
from schema import SettlementBatch, SourceType
from pipeline import reconcile_settlement

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "50k_stress")


def peak_working_set_mb() -> float | None:
    """Peak RSS via the Windows psapi call -- no extra dependency (psutil
    isn't in requirements.txt). Returns None on non-Windows platforms."""
    if os.name != "nt":
        return None

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    return (counters.PeakWorkingSetSize / (1024 * 1024)) if ok else None


def load_source(filename: str, source_type: SourceType) -> list:
    path = os.path.join(DATA_DIR, filename)
    with open(path, "rb") as f:
        content = f.read()

    parsed_rows = file_agent.parse_file_content(content, filename)
    logger.info(f"{filename}: Agent 0 parsed {len(parsed_rows)} rows")

    txns = normalize_batch(parsed_rows, source_type)
    dropped = len(parsed_rows) - len(txns)
    logger.info(
        f"{filename}: Agent 1 normalized {len(txns)} transactions"
        + (f" ({dropped} dropped as unparseable)" if dropped else "")
    )
    return txns


def main():
    with open(os.path.join(DATA_DIR, "batch_config.json")) as f:
        config = json.load(f)

    batch = SettlementBatch(
        batch_id=config["batch_id"],
        net_amount_cents=normalize_amount_to_cents(config["target_net_amount_inr"]),
        currency="INR",
        settled_at_utc=dp.parse(config["settled_at_utc"]).astimezone(timezone.utc),
        # The feed the members live in. A production reconciliation knows
        # this; without it the ERP journal mirrors are substitutable with the
        # gateway payments they mirror and the engine correctly declines.
        member_source=(
            SourceType(config["member_source"]) if config.get("member_source") else None
        ),
    )

    t_load0 = time.perf_counter()
    candidates = []
    candidates += load_source("bank_statement.csv", SourceType.BANK)
    candidates += load_source("gateway_report.csv", SourceType.GATEWAY)
    candidates += load_source("erp_ledger.json", SourceType.ERP)
    t_load1 = time.perf_counter()

    logger.info(
        f"Merged candidate pool: {len(candidates)} transactions "
        f"(expected {config['total_rows']} from all 3 sources)"
    )
    logger.info(f"Load + parse + normalize time: {t_load1 - t_load0:.2f}s")

    t0 = time.perf_counter()
    report = reconcile_settlement(batch, candidates, settlement_window_days=5)
    elapsed = time.perf_counter() - t0

    peak_mb = peak_working_set_mb()

    matched_ids = set(report.match_result.matched_txn_ids)
    expected_true_count = config["true_subset_count"]

    # Verify by IDENTITY, not by count-and-sum.
    #
    # The previous version of this script asserted only
    # len(matched) == true_subset_count, against a config that recorded
    # nothing but that count. With a contested target that check is
    # worthless — any set of the right size summing to the target passes it,
    # including a completely wrong one. The generator now records the actual
    # member ids for exactly this reason.
    truth_ids = set(config.get("true_subset_ids") or [])
    correct = bool(truth_ids) and matched_ids == truth_ids
    precision = len(matched_ids & truth_ids) / len(matched_ids) if matched_ids else 0.0
    recall = len(matched_ids & truth_ids) / len(truth_ids) if truth_ids else 0.0

    print("\n" + "=" * 64)
    print("50K STRESS TEST RESULTS")
    print("=" * 64)
    print(f"Sources merged:         bank({len([c for c in candidates if c.source == SourceType.BANK])}) "
          f"+ gateway({len([c for c in candidates if c.source == SourceType.GATEWAY])}) "
          f"+ erp({len([c for c in candidates if c.source == SourceType.ERP])})")
    print(f"Total candidate pool:   {len(candidates)}  (expected {config['total_rows']})")
    print(f"Load/parse/normalize:   {t_load1 - t_load0:.2f}s")
    print(f"Reconciliation time:    {elapsed:.2f}s")
    print(f"Total wall clock:       {(t_load1 - t_load0) + elapsed:.2f}s")
    print("-" * 64)
    print(f"Cleared:                {report.match_result.cleared}")
    print(f"Method:                 {report.match_result.method.value}")
    print(f"Confidence:             {report.match_result.confidence:.2f}")
    print(f"Matched txn count:      {len(matched_ids)}  (expected {expected_true_count})")
    print(f"Exact set match:        {correct}   <- by transaction ID, not count")
    print(f"Precision / recall:     {precision:.4f} / {recall:.4f}")
    if not correct and truth_ids:
        print(f"  matched, not true:    {sorted(matched_ids - truth_ids)[:5]}")
        print(f"  true, not matched:    {sorted(truth_ids - matched_ids)[:5]}")
    print(f"Matched sum (INR):      {report.match_result.matched_sum_cents / 100:.2f}  "
          f"(target gross {config['target_gross_amount_inr']})")
    print(f"Exceptions raised:      {len(report.exceptions)}")
    if peak_mb is not None:
        print(f"Peak working set:       {peak_mb:.1f} MB")
    print("=" * 64)

    pass_fail = report.match_result.cleared and correct
    if pass_fail:
        verdict = f"cleared with the exact {expected_true_count}-transaction true set"
    elif report.match_result.cleared:
        verdict = "CLEARED THE WRONG SET — a false clear, the worst possible outcome"
    else:
        verdict = "did not clear (no false clear, but the settlement is unresolved)"
    print(f"\n{'PASS' if pass_fail else 'FAIL'}: {verdict}")

    sys.exit(0 if pass_fail else 1)


if __name__ == "__main__":
    main()
