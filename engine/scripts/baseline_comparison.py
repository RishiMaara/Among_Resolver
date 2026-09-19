"""
AI vs Rules-Only baseline comparison.

Runs the benchmark suite twice — once with the full AI-augmented pipeline
(linkage, fuzzy tiebreak, LLM header mapping), once with linkage disabled
(AMONGRESOLVER_NO_LINKAGE=1) — and produces a side-by-side comparison.

The rules-only baseline is the engine as it was before the entity-resolution
reframe: subset-sum against the whole pool, no identity signal. It answers
the question "what does the AI actually buy you?" with measured numbers
instead of a claim.

Run:
    cd engine && python scripts/baseline_comparison.py
    cd engine && python scripts/baseline_comparison.py --scenarios 180 --json docs/benchmarks/baseline_comparison.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ENGINE_DIR = SCRIPT_DIR.parent
ROOT_DIR = ENGINE_DIR.parent


def _run_benchmark(scenarios: int, seed: int, no_linkage: bool, declare_source: bool) -> dict:
    """Run benchmark.py in a subprocess and capture its JSON output."""
    tmp = ENGINE_DIR / "data" / f"_baseline_{'nolinkage' if no_linkage else 'full'}.json"
    cmd = [
        sys.executable, str(SCRIPT_DIR / "benchmark.py"),
        "--scenarios", str(scenarios),
        "--seed", str(seed),
        "--json", str(tmp),
    ]
    if declare_source:
        cmd.append("--declare-source")

    env = os.environ.copy()
    if no_linkage:
        env["AMONGRESOLVER_NO_LINKAGE"] = "1"
    else:
        env.pop("AMONGRESOLVER_NO_LINKAGE", None)

    print(f"  Running {'rules-only baseline' if no_linkage else 'full AI-augmented'}…")
    t0 = time.time()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=str(ENGINE_DIR))
    elapsed = time.time() - t0

    if proc.returncode != 0:
        print(f"  FAILED (exit {proc.returncode})")
        print(proc.stderr[-500:] if proc.stderr else "(no stderr)")
        sys.exit(1)

    with open(tmp, encoding="utf-8") as f:
        data = json.load(f)
    tmp.unlink(missing_ok=True)

    data["wall_clock_s"] = round(elapsed, 1)
    return data


def _extract_metrics(data: dict) -> dict:
    """Pull the metrics we care about from the benchmark JSON."""
    h = data["headline"]
    p = data["partial_credit"]
    t = data["throughput"]
    return {
        "false_clears": h["false_clear_count"],
        "auto_clear_correct_pct": h["auto_cleared_correct_pct"],
        "truth_identified_pct": h["truth_identified_pct"],
        "correct_abstention_pct": h["correct_abstention_pct"],
        "mean_precision": p["mean_precision"],
        "mean_recall": p["mean_recall"],
        "median_latency_s": t["median_latency_s"],
        "p95_latency_s": t["p95_latency_s"],
        "total_txns": t["total_txns_processed"],
        "wall_clock_s": data["wall_clock_s"],
    }


def main():
    ap = argparse.ArgumentParser(description="Compare AI-augmented vs rules-only baseline")
    ap.add_argument("--scenarios", type=int, default=120)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--declare-source", action="store_true")
    ap.add_argument("--json", type=str, default="")
    args = ap.parse_args()

    print(f"Baseline comparison: {args.scenarios} scenarios, seed {args.seed}\n")

    # Run both configurations
    baseline_raw = _run_benchmark(args.scenarios, args.seed, no_linkage=True,
                                   declare_source=args.declare_source)
    augmented_raw = _run_benchmark(args.scenarios, args.seed, no_linkage=False,
                                    declare_source=args.declare_source)

    baseline = _extract_metrics(baseline_raw)
    augmented = _extract_metrics(augmented_raw)

    # Build comparison
    comparison = {
        "scenarios": args.scenarios,
        "seed": args.seed,
        "declare_source": args.declare_source,
        "baseline_rules_only": baseline,
        "ai_augmented": augmented,
        "improvement": {},
    }

    # Compute deltas
    for key in baseline:
        b, a = baseline[key], augmented[key]
        if isinstance(b, (int, float)) and isinstance(a, (int, float)):
            comparison["improvement"][key] = {
                "baseline": b,
                "augmented": a,
                "delta": round(a - b, 4) if isinstance(a, float) else a - b,
            }

    # Print results
    print("\n" + "=" * 76)
    print("AI vs RULES-ONLY BASELINE COMPARISON")
    print("=" * 76)
    print(f"{'Metric':<30} {'Rules-Only':>15} {'AI-Augmented':>15} {'Delta':>12}")
    print("-" * 76)

    labels = {
        "false_clears": "False Clears",
        "auto_clear_correct_pct": "Auto-clear correct (%)",
        "truth_identified_pct": "Truth identified (%)",
        "correct_abstention_pct": "Correct abstentions (%)",
        "mean_precision": "Mean precision",
        "mean_recall": "Mean recall",
        "median_latency_s": "Median latency (s)",
        "total_txns": "Transactions processed",
        "wall_clock_s": "Wall clock (s)",
    }

    for key, label in labels.items():
        b = baseline[key]
        a = augmented[key]
        if isinstance(b, float):
            delta = a - b
            sign = "+" if delta > 0 else ""
            print(f"{label:<30} {b:>15.4f} {a:>15.4f} {sign + str(round(delta, 4)):>12}")
        else:
            delta = a - b
            sign = "+" if delta > 0 else ""
            print(f"{label:<30} {b:>15} {a:>15} {sign + str(delta):>12}")

    print("=" * 76)
    print()

    # Key takeaway
    fc_b = baseline["false_clears"]
    fc_a = augmented["false_clears"]
    ac_b = baseline["auto_clear_correct_pct"]
    ac_a = augmented["auto_clear_correct_pct"]
    ti_b = baseline["truth_identified_pct"]
    ti_a = augmented["truth_identified_pct"]

    print("Key findings:")
    print(f"  False clears:      {fc_b} (baseline) vs {fc_a} (augmented) — "
          f"{'BOTH ZERO' if fc_b == 0 and fc_a == 0 else 'REGRESSION' if fc_a > fc_b else 'IMPROVED'}")
    print(f"  Auto-clear rate:   {ac_b}% -> {ac_a}%  ({'+' if ac_a > ac_b else ''}{ac_a - ac_b}pp)")
    print(f"  Truth identified:  {ti_b}% -> {ti_a}%  ({'+' if ti_a > ti_b else ''}{ti_a - ti_b}pp)")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(comparison, f, indent=2)
        print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
