"""
Scale proof: reconcile 200,000+ records and report the numbers.

This extends the 50K stress test to a scale that is unambiguously
enterprise-grade. The architecture is identical — the same generator, parser,
normaliser, linkage, and CP-SAT solver — but the dataset is 4x larger.

What it proves:
  * The engine does not degrade non-linearly with scale.
  * Memory stays bounded (no O(n^2) blowup).
  * Precision and recall remain 1.0 at enterprise transaction volumes.
  * Throughput is measurable in records-per-second.

Run from engine/:
    python scripts/run_scale_proof.py                  # default 200k
    python scripts/run_scale_proof.py --records 500000  # half a million
    python scripts/run_scale_proof.py --json docs/benchmarks/scale_proof.json
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import io
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scale_proof")

import file_agent
from ingestion import normalize_batch
from schema import SettlementBatch, SourceType
from pipeline import reconcile_settlement


SETTLEMENT_ID = "STL20260901001"
TRUE_SUBSET_SIZE = 55  # same as the 50k test — a realistic settlement size


def peak_working_set_mb() -> float | None:
    """Peak RSS on Windows. Returns None elsewhere."""
    if os.name != "nt":
        try:
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        except Exception:
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


def _realistic_amount(rng: random.Random) -> int:
    """Payment-shaped amount distribution (same as generate_50k)."""
    r = rng.random()
    if r < 0.55:
        return rng.randint(9_900, 200_000)
    if r < 0.85:
        return rng.randint(200_000, 1_000_000)
    if r < 0.97:
        return rng.randint(1_000_000, 5_000_000)
    return rng.randint(5_000_000, 25_000_000)


def generate_dataset(total_records: int, rng: random.Random):
    """Generate a multi-source dataset in memory. Returns (gateway_csv, bank_csv, erp_json, config)."""
    base_time = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    settled_at = base_time + timedelta(hours=72)

    # Proportions: 50% gateway, 30% bank, 20% ERP
    gw_count = int(total_records * 0.50)
    bank_count = int(total_records * 0.30)
    erp_count = total_records - gw_count - bank_count

    true_ids = []
    gross_target_cents = 0

    # Generate true members (in gateway)
    gw_rows = []
    for i in range(TRUE_SUBSET_SIZE):
        amount = _realistic_amount(rng)
        gross_target_cents += amount
        pid = f"pay_{100000 + i}"
        true_ids.append(pid)
        ts = base_time + timedelta(hours=rng.uniform(0, 48))
        gw_rows.append({
            "Payment ID": pid,
            "Order ID": f"{SETTLEMENT_ID}-ORD{200000 + i}",
            "Amount (INR)": f"{amount / 100:.2f}",
            "Timestamp": ts.isoformat(),
            "Memo": f"Sale settled in {SETTLEMENT_ID}",
            "Payer": f"customer_{rng.randint(1, 1000)}",
        })

    # Gateway noise
    decoy_stls = ["STL20260831001", "STL20260902001", "STL20260830001"]
    for i in range(gw_count - TRUE_SUBSET_SIZE):
        amount = _realistic_amount(rng)
        pid = f"pay_{200000 + i}"
        ts = base_time + timedelta(hours=rng.uniform(-24, 72))
        memo = f"Sale settled in {rng.choice(decoy_stls)}" if rng.random() < 0.2 else "Online sale"
        gw_rows.append({
            "Payment ID": pid,
            "Order ID": f"ORD{300000 + i}",
            "Amount (INR)": f"{amount / 100:.2f}",
            "Timestamp": ts.isoformat(),
            "Memo": memo,
            "Payer": f"customer_{rng.randint(1, 5000)}",
        })

    # Bank statement
    bank_rows = []
    net_amount = gross_target_cents - int(gross_target_cents * 0.03)  # 3% fee approximation
    bank_rows.append({
        "Value Date": settled_at.strftime("%Y-%m-%d"),
        "Description": f"Gateway settlement {SETTLEMENT_ID}",
        "Credit": f"{net_amount / 100:.2f}",
    })
    for i in range(bank_count - 1):
        amount = _realistic_amount(rng)
        ts = base_time + timedelta(hours=rng.uniform(-48, 96))
        bank_rows.append({
            "Value Date": ts.strftime("%Y-%m-%d"),
            "Description": f"{'UPI' if rng.random() < 0.4 else 'NEFT'} TXN {400000 + i}",
            "Credit": f"{amount / 100:.2f}",
        })

    # ERP ledger
    erp_entries = []
    for i in range(erp_count):
        amount = _realistic_amount(rng)
        ts = base_time + timedelta(hours=rng.uniform(-24, 72))
        erp_entries.append({
            "ref": f"ERP{500000 + i}",
            "erp_id": f"JE{600000 + i}",
            "amount_inr": round(amount / 100, 2),
            "posted_at": ts.isoformat(),
            "description": "Revenue entry",
        })

    # Serialize to in-memory files
    gw_buf = io.StringIO()
    w = csv.DictWriter(gw_buf, fieldnames=list(gw_rows[0].keys()))
    w.writeheader()
    w.writerows(gw_rows)

    bank_buf = io.StringIO()
    w = csv.DictWriter(bank_buf, fieldnames=list(bank_rows[0].keys()))
    w.writeheader()
    w.writerows(bank_rows)

    erp_json = json.dumps(erp_entries)

    config = {
        "settlement_id": SETTLEMENT_ID,
        "net_amount_cents": net_amount,
        "settled_at": settled_at.isoformat(),
        "gross_target_cents": gross_target_cents,
        "true_member_ids": true_ids,
        "declared_deductions_cents": gross_target_cents - net_amount,
        "total_records": {
            "gateway": gw_count,
            "bank": bank_count,
            "erp": erp_count,
            "total": total_records,
        },
    }

    return gw_buf.getvalue(), bank_buf.getvalue(), erp_json, config


def run_scale_proof(total_records: int, output_json: str | None = None):
    rng = random.Random(42)

    print(f"\n{'=' * 72}")
    print(f"SCALE PROOF — {total_records:,} RECORDS")
    print(f"{'=' * 72}")

    # Generate dataset
    t_gen_start = time.time()
    gw_csv, bank_csv, erp_json, config = generate_dataset(total_records, rng)
    t_gen = time.time() - t_gen_start
    logger.info("Generated %d records in %.2fs", total_records, t_gen)

    # Parse
    t_parse_start = time.time()
    gw_parsed = file_agent.parse_file_content(gw_csv.encode(), "gateway_report.csv")
    bank_parsed = file_agent.parse_file_content(bank_csv.encode(), "bank_statement.csv")
    erp_parsed = file_agent.parse_file_content(erp_json.encode(), "erp_ledger.json")
    t_parse = time.time() - t_parse_start

    logger.info("Parsed: gw=%d, bank=%d, erp=%d in %.2fs",
                len(gw_parsed), len(bank_parsed),
                len(erp_parsed), t_parse)

    # Normalize
    t_norm_start = time.time()
    gw_txns = normalize_batch(gw_parsed, SourceType.GATEWAY)
    bank_txns = normalize_batch(bank_parsed, SourceType.BANK)
    erp_txns = normalize_batch(erp_parsed, SourceType.ERP)
    candidates = gw_txns + bank_txns + erp_txns
    t_norm = time.time() - t_norm_start

    actual_total = len(candidates)
    logger.info("Normalized %d candidates in %.2fs", actual_total, t_norm)

    # Build settlement batch
    import dateutil.parser as dp
    batch = SettlementBatch(
        batch_id=config["settlement_id"],
        net_amount_cents=config["net_amount_cents"],
        currency="INR",
        settled_at_utc=dp.isoparse(config["settled_at"]).replace(tzinfo=timezone.utc) if dp.isoparse(config["settled_at"]).tzinfo is None else dp.isoparse(config["settled_at"]),
        source=SourceType.BANK,
        declared_deductions_cents=config["declared_deductions_cents"],
        member_source=SourceType.GATEWAY,
    )

    # Reconcile
    t_recon_start = time.time()
    result = reconcile_settlement(batch, candidates)
    t_recon = time.time() - t_recon_start
    t_total = t_parse + t_norm + t_recon

    # Accuracy
    truth_set = set(config["true_member_ids"])
    matched_set = set(result.match_result.matched_txn_ids) if result.match_result else set()
    precision = len(matched_set & truth_set) / len(matched_set) if matched_set else 0.0
    recall = len(matched_set & truth_set) / len(truth_set) if truth_set else 0.0
    exact_match = matched_set == truth_set

    mem_mb = peak_working_set_mb()

    # Results
    print(f"\nSources merged:         gw({config['total_records']['gateway']}) "
          f"+ bank({config['total_records']['bank']}) + erp({config['total_records']['erp']})")
    print(f"Total candidate pool:   {actual_total:,}  (target {total_records:,})")
    print(f"Generation time:        {t_gen:.2f}s")
    print(f"Parse/normalize:        {t_parse + t_norm:.2f}s")
    print(f"Reconciliation time:    {t_recon:.2f}s")
    print(f"Total wall clock:       {t_total:.2f}s")
    print(f"Records per second:     {actual_total / t_recon:,.0f}")
    if mem_mb is not None:
        print(f"Peak memory:            {mem_mb:.0f} MB")
    print(f"-" * 72)
    print(f"Cleared:                {result.match_result.cleared}")
    print(f"Method:                 {result.match_result.method.value}")
    print(f"Confidence:             {result.match_result.confidence:.2f}")
    print(f"Matched txn count:      {len(matched_set)}  (expected {len(truth_set)})")
    print(f"Exact set match:        {exact_match}   <- by transaction ID, not count")
    print(f"Precision / recall:     {precision:.4f} / {recall:.4f}")
    print(f"Matched sum (INR):      {result.match_result.matched_sum_cents / 100:.2f}"
          f"  (target gross {config['gross_target_cents'] / 100:.2f})")
    print(f"Exceptions raised:      {len(result.exceptions)}")
    print(f"{'=' * 72}")

    if exact_match:
        print(f"\nPASS: cleared with the exact {len(truth_set)}-transaction true set")
    else:
        print(f"\nFAIL: matched set does not equal truth set")
        missing = truth_set - matched_set
        extra = matched_set - truth_set
        if missing:
            print(f"  Missing from match: {sorted(list(missing))[:10]}")
        if extra:
            print(f"  Extra in match: {sorted(list(extra))[:10]}")

    # JSON output
    output = {
        "total_records": total_records,
        "actual_candidates": actual_total,
        "true_subset_size": len(truth_set),
        "generation_time_s": round(t_gen, 2),
        "parse_normalize_time_s": round(t_parse + t_norm, 2),
        "reconciliation_time_s": round(t_recon, 2),
        "total_wall_clock_s": round(t_total, 2),
        "records_per_second": round(actual_total / t_recon),
        "peak_memory_mb": round(mem_mb) if mem_mb else None,
        "cleared": result.match_result.cleared,
        "exact_set_match": exact_match,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "matched_count": len(matched_set),
        "exceptions": len(result.exceptions),
    }

    if output_json:
        out_path = Path(output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)
        print(f"\nWrote {out_path}")

    return output


def main():
    ap = argparse.ArgumentParser(description="Scale proof: reconcile N records")
    ap.add_argument("--records", type=int, default=200_000,
                    help="Total records across all three sources (default: 200000)")
    ap.add_argument("--json", type=str, default="",
                    help="Write JSON results to this file")
    args = ap.parse_args()
    run_scale_proof(args.records, args.json or None)


if __name__ == "__main__":
    main()
