#!/usr/bin/env python3
"""
Are the hand-set linkage weights any good?

WHY THIS EXISTS
---------------
0.55 / 0.25 / 0.15 / 0.10 are reasoned from how forgeable each signal is, not
fitted to data. LINKAGE.md says so and lists it as a gap, and a reviewer
listed it again. Reasoning is a decent way to pick a starting point and a poor
way to defend a number.

Fitting them properly is the wrong move here, and deliberately not what this
does: the only labelled data available is our own generated benchmark, so a
weight learned on it would be tuned to scenarios we wrote while looking far
more authoritative than a reasoned one. The useful question is narrower and
answerable — **does the exact value matter?**

WHAT IT MEASURES
----------------
Each weight is moved up and down on its own, everything else held, and the
benchmark is re-run. Two readings matter:

  false clears     must stay 0 at every setting. If a perturbation produces
                   one, that weight is load-bearing for safety and its value
                   is not a judgement call any more.

  auto-clear rate  how much accuracy moves. A weight that changes nothing is
                   not doing work; one that changes a lot deserves fitting
                   rather than reasoning.

A flat result is the good outcome and the honest one to report: it says the
engine is not balanced on a knife edge of numbers somebody guessed.

Run from engine/ (about a minute per row):
    python scripts/sweep_linkage_weights.py
    python scripts/sweep_linkage_weights.py --delta 0.10 --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.abspath(os.path.join(HERE, ".."))

WEIGHTS = {
    "W_SETTLEMENT_ID": 0.55,
    "W_SHARED_REF_TOKEN": 0.25,
    "W_REF_PREFIX_CLUSTER": 0.15,
    "W_CROSS_SOURCE_AMOUNT": 0.10,
    "W_OUT_OF_WINDOW_PENALTY": 0.30,
}


def run_benchmark(overrides: dict) -> dict | None:
    """
    One benchmark run in a subprocess, so each setting is read at import time.

    A subprocess rather than monkeypatching in-process: the weights are read
    into module constants when linkage is imported, so mutating them after the
    fact would leave whichever value was captured first in place and every row
    of this table would silently measure the same configuration.
    """
    env = dict(os.environ)
    env.update({k: f"{v:.4f}" for k, v in overrides.items()})
    out = subprocess.run(
        [sys.executable, "scripts/benchmark.py"],
        cwd=ENGINE, env=env, capture_output=True, text=True, check=False,
    ).stdout

    def grab(pattern):
        m = re.search(pattern, out)
        return float(m.group(1)) if m else None

    fc = re.search(r"FALSE CLEARS\s+:\s+(\d+)", out)
    return {
        "false_clears": int(fc.group(1)) if fc else None,
        "auto_clear_pct": grab(r"Auto-cleared & correct\s+:\s+([\d.]+)%"),
        "truth_found_pct": grab(r"Truth identified\s+:\s+([\d.]+)%"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delta", type=float, default=0.10)
    ap.add_argument("--json", type=str, default="")
    args = ap.parse_args()

    print("Baseline (the shipped weights) …", flush=True)
    base = run_benchmark({})
    if base["auto_clear_pct"] is None:
        print("Could not read the benchmark's output; nothing measured.")
        return 1
    print(f"  auto-clear {base['auto_clear_pct']}%  "
          f"truth {base['truth_found_pct']}%  false clears {base['false_clears']}\n")

    # Control: every weight zeroed. linkage.py keeps only candidates scoring
    # above zero, so this should collapse the pool and take accuracy with it.
    # Without this row a flat table cannot be told apart from an override that
    # never reached the module -- which is exactly what a flat table looks like.
    print("Control: all weights zeroed …", flush=True)
    zeroed = run_benchmark({k: 0.0 for k in WEIGHTS if k != "W_OUT_OF_WINDOW_PENALTY"})
    print(f"  auto-clear {zeroed['auto_clear_pct']}%  "
          f"false clears {zeroed['false_clears']}")
    if zeroed["auto_clear_pct"] == base["auto_clear_pct"]:
        print("\n  The control did not move the result. The overrides are not "
              "reaching linkage, and every row below would be measuring the "
              "same configuration. Stopping rather than reporting a flat table.")
        return 1
    print()

    rows, unsafe = [], []
    for name, default in WEIGHTS.items():
        for direction in (-1, 1):
            value = round(default + direction * args.delta, 4)
            if value < 0:
                continue
            r = run_benchmark({name: value})
            delta_ac = (r["auto_clear_pct"] or 0) - base["auto_clear_pct"]
            rows.append({"weight": name, "value": value, **r,
                         "auto_clear_delta": round(delta_ac, 2)})
            flag = ""
            if r["false_clears"]:
                flag = "   <- FALSE CLEARS"
                unsafe.append(rows[-1])
            print(f"{name:<26} {default:>5.2f} -> {value:<5.2f} "
                  f"auto-clear {r['auto_clear_pct']:>6.2f}% "
                  f"({delta_ac:+.2f})  false clears {r['false_clears']}{flag}",
                  flush=True)

    moves = [abs(r["auto_clear_delta"]) for r in rows]
    print("\n" + "=" * 72)
    print(f"largest move in auto-clear from a {args.delta:+.2f} perturbation: "
          f"{max(moves):.2f} points")
    if unsafe:
        print(f"{len(unsafe)} setting(s) produced FALSE CLEARS. Those weights are "
              f"load-bearing for safety, not judgement calls:")
        for r in unsafe:
            print(f"   {r['weight']} = {r['value']}  ->  {r['false_clears']}")
    else:
        print("No setting produced a false clear.")
    print()
    print(f"Zeroing every weight took auto-clear to {zeroed['auto_clear_pct']}%, "
          f"so the weights ARE load-bearing — linkage keeps only candidates "
          f"scoring above zero (linkage.py: `c.score > 0.0`), and with no "
          f"weights nothing links at all.")
    if max(moves) == 0:
        print("But no individual value mattered. On this benchmark the work is "
              "done by WHICH signals fire, not by how they are weighted: tier "
              "membership is decided by the anchor boolean, and perturbations "
              "across the 0.25 strong-link boundary moved nothing either. That "
              "is measured on a corpus where anchors are plentiful; it does not "
              "follow that the weights are irrelevant where they are not.")
    print("=" * 72)

    if args.json:
        with open(os.path.join(ENGINE, args.json) if not os.path.isabs(args.json)
                  else args.json, "w", encoding="utf-8") as f:
            json.dump({"baseline": base, "delta": args.delta, "rows": rows}, f, indent=2)
        print(f"Wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
