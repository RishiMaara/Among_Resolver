#!/usr/bin/env python3
"""
The 120-scenario benchmark on every seed the documents quote, in one file.

Seeds 7 and 101 were looked at while tuning; 202 and 303 never were, and are
the held-out check. The control erases every description; the declared run
tells the engine which feed holds the members; the mixed-method runs give
each payment a stated method and compute each payout per payment, once with
the methods visible to the engine and once hidden, which is how it
reconciled before fees were estimated per method.

From engine/ (about twenty minutes):
    python scripts/benchmark_seeds.py --json docs/benchmarks/benchmark_seeds.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
sys.path.insert(0, os.path.dirname(__file__))

import benchmark as bench  # noqa: E402

TUNING, HELD_OUT = ("7", "101"), ("202", "303")


def run(seed: int, **kw) -> dict:
    rng = random.Random(seed)
    densities = list(bench.DENSITIES)
    scenarios = []
    for i in range(120):
        family, solvable, _ = bench.FAMILIES[i % len(bench.FAMILIES)]
        density = densities[(i // len(bench.FAMILIES)) % len(densities)]
        scenarios.append(bench.build_scenario(i, family, solvable, density, rng,
                                              declare_source=kw.get("declare", False),
                                              methods=kw.get("methods", False)))
    for sc in scenarios:
        for t in sc.candidates:
            if kw.get("blank"):
                t.memo_raw = t.memo_normalized = ""
            if kw.get("hide_methods"):
                t.extra = {}
    h = bench.summarise([bench.run_scenario(sc) for sc in scenarios])["headline"]
    return {k: h[k] for k in ("auto_cleared_correct_pct", "truth_identified_pct", "false_clear_count")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    out: dict = {"tuning_seeds": list(TUNING), "held_out_seeds": list(HELD_OUT), "seeds": {}}
    for seed in TUNING + HELD_OUT:
        out["seeds"][seed] = run(int(seed))
        print(seed, out["seeds"][seed], flush=True)
    out["control_descriptions_erased_seed_7"] = run(7, blank=True)
    out["member_feed_declared_seed_7"] = run(7, declare=True)
    out["mixed_methods"] = {
        seed: {"methods_seen": run(int(seed), methods=True),
               "methods_hidden": run(int(seed), methods=True, hide_methods=True)}
        for seed in ("7", "202")
    }
    out["measured_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(json.dumps(out, indent=1))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
