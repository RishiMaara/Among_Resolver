#!/usr/bin/env python3
"""
Measure the figures the documentation quotes, and write them to
docs/benchmarks/latest.json.

WHY THIS EXISTS
---------------
Four documents once claimed four different test counts and three different
50K runtimes. Every one was a real number from a real run; nothing recorded
which run, so they read as contradictions rather than as one measurement
taken at different times.

WHY IT FAILS LOUDLY
-------------------
The first version of this script had a silent fallback on every extraction:

    m = re.search(r"Expected Calibration Error \\(ECE\\):\\s+([\\d\\.]+)", out)
    benchmarks["calibration_ece"] = float(m.group(1)) if m else 0.0763

calibration.py does not print that string — it prints "ECE  (avg gap)". So
the regex never matched, the fallback always fired, and the file recorded a
hardcoded 0.0763 while the tool itself printed 0.0863. A "single source of
truth" that quietly reports a constant when it cannot measure is worse than
no file at all: the old drift was visible if you compared two documents,
this kind is invisible until someone runs the tool.

So: no fallbacks. If a figure cannot be extracted, this script says which
one and exits non-zero, and latest.json is not written.

Run from the repository root:
    python scripts/generate_benchmarks.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

# vitest colours its summary, so "Tests" and the count are separated by escape
# sequences and a plain \s+ between them never matches.
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def run(cmd: list[str], cwd: str | None = None) -> str:
    print(f"  $ {' '.join(cmd)}", flush=True)
    # shell=True on Windows: npx is a .cmd shim and CreateProcess cannot execute
    # it directly, which fails with a bare "cannot find the file specified".
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       check=False, shell=(os.name == "nt"))
    return ANSI.sub("", r.stdout + r.stderr)


def extract(name: str, pattern: str, text: str, cast=float):
    """Pull one figure out of a tool's output, or fail saying which."""
    m = re.search(pattern, text)
    if not m:
        tail = "\n".join(text.strip().splitlines()[-15:])
        raise SystemExit(
            f"\nCould not extract '{name}' with /{pattern}/.\n"
            f"The tool's output ended:\n{tail}\n\n"
            f"Fix the pattern rather than defaulting the value — a figure that "
            f"silently falls back to a constant is how this file came to claim "
            f"an ECE the tool never produced."
        )
    return cast(m.group(1))


def commit() -> str:
    out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True, check=False)
    return out.stdout.strip() or "unknown"


def main() -> int:
    b: dict = {}

    print("Backend tests ...")
    # Collected, not passed. A skipped test is still a test, and the docs quote
    # the collected count -- reading "N passed" here made this file disagree
    # with the README by exactly the number of skips, which is the drift this
    # whole script exists to prevent.
    out = run([sys.executable, "-m", "pytest", "tests/", "-q", "--collect-only"],
              cwd="engine")
    b["backend_tests"] = extract("backend_tests", r"(\d+) tests? collected", out, int)

    print("Frontend tests ...")
    out = run(["npx", "vitest", "run"], cwd=".")
    b["frontend_tests"] = extract("frontend_tests", r"Tests\s+(\d+) passed", out, int)

    print("50K stress run ...")
    out = run([sys.executable, "scripts/run_50k_stress_test.py"], cwd="engine")
    b["stress_50k_reconcile_s"] = extract(
        "stress_50k_reconcile_s", r"Reconciliation time:\s+([\d.]+)s", out)
    b["stress_50k_wall_clock_s"] = extract(
        "stress_50k_wall_clock_s", r"Total wall clock:\s+([\d.]+)s", out)

    print("Calibration ...")
    out = run([sys.executable, "scripts/calibration.py"], cwd="engine")
    b["calibration_ece"] = extract("calibration_ece", r"ECE\s+\(avg gap\)\s+:\s+([\d.]+)", out)
    b["calibration_mce"] = extract("calibration_mce", r"MCE\s+\(worst bucket\)\s+:\s+([\d.]+)", out)
    b["calibration_brier"] = extract("calibration_brier", r"Brier score\s+:\s+([\d.]+)", out)
    b["calibration_scenarios"] = extract("calibration_scenarios", r"predictions\s+:\s+(\d+)", out, int)

    # Read from source rather than run anything: the guard's value IS the fact.
    limit = None
    with open("engine/src/orchestrator.py", encoding="utf-8") as f:
        for line in f:
            if "UNANCHORED_AUTOCLEAR_LIMIT" in line and "environ" in line:
                m = re.search(r'"(\d+)"', line)
                if m:
                    limit = int(m.group(1))
    if limit is None:
        raise SystemExit("Could not read UNANCHORED_AUTOCLEAR_LIMIT from orchestrator.py")
    b["unanchored_autoclear_limit"] = limit

    b["measured_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    b["commit"] = commit()

    os.makedirs("docs/benchmarks", exist_ok=True)
    with open("docs/benchmarks/latest.json", "w", encoding="utf-8") as f:
        json.dump(b, f, indent=2)
        f.write("\n")

    print("\nWrote docs/benchmarks/latest.json")
    for k, v in b.items():
        print(f"  {k:<28} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
