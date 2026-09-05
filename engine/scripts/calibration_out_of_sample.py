#!/usr/bin/env python3
"""
Is the confidence calibrated on data the engine was NOT tuned against?

WHY THIS EXISTS
---------------
`calibration.py` reports ECE 0.0863 over 180 scenarios from `benchmark.py`.
Those scenarios are ours: we chose the amount distributions, decided how
references degrade, and picked the 0.85 auto-clear gate by reading exactly
those buckets. That makes the figure real and in-sample, and the two are not
the same claim. LINKAGE.md listed it as an open gap and a reviewer listed it
again: calibration fitted to scenarios you wrote says little about whether the
gate generalises.

This measures the same thing on a corpus the gate was never tuned against:

  ReconRiver   third-party synthetic, its own schema, its own fee policy,
               both the anchored and reference-stripped conditions

One corpus is validation, not proof, and this one carries usable references.
What the gate does where references are absent is not measured here.

WHAT WOULD FALSIFY THE GATE
---------------------------
One thing, specifically: a prediction at or above 0.85 that turned out wrong.
In-sample there were 103 such predictions and 103 were correct. If that holds
out-of-sample the gate is doing its job; if a single high-confidence
prediction is wrong, the number to change is the gate, not this script.

Run from engine/:
    python scripts/calibration_out_of_sample.py
    python scripts/calibration_out_of_sample.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
sys.path.insert(0, os.path.dirname(__file__))

import calibration as cal            # noqa: E402  (Pair, summarise, bucket_stats)
import run_reconriver as rr          # noqa: E402


def pairs_from_reconriver(cap: int) -> list[cal.Pair]:
    """
    Every ReconRiver batch that proposed a set, as one confidence/outcome pair.

    Both conditions are included and labelled. Declining to answer is not a
    prediction and carries no confidence claim, so a batch that matched nothing
    is skipped rather than counted as a wrong answer — the same rule
    collect_from_benchmark applies.
    """
    out: list[cal.Pair] = []
    for condition, strip in (("anchored", False), ("stripped", True)):
        for r in rr.run_condition(condition, strip, cap=cap):
            if not r.matched:
                continue
            out.append(cal.Pair(
                confidence=round(r.confidence, 3),
                correct=r.exact,
                source=f"reconriver/{condition}",
                scenario=f"{r.scenario}/{condition}",
            ))
    return out


def report(name: str, pairs: list[cal.Pair]) -> dict:
    s = cal.summarise(pairs)
    print("\n" + "=" * 72)
    print(f"OUT-OF-SAMPLE CALIBRATION — {name}")
    print("=" * 72)
    if not s.get("pairs"):
        print("No predictions to score.")
        return s

    print(f"predictions            : {s['pairs']}")
    print(f"mean confidence said   : {s['mean_confidence']}")
    print(f"actual accuracy        : {s['overall_accuracy']}")
    print("-" * 72)
    print(f"ECE  (avg gap)         : {s['ece']}")
    print(f"MCE  (worst bucket)    : {s['mce']}")
    print(f"Brier score            : {s['brier']}")
    print("-" * 72)
    print(f"{'confidence':<16}{'n':>6}{'said':>10}{'actual':>10}{'gap':>10}")
    print("-" * 72)
    for b in s["buckets"]:
        print(f"{b['range']:<16}{b['n']:>6}{b['predicted']:>10}"
              f"{b['observed']:>10}{b['gap']:>+10}")

    # The claim that actually matters.
    above = [p for p in pairs if p.confidence >= 0.85]
    wrong = [p for p in above if not p.correct]
    print("-" * 72)
    if not above:
        print("Nothing at or above the 0.85 auto-clear gate — the gate is untested here.")
    elif wrong:
        print(f"GATE BREACHED: {len(wrong)} of {len(above)} predictions at or above "
              f"0.85 were WRONG.")
        for p in wrong[:5]:
            print(f"   {p.scenario} said {p.confidence}")
        print("   The gate is the number to change, not this script.")
    else:
        print(f"Every one of the {len(above)} prediction(s) at or above the 0.85 "
              f"gate was correct.")
    s["above_gate"] = len(above)
    s["above_gate_wrong"] = len(wrong)
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=10,
                    help="batches per ReconRiver scenario")
    ap.add_argument("--json", type=str, default="")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    out: dict = {}

    print("Running ReconRiver, both conditions …", flush=True)
    rrp = pairs_from_reconriver(args.cap)
    out["reconriver"] = report("ReconRiver (third-party synthetic)", rrp)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
