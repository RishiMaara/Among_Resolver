"""
Confidence calibration.

WHY THIS MATTERS MORE THAN ANOTHER ACCURACY POINT
--------------------------------------------------
The engine reports a confidence with every match — 0.45 to 0.97 — and a human
uses that number to decide whether to look. Nothing has ever checked whether
it means anything.

A confidence that is not calibrated is worse than no confidence at all. If the
engine says 0.95 and is right 60% of the time, it is actively steering
reviewers away from the batches that most need them, and it does so with an
air of precision. The reconciliation literature names match confidence
calibration as one of three
operational reconciliation metrics for exactly this reason; it is the metric
that distinguishes "accurate" from "reliable", and "reliable" is the claim.

WHAT IS MEASURED
----------------
For every scenario that produced a proposed set, one pair:

    (confidence the engine reported, was the set actually correct)

Correctness is by transaction IDENTITY against ground truth, not by count or
sum. Pairs are bucketed by confidence and each bucket's predicted confidence
is compared with its observed accuracy.

    ECE   Expected Calibration Error — the size of the average gap, weighted
          by how many predictions fall in each bucket. The headline.
    MCE   Maximum Calibration Error — the worst single bucket. ECE can look
          respectable while one bucket is badly wrong, and that bucket is
          usually the high-confidence one people actually trust.
    Brier Mean squared error of the probability itself. Rewards being both
          accurate AND appropriately uncertain.

DIRECTION OF ERROR IS NOT SYMMETRIC
-----------------------------------
Overconfidence (observed < predicted) is the dangerous direction: it tells a
reviewer to skip something that was wrong. Underconfidence wastes review time,
which is a cost, not a loss. They are reported separately rather than folded
into one number.

Run from engine/:
    python scripts/calibration.py
    python scripts/calibration.py --scenarios 240 --json out.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
sys.path.insert(0, os.path.dirname(__file__))

import benchmark as bench

# Buckets are deliberately uneven: the engine emits confidence in a handful of
# discrete bands (0.45 / 0.65 / 0.80 / 0.95 plus small adjustments), so evenly
# spaced deciles would leave most of them empty and concentrate everything in
# two. These follow where the values actually land.
BUCKETS = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 0.93), (0.93, 1.01)]


@dataclass
class Pair:
    confidence: float
    correct: bool
    source: str
    scenario: str


def collect_from_benchmark(n: int, seed: int) -> list[Pair]:
    rng = random.Random(seed)
    densities = list(bench.DENSITIES)
    pairs: list[Pair] = []

    for i in range(n):
        family, solvable, _ = bench.FAMILIES[i % len(bench.FAMILIES)]
        density = densities[(i // len(bench.FAMILIES)) % len(densities)]
        sc = bench.build_scenario(i, family, solvable, density, rng, declare_source=True)
        r = bench.run_scenario(sc)

        # Only scenarios where the engine actually proposed something carry a
        # confidence claim to evaluate. Declining to answer is not a prediction.
        if not r.matched_ids:
            continue
        pairs.append(Pair(
            confidence=round(r.confidence, 3),
            correct=r.set_is_truth,
            source="benchmark",
            scenario=f"{family}/{density}",
        ))
    return pairs


def bucket_stats(pairs: list[Pair]) -> list[dict]:
    out = []
    for lo, hi in BUCKETS:
        in_bucket = [p for p in pairs if lo <= p.confidence < hi]
        if not in_bucket:
            continue
        predicted = sum(p.confidence for p in in_bucket) / len(in_bucket)
        observed = sum(1 for p in in_bucket if p.correct) / len(in_bucket)
        out.append({
            "range": f"[{lo:.2f}, {hi:.2f})",
            "n": len(in_bucket),
            "predicted": round(predicted, 4),
            "observed": round(observed, 4),
            "gap": round(observed - predicted, 4),
        })
    return out


def summarise(pairs: list[Pair]) -> dict:
    if not pairs:
        return {"pairs": 0}

    stats = bucket_stats(pairs)
    total = len(pairs)

    ece = sum(b["n"] / total * abs(b["gap"]) for b in stats)
    mce = max((abs(b["gap"]) for b in stats), default=0.0)
    brier = sum((p.confidence - (1.0 if p.correct else 0.0)) ** 2 for p in pairs) / total

    overconfident = [b for b in stats if b["gap"] < -0.05]
    underconfident = [b for b in stats if b["gap"] > 0.05]

    return {
        "pairs": total,
        "overall_accuracy": round(sum(1 for p in pairs if p.correct) / total, 4),
        "mean_confidence": round(sum(p.confidence for p in pairs) / total, 4),
        "ece": round(ece, 4),
        "mce": round(mce, 4),
        "brier": round(brier, 4),
        "buckets": stats,
        "overconfident_buckets": [b["range"] for b in overconfident],
        "underconfident_buckets": [b["range"] for b in underconfident],
    }


def _commit() -> str:
    """The commit a figure was measured at, or "unknown" outside a checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", type=int, default=180)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", type=str, default="")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING)

    print(f"Collecting confidence/outcome pairs from {args.scenarios} scenarios…")
    pairs = collect_from_benchmark(args.scenarios, args.seed)
    s = summarise(pairs)

    print("\n" + "=" * 72)
    print("CONFIDENCE CALIBRATION")
    print("=" * 72)
    if not s["pairs"]:
        print("No scenarios produced a proposed set — nothing to calibrate.")
        return

    print(f"predictions            : {s['pairs']}")
    print(f"mean confidence said   : {s['mean_confidence']}")
    print(f"actual accuracy        : {s['overall_accuracy']}")
    print("-" * 72)
    print(f"ECE  (avg gap)         : {s['ece']}   <- headline; lower is better")
    print(f"MCE  (worst bucket)    : {s['mce']}")
    print(f"Brier score            : {s['brier']}")
    print("-" * 72)
    print(f"{'confidence':<16}{'n':>6}{'said':>10}{'actual':>10}{'gap':>10}  verdict")
    print("-" * 72)
    for b in s["buckets"]:
        if b["gap"] < -0.05:
            verdict = "OVERCONFIDENT — steers reviewers away from errors"
        elif b["gap"] > 0.05:
            verdict = "underconfident — wastes review time"
        else:
            verdict = "well calibrated"
        print(f"{b['range']:<16}{b['n']:>6}{b['predicted']:>10.3f}"
              f"{b['observed']:>10.3f}{b['gap']:>+10.3f}  {verdict}")
    print("=" * 72)

    if s["overconfident_buckets"]:
        print("\nOVERCONFIDENT in: " + ", ".join(s["overconfident_buckets"]))
        print("  This is the dangerous direction — the number tells a reviewer")
        print("  to skip a batch that was wrong.")
    else:
        print("\nNo bucket is overconfident: where the engine claims high")
        print("confidence, it is at least that accurate.")

    if args.json:
        # Provenance, not decoration. Three different ECE figures reached the
        # documentation - 0.067, 0.073, 0.0763 - and every one was a real
        # number from a real run. Nothing recorded which run, so nobody could
        # tell they were one measurement taken under different conditions
        # rather than three contradictory claims. A snapshot that cannot be
        # traced to its inputs is not evidence.
        s["run"] = {
            "measured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scenarios": args.scenarios,
            "seed": args.seed,
            "commit": _commit(),
            "solver_workers": 1,
            "note": (
                "benchmark.run_scenario pins CP-SAT to one worker. Under the "
                "default parallel search this measurement does not reproduce: "
                "scenarios with more than one valid subset flip between runs."
            ),
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
