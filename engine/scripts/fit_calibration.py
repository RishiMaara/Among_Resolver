#!/usr/bin/env python3
"""
Fit the confidence calibration map, and judge it on data it did not see.

THREE FITS
----------
  benchmark -> ReconRiver   fit on our 180 scenarios, score on ReconRiver
  ReconRiver -> benchmark   the other way round
  both                      the map that ships (src/calibration_map.json)

The two cross-corpus rows are the evidence; the shipped map is fitted on every
measured prediction, because both corpora are measurements and neither is the
merchant's data. What a map fitted elsewhere is worth on one merchant's data
is what the reviewer outcomes in GET /calibration exist to show.

ECE is reported raw and mapped for each held-out corpus, with the low band
called out: out-of-sample it said about 0.14 and was right 3.1% of the time.

From engine/ (about three minutes):
    python scripts/fit_calibration.py
    python scripts/fit_calibration.py --json docs/benchmarks/calibration_fit.json --write-map
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

os.environ.setdefault("SETTLEMENT_CYCLE_STORE", "memory")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

import calibration as cal                 # noqa: E402
import calibration_map as cm              # noqa: E402
import calibration_out_of_sample as oos   # noqa: E402


def low_band(pairs, mapping=None):
    f = mapping or (lambda c: c)
    inb = [(f(c), ok) for c, ok in pairs if c < 0.5]
    if not inb:
        return None
    return {"n": len(inb), "said": round(sum(p for p, _ in inb) / len(inb), 4),
            "right": round(sum(1 for _, ok in inb if ok) / len(inb), 4)}


def judge(name, train, test):
    m = cm.fit(train, source=name)
    return {"fit": name, "train_pairs": len(train), "test_pairs": len(test),
            "ece_raw": cm.ece(test), "ece_calibrated": cm.ece(test, m),
            "low_band_raw": low_band(test), "low_band_calibrated": low_band(test, m)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", type=int, default=180)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--cap", type=int, default=10)
    ap.add_argument("--json", default="")
    ap.add_argument("--write-map", action="store_true")
    args = ap.parse_args()
    logging.disable(logging.WARNING)

    bench = [(p.confidence, p.correct) for p in cal.collect_from_benchmark(args.scenarios, args.seed)]
    river = [(p.confidence, p.correct) for p in oos.pairs_from_reconriver(args.cap)]

    both = cm.fit(bench + river, source="benchmark + ReconRiver")
    result = {
        "benchmark_pairs": len(bench), "reconriver_pairs": len(river),
        "cross": [judge("benchmark -> ReconRiver", bench, river),
                  judge("ReconRiver -> benchmark", river, bench)],
        "shipped_map": both.to_dict(),
        "gate_note": ("Auto-clear still reads the raw confidence; the calibrated figure is "
                      "shown beside it."),
    }
    print(json.dumps({k: v for k, v in result.items() if k != "shipped_map"}, indent=1))
    if args.write_map:
        with open(cm.MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(both.to_dict(), f, indent=1)
        print(f"wrote {cm.MAP_PATH}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
