"""
Evaluate the engine against ReconRiver — a third-party reconciliation dataset.

WHY THIS MATTERS MORE THAN OUR OWN BENCHMARK
--------------------------------------------
Every accuracy number this project has produced so far came from data we
generated ourselves. That is a real weakness: we chose the amount
distributions, we decided how references degrade, and we wrote both the
scenarios and the code that solves them. A benchmark you author can flatter
you in ways you will not notice.

ReconRiver (heybadrinath/reconriver-synthetic-reconciliation) is authored by
someone else, with its own schema, its own fee policy and its own ground
truth. It is still synthetic, so this is not production data — but it is not
OUR synthetic data, which is the point.

THE TASK
--------
Each bank settlement credits a net amount. The processor transactions tagged
with that settlement's batch id sum EXACTLY to it (verified: all 7 batches in
clean-settlement tie to the cent). So the job is: given a bank credit and a
pool of processor transactions, recover the exact set that composes it.

Fee handling: the dataset's processor rows already carry `net_amount`, and
the bank credits the sum of those. So the correct rate card here is ZERO —
matching is net-to-net. This is why reconcile_batch's rate card had to become
a parameter; it was hardcoded to 2%+1%, which would have inflated the target
off the answer entirely.

TWO CONDITIONS, MEASURED SEPARATELY
-----------------------------------
The processor rows carry `settlement_batch_id` explicitly, which is about the
strongest linkage anchor imaginable. Reporting only that number would flatter
the engine, so both are measured:

  anchored  settlement_batch_id present in the reference — the realistic case
            for a processor feed, which does tag its payouts.

  stripped  settlement_batch_id removed from the reference, leaving only
            merchant order ids. Nothing names the settlement, so linkage has
            to fall back on weaker signal or decline.

The gap between the two IS the finding: it measures how much of the engine's
accuracy is carried by reference quality rather than by the engine.

Run from engine/:
    python scripts/run_reconriver.py
    python scripts/run_reconriver.py --json out.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import dateutil.parser as dp

from schema import NormalizedTxn, SettlementBatch, SourceType
import file_agent
from ingestion import normalize_batch
from fee_decomposition import FeeRateCard
from ingestion import normalize_amount_to_cents
from subset_sum import SubsetSumConfig
from pipeline import reconcile_settlement

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "reconriver")
SCENARIOS = ["clean-settlement", "failure-recovery", "mixed-exceptions", "month-end-close"]

# The dataset's processor rows are already net of fees and the bank credits
# their sum, so there is nothing left to add back.
NET_TO_NET = FeeRateCard(gateway_fee_bps=0, flat_fee_cents=0, tax_withholding_bps=0)


@dataclass
class BatchResult:
    scenario: str
    batch_id: str
    condition: str
    pool_size: int
    truth_size: int
    matched: set = field(default_factory=set)
    truth: set = field(default_factory=set)
    cleared: bool = False
    ambiguous: bool = False
    confidence: float = 0.0
    elapsed_s: float = 0.0

    @property
    def exact(self) -> bool:
        return bool(self.matched) and self.matched == self.truth

    @property
    def false_clear(self) -> bool:
        return self.cleared and not self.exact

    @property
    def precision(self) -> float:
        return len(self.matched & self.truth) / len(self.matched) if self.matched else 0.0

    @property
    def recall(self) -> float:
        return len(self.matched & self.truth) / len(self.truth) if self.truth else 0.0


# Rows the dataset deliberately corrupts (e.g. "2026-99-99T99:99:99Z") and
# which real ingestion would reject rather than guess at. Counted, not hidden:
# silently dropping malformed input is how a reconciliation quietly loses
# records, and the count belongs in the report.
REJECTED: dict[str, int] = {}
REJECTED_FILES: list[str] = []
SAMPLING: dict[str, tuple[int, int]] = {}
DROPPED_ROWS: dict[str, int] = {}

# Tighter than the default 15s+3x8s so 56 reconciliations stay tractable.
# Reported alongside the results because a solver budget is part of the
# measurement, not a detail.
EVAL_CONFIG = SubsetSumConfig(tolerance_cents=5, solver_time_limit_s=6.0,
                              ambiguity_probe_limit=2, probe_time_limit_s=3.0)

# failure-recovery and month-end-close carry ~1,300 and ~1,500 settlements
# respectively — about 2,850 batches across the four scenarios, and 5,700
# reconciliations once both conditions run. That is hours of solver time to
# answer a question a sample answers just as well, so batches are sampled
# per scenario. The sample is evenly spaced rather than random so the run is
# reproducible and spans the whole file instead of clustering at the start.
DEFAULT_BATCHES_PER_SCENARIO = 10


def _sample(items: list, cap: int) -> list:
    if cap <= 0 or len(items) <= cap:
        return items
    step = len(items) / cap
    return [items[int(i * step)] for i in range(cap)]


def _safe_ts(raw: str, scenario: str):
    try:
        return dp.parse(raw)
    except Exception:
        REJECTED[scenario] = REJECTED.get(scenario, 0) + 1
        return None


def _safe_amount(raw: str, scenario: str):
    try:
        return normalize_amount_to_cents(raw)
    except Exception:
        REJECTED[scenario] = REJECTED.get(scenario, 0) + 1
        return None


def _read(scenario: str, name: str) -> list[dict]:
    path = os.path.join(DATA_DIR, scenario, name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _ingest(scenario: str, filename: str, source: SourceType) -> list[NormalizedTxn]:
    """
    Ingest through the REAL path: Agent 0 maps the headers, Agent 1 normalises.

    The first version of this harness read the CSVs with csv.DictReader and
    hand-mapped the columns, which quietly tested only the matching engine and
    skipped the file-understanding agent entirely — on a dataset whose whole
    point is that its schema is unfamiliar. Agent 0 is the component most
    exercised by third-party data, so bypassing it wasted the test.
    """
    path = os.path.join(DATA_DIR, scenario, filename)
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        content = f.read()

    try:
        rows = file_agent.parse_file_content(content, filename)
    except file_agent.FileRejected as e:
        REJECTED_FILES.append(f"{scenario}/{filename}: {e.problems[0]}")
        return []

    txns = normalize_batch(rows, source)
    dropped = len(rows) - len(txns)
    if dropped:
        DROPPED_ROWS[f"{scenario}/{filename}"] = dropped
    return txns


def _strip_settlement_ref(txns: list[NormalizedTxn], batch_ids: set[str]) -> None:
    """
    Remove the settlement identifier from every reference, in place.

    ref_id_canonical has already had separators stripped, so the batch id has
    to be canonicalised the same way before it can be removed.
    """
    tokens = {"".join(ch for ch in b if ch.isalnum()).upper() for b in batch_ids}
    for t in txns:
        ref = t.ref_id_canonical
        for tok in tokens:
            ref = ref.replace(tok, "")
        t.ref_id_canonical = ref


def build_pool(scenario: str, strip_anchor: bool) -> list[NormalizedTxn]:
    """
    One candidate pool per scenario: every processor transaction (so batches
    compete with each other), the internal ledger as a second feed, and the
    bank settlements themselves.

    The internal ledger carries GROSS amounts while the processor rows carry
    NET, so the two feeds are genuinely different views of the same payments
    rather than exact duplicates — which is what real multi-source data looks
    like.
    """
    pool: list[NormalizedTxn] = []
    pool += _ingest(scenario, "processor_transactions.csv", SourceType.GATEWAY)
    pool += _ingest(scenario, "internal_transactions.csv", SourceType.ERP)
    pool += _ingest(scenario, "bank_settlements.csv", SourceType.BANK)

    if strip_anchor:
        batch_ids = {
            b["settlement_batch_id"]
            for b in _read(scenario, "bank_settlements.csv")
            if b.get("settlement_batch_id")
        }
        _strip_settlement_ref(pool, batch_ids)

    return pool


def truth_for(scenario: str, batch_id: str) -> set[str]:
    """Processor transactions the dataset says compose this batch."""
    return {
        p["processor_transaction_id"]
        for p in _read(scenario, "processor_transactions.csv")
        if p.get("settlement_batch_id") == batch_id
    }


def run_condition(condition: str, strip_anchor: bool,
                  cap: int = DEFAULT_BATCHES_PER_SCENARIO) -> list[BatchResult]:
    results: list[BatchResult] = []

    for scenario in SCENARIOS:
        settlements = _read(scenario, "bank_settlements.csv")
        if not settlements:
            continue
        total_settlements = len(settlements)
        settlements = _sample(settlements, cap)
        SAMPLING[scenario] = (len(settlements), total_settlements)
        pool = build_pool(scenario, strip_anchor)

        for b in settlements:
            batch_id = b["settlement_batch_id"]
            truth = truth_for(scenario, batch_id)
            if not truth:
                continue

            b_ts = _safe_ts(b["booked_at"], scenario)
            b_amt = _safe_amount(b["credited_amount"], scenario)
            if b_ts is None or b_amt is None:
                continue
            batch = SettlementBatch(
                batch_id=batch_id,
                net_amount_cents=b_amt,
                currency=b["currency"],
                settled_at_utc=b_ts,
                source=SourceType.BANK,
                member_source=SourceType.GATEWAY,
            )

            t0 = time.perf_counter()
            report = reconcile_settlement(
                batch,
                [t for t in pool if t.source_txn_id != b["bank_entry_id"]],
                settlement_window_days=10,
                rate_card=NET_TO_NET,
                subset_config=EVAL_CONFIG,
            )
            elapsed = time.perf_counter() - t0

            print(f"  [{condition}] {scenario}/{batch_id} "
                  f"pool={len(pool)} truth={len(truth)} {elapsed:.1f}s", flush=True)
            results.append(BatchResult(
                scenario=scenario,
                batch_id=batch_id,
                condition=condition,
                pool_size=len(pool),
                truth_size=len(truth),
                matched=set(report.match_result.matched_txn_ids),
                truth=truth,
                cleared=report.match_result.cleared,
                ambiguous=report.match_result.ambiguous,
                confidence=report.match_result.confidence,
                elapsed_s=elapsed,
            ))
    return results


def summarise(results: list[BatchResult]) -> dict:
    n = len(results)
    if not n:
        return {}
    exact = sum(1 for r in results if r.exact)
    fc = sum(1 for r in results if r.false_clear)
    auto = sum(1 for r in results if r.cleared and r.exact)
    return {
        "batches": n,
        "auto_cleared_correct": auto,
        "auto_cleared_correct_pct": round(100 * auto / n, 2),
        "exact_set_identified": exact,
        "exact_set_identified_pct": round(100 * exact / n, 2),
        "false_clears": fc,
        "false_clear_pct": round(100 * fc / n, 2),
        "mean_precision": round(sum(r.precision for r in results) / n, 4),
        "mean_recall": round(sum(r.recall for r in results) / n, 4),
        "mean_latency_s": round(sum(r.elapsed_s for r in results) / n, 3),
        "max_latency_s": round(max(r.elapsed_s for r in results), 3),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=str, default="")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--batches-per-scenario", type=int,
                    default=DEFAULT_BATCHES_PER_SCENARIO,
                    help="0 = every settlement (hours).")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    conditions = [("anchored", False), ("stripped", True)]
    all_results: dict[str, list[BatchResult]] = {}

    for name, strip in conditions:
        print(f"Running condition: {name} "
              f"({'settlement_batch_id removed' if strip else 'settlement_batch_id present'})…")
        all_results[name] = run_condition(name, strip, args.batches_per_scenario)

    print("\n" + "=" * 76)
    print("RECONRIVER — third-party reconciliation dataset")
    print("=" * 76)

    for name, _ in conditions:
        s = summarise(all_results[name])
        print(f"\n{name.upper()}")
        print(f"  batches                : {s['batches']}")
        print(f"  auto-cleared & correct : {s['auto_cleared_correct']}/{s['batches']}  ({s['auto_cleared_correct_pct']}%)")
        print(f"  exact set identified   : {s['exact_set_identified']}/{s['batches']}  ({s['exact_set_identified_pct']}%)")
        print(f"  FALSE CLEARS           : {s['false_clears']}  ({s['false_clear_pct']}%)")
        print(f"  mean precision/recall  : {s['mean_precision']} / {s['mean_recall']}")
        print(f"  latency mean/max       : {s['mean_latency_s']}s / {s['max_latency_s']}s")

    print("\n" + "-" * 76)
    print(f"{'scenario':<22}{'batch':<36}{'anch':>6}{'strip':>7}")
    print("-" * 76)
    by_key = {}
    for name, _ in conditions:
        for r in all_results[name]:
            by_key.setdefault((r.scenario, r.batch_id), {})[name] = r
    for (sc, bid), d in sorted(by_key.items()):
        a = d.get("anchored")
        st = d.get("stripped")
        mark = lambda r: ("ok" if r.exact else ("FC" if r.false_clear else "--")) if r else "?"
        print(f"{sc:<22}{bid:<36}{mark(a):>6}{mark(st):>7}")
    if REJECTED_FILES:
        print("\nFiles REJECTED by Agent 0:")
        for r in REJECTED_FILES:
            print(f"  {r}")
    if DROPPED_ROWS:
        print("\nRows dropped during normalisation:")
        for k, v in sorted(DROPPED_ROWS.items()):
            print(f"  {k:<48}{v}")

    if REJECTED:
        print("\nRows rejected as unparseable (malformed timestamps/amounts "
              "injected by the dataset):")
        for sc, n in sorted(REJECTED.items()):
            print(f"  {sc:<24}{n}")

    print("=" * 76)
    print("ok = exact set identified   -- = declined / wrong set, not cleared   FC = FALSE CLEAR")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(
                {name: summarise(all_results[name]) for name, _ in conditions},
                f, indent=2,
            )
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
