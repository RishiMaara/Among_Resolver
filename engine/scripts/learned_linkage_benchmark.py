#!/usr/bin/env python3
"""
What the learned linkage buys, measured on data this project did not write.

THE QUESTION
------------
ReconRiver with settlement ids stripped from every reference is the case the
hand-set linkage weights have nothing to say about: 21.6% of settlements
identified, because once the id is gone the pool is a sea of payments that
all look alike. What is left is timing — a processor pays out on a cycle —
and whether the engine can learn that cycle and use it.

THE ARMS
--------
  before         LINKAGE_EM=0. The engine as it was.
  no history     Learned linkage on, nothing learned yet. A stripped pool
                 has no anchors, so the Fellegi-Sunter model has nothing to
                 identify members with and must decline to fit. This arm
                 exists to show the model does not invent evidence: it
                 should score exactly like `before`.
  with history   The settlement cycle learned from the anchored clears of
                 the OTHER three scenarios (leave-one-scenario-out), then
                 applied to this scenario's stripped settlements. A merchant
                 whose earlier payouts carried references, reconciling ones
                 that do not. Nothing from the scenario being measured is
                 ever learned from.

Every arm reports false clears. A learned tier that bought accuracy with a
single wrong approval would be a regression, whatever its hit rate.

WHAT THIS IS NOT
----------------
Proof that the cycle is T+1 everywhere. ReconRiver's processor settles T+1;
the engine learned that from the data, and a processor on T+2 would teach
it T+2. The lag feature was added after looking at this dataset, so the
held-out checks that matter are the other corpora (benchmark.py and the edge
suites), which this script also runs for false clears.

Run from engine/ (a few minutes):
    python scripts/learned_linkage_benchmark.py
    python scripts/learned_linkage_benchmark.py --json docs/benchmarks/learned_linkage.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

os.environ["SETTLEMENT_CYCLE_STORE"] = "memory"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

import linkage  # noqa: E402
import orchestrator  # noqa: E402
import settlement_cycle  # noqa: E402
from pipeline import reconcile_settlement  # noqa: E402
from run_reconriver import (EVAL_CONFIG, NET_TO_NET, SCENARIOS, _read, _safe_amount,  # noqa: E402
                            _safe_ts, _sample, build_pool, truth_for)
from schema import SettlementBatch, SourceType  # noqa: E402

_last_link = {}
_real_build = orchestrator.build_candidate_links


def _spy(*a, **k):
    res = _real_build(*a, **k)
    _last_link["r"] = res
    return res


orchestrator.build_candidate_links = _spy


def batches_for(scenario: str, cap: int):
    for b in _sample(_read(scenario, "bank_settlements.csv"), cap):
        truth = truth_for(scenario, b["settlement_batch_id"])
        ts = _safe_ts(b["booked_at"], scenario)
        amt = _safe_amount(b["credited_amount"], scenario)
        if not truth or ts is None or amt is None:
            continue
        yield b, truth, SettlementBatch(
            batch_id=b["settlement_batch_id"], net_amount_cents=amt, currency=b["currency"],
            settled_at_utc=ts, source=SourceType.BANK, member_source=SourceType.GATEWAY)


def solve(pool, b, batch):
    sub = [t for t in pool if t.source_txn_id != b["bank_entry_id"]]
    t0 = time.perf_counter()
    report = reconcile_settlement(batch, sub, settlement_window_days=10,
                                  rate_card=NET_TO_NET, subset_config=EVAL_CONFIG)
    return report, sub, time.perf_counter() - t0


def band_of(report, sub) -> str:
    link = _last_link.get("r")
    ids = set(report.match_result.matched_txn_ids)
    if not link or not ids:
        return "none"
    keys = {linkage.txn_key(t) for t in sub if t.source_txn_id in ids}
    scores = {linkage.txn_key(c.txn): c.score for c in link.scored}
    vals = [scores[k] for k in keys if k in scores]
    mean = sum(vals) / len(vals) if vals else 0.0
    return linkage.confidence_band(link.anchor_keys, keys, mean, link.learned_keys)


def row(scenario, b, truth, report, band, elapsed):
    matched = set(report.match_result.matched_txn_ids)
    exact = bool(matched) and matched == truth
    return {"scenario": scenario, "batch": b["settlement_batch_id"], "truth": len(truth),
            "matched": len(matched), "exact": exact, "cleared": report.match_result.cleared,
            "false_clear": report.match_result.cleared and not exact,
            "confidence": report.match_result.confidence, "band": band,
            "latency_s": round(elapsed, 3)}


def summarise(rows):
    n = len(rows) or 1
    by_band = {}
    for r in rows:
        k = by_band.setdefault(r["band"], {"n": 0, "exact": 0})
        k["n"] += 1
        k["exact"] += int(r["exact"])
    return {
        "batches": len(rows),
        "exact_set_identified": sum(r["exact"] for r in rows),
        "exact_set_identified_pct": round(100 * sum(r["exact"] for r in rows) / n, 2),
        "auto_cleared_correct": sum(r["cleared"] and r["exact"] for r in rows),
        "false_clears": sum(r["false_clear"] for r in rows),
        "mean_latency_s": round(sum(r["latency_s"] for r in rows) / n, 3),
        "by_band": by_band,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    ap.add_argument("--cap", type=int, default=10, help="settlements per scenario")
    args = ap.parse_args()
    logging.disable(logging.WARNING)

    scenarios = [s for s in SCENARIOS if _read(s, "bank_settlements.csv")]
    stripped_pools = {s: build_pool(s, strip_anchor=True) for s in scenarios}
    anchored_pools = {s: build_pool(s, strip_anchor=False) for s in scenarios}

    # History: every anchored clear, per scenario, as (batch, member times).
    print("Learning phase: anchored settlements, learned linkage on ...", flush=True)
    os.environ["LINKAGE_EM"] = "1"
    history, anchored_rows = {}, []
    for s in scenarios:
        history[s] = []
        for b, truth, batch in batches_for(s, args.cap):
            settlement_cycle.reset()
            report, sub, el = solve(anchored_pools[s], b, batch)
            anchored_rows.append(row(s, b, truth, report, band_of(report, sub), el))
            if report.match_result.cleared:
                ids = set(report.match_result.matched_txn_ids)
                times = [t.timestamp_utc for t in sub
                         if t.source == SourceType.GATEWAY and t.source_txn_id in ids]
                history[s].append((batch, times))

    def run_stripped(arm: str, em: str, learn_from_others: bool):
        os.environ["LINKAGE_EM"] = em
        out = []
        for s in scenarios:
            settlement_cycle.reset()
            if learn_from_others:
                for other in scenarios:
                    if other == s:
                        continue
                    for batch, times in history[other]:
                        settlement_cycle.record_clear(batch.batch_id, "gateway", batch.currency,
                                                      batch.settled_at_utc, times)
            for b, truth, batch in batches_for(s, args.cap):
                report, sub, el = solve(stripped_pools[s], b, batch)
                out.append(row(s, b, truth, report, band_of(report, sub), el))
        print(f"  {arm}: {summarise(out)}", flush=True)
        return out

    print("Stripped condition ...", flush=True)
    before = run_stripped("before", "0", False)
    no_history = run_stripped("no history", "1", False)
    with_history = run_stripped("with history", "1", True)

    os.environ["LINKAGE_EM"] = "0"
    anchored_before = []
    for s in scenarios:
        for b, truth, batch in batches_for(s, args.cap):
            report, sub, el = solve(anchored_pools[s], b, batch)
            anchored_before.append(row(s, b, truth, report, band_of(report, sub), el))

    result = {
        "dataset": "ReconRiver (third-party synthetic), settlements sampled evenly per scenario",
        "settlements_per_scenario": args.cap,
        "stripped": {"before": summarise(before), "no_history": summarise(no_history),
                     "with_history": summarise(with_history)},
        "anchored": {"before": summarise(anchored_before), "after": summarise(anchored_rows)},
        "learned_cycle_band": {
            "n": sum(1 for r in with_history if r["band"] == "learned_cycle"),
            "exact": sum(1 for r in with_history if r["band"] == "learned_cycle" and r["exact"]),
        },
        "rows": {"stripped_with_history": with_history},
    }
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=1))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
