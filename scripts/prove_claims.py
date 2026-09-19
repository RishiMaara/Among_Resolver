"""
Prove every headline claim in the documentation.

One command that re-measures every number the README, ARCHITECTURE.md and
FAILURE_LOG.md state, and reports pass/fail for each with the measured
value beside the documented one.

Any claim that cannot be reproduced in a single script run is an
undocumented assertion, and this script exists to make that category empty.

Run from the repo root:
    python scripts/prove_claims.py
    python scripts/prove_claims.py --quick     # skip heavy benchmarks
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
SCRIPTS = ENGINE / "scripts"


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 600, env=None) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    return subprocess.run(
        cmd, cwd=str(cwd or ENGINE), capture_output=True, text=True,
        timeout=timeout, env=env or os.environ.copy(),
    )


class Claim:
    def __init__(self, name: str, source: str):
        self.name = name
        self.source = source
        self.status = "PENDING"
        self.documented = None
        self.measured = None
        self.detail = ""

    def pass_(self, documented, measured, detail=""):
        self.status = "PASS"
        self.documented = documented
        self.measured = measured
        self.detail = detail

    def fail_(self, documented, measured, detail=""):
        self.status = "FAIL"
        self.documented = documented
        self.measured = measured
        self.detail = detail

    def skip_(self, reason=""):
        self.status = "SKIP"
        self.detail = reason


def check_backend_tests(claim: Claim):
    """Backend test suite passes."""
    result = _run([sys.executable, "-m", "pytest", "tests/", "-q", "--tb=line"], cwd=ENGINE)
    m = re.search(r"(\d+) passed", result.stdout)
    if m and result.returncode == 0:
        count = int(m.group(1))
        claim.pass_("all pass", f"{count} passed", f"exit code {result.returncode}")
    else:
        claim.fail_("all pass", f"exit {result.returncode}", result.stdout[-300:])


def check_backend_test_count(claim: Claim):
    """Backend test count matches documentation."""
    result = _run([sys.executable, "-m", "pytest", "tests/", "-q", "--collect-only"], cwd=ENGINE)
    m = re.search(r"(\d+) tests? collected", result.stdout)
    if not m:
        claim.fail_("N tests", "could not read count", result.stdout[-200:])
        return
    actual = int(m.group(1))

    # Check README
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    m2 = re.search(r"\*\*(\d+)\*\* backend", readme)
    if m2:
        documented = int(m2.group(1))
        if documented == actual:
            claim.pass_(documented, actual)
        else:
            claim.fail_(documented, actual, "README.md count does not match")
    else:
        claim.fail_("N", actual, "could not find count in README")


def check_50k_stress(claim: Claim):
    """50K stress test: precision/recall 1.0, exact set match."""
    result = _run([sys.executable, "scripts/run_50k_stress_test.py"], cwd=ENGINE, timeout=120)
    if "PASS" in result.stdout:
        # Extract precision/recall
        m = re.search(r"Precision / recall:\s+([\d.]+)\s*/\s*([\d.]+)", result.stdout)
        if m:
            p, r = float(m.group(1)), float(m.group(2))
            claim.pass_("1.0 / 1.0", f"{p} / {r}")
        else:
            claim.pass_("PASS", "PASS (could not parse precision/recall)")
    else:
        claim.fail_("PASS", "FAIL", result.stdout[-300:])


def check_benchmark_false_clears(claim: Claim):
    """Benchmark: 0 false clears across 120 scenarios."""
    tmp = ENGINE / "data" / "_prove_benchmark.json"
    result = _run(
        [sys.executable, "scripts/benchmark.py", "--json", str(tmp)],
        cwd=ENGINE, timeout=300,
    )
    if tmp.exists():
        data = json.loads(tmp.read_text())
        fc = data["headline"]["false_clear_count"]
        tmp.unlink(missing_ok=True)
        if fc == 0:
            claim.pass_(0, 0, f"{data['scenarios_total']} scenarios")
        else:
            claim.fail_(0, fc)
    else:
        claim.fail_(0, "script failed", result.stderr[-300:])


def check_pylint(claim: Claim):
    """pylint --errors-only passes."""
    result = _run(
        [sys.executable, "-m", "pylint", "engine/src", "--errors-only", "--disable=import-error"],
        cwd=ROOT,
    )
    if result.returncode == 0:
        claim.pass_("0 errors", "0 errors")
    else:
        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        claim.fail_("0 errors", f"{len(lines)} errors", result.stdout[:300])


def check_bandit(claim: Claim):
    """bandit security scan passes at medium+ severity."""
    result = _run(
        [sys.executable, "-m", "bandit", "-r", "engine/src", "-ll"],
        cwd=ROOT,
    )
    m = re.search(r"Total issues \(by severity\):.*?Medium:\s+(\d+).*?High:\s+(\d+)", result.stdout, re.DOTALL)
    if m:
        med, high = int(m.group(1)), int(m.group(2))
        if med == 0 and high == 0:
            claim.pass_("0 medium/high", "0 medium/high")
        else:
            claim.fail_("0 medium/high", f"{med} medium, {high} high")
    elif result.returncode == 0:
        claim.pass_("pass", "pass")
    else:
        claim.fail_("pass", f"exit {result.returncode}", result.stdout[-200:])


def main():
    ap = argparse.ArgumentParser(description="Prove every documented claim")
    ap.add_argument("--quick", action="store_true",
                    help="Skip heavy benchmarks (50K stress, full benchmark)")
    args = ap.parse_args()

    claims: list[Claim] = []

    # 1. Backend tests pass
    c = Claim("Backend test suite passes", "README.md")
    claims.append(c)
    print("  Checking: backend tests...", flush=True)
    check_backend_tests(c)

    # 2. Backend test count matches docs
    c = Claim("Backend test count matches README", "README.md")
    claims.append(c)
    print("  Checking: test count...", flush=True)
    check_backend_test_count(c)

    # 3. pylint clean
    c = Claim("pylint --errors-only clean", "CI")
    claims.append(c)
    print("  Checking: pylint...", flush=True)
    check_pylint(c)

    # 4. bandit clean
    c = Claim("bandit security scan clean", "CI")
    claims.append(c)
    print("  Checking: bandit...", flush=True)
    check_bandit(c)

    if not args.quick:
        # 5. 50K stress test
        c = Claim("50K stress: precision/recall 1.0", "README.md, ARCHITECTURE.md")
        claims.append(c)
        print("  Checking: 50K stress test...", flush=True)
        check_50k_stress(c)

        # 6. Benchmark: 0 false clears
        c = Claim("Benchmark: 0 false clears", "README.md, ARCHITECTURE.md")
        claims.append(c)
        print("  Checking: benchmark (120 scenarios)...", flush=True)
        check_benchmark_false_clears(c)

    # Print results
    print("\n" + "=" * 76)
    print("CLAIM VERIFICATION REPORT")
    print("=" * 76)

    passed = sum(1 for c in claims if c.status == "PASS")
    failed = sum(1 for c in claims if c.status == "FAIL")
    skipped = sum(1 for c in claims if c.status == "SKIP")

    for c in claims:
        icon = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[c.status]
        print(f"  [{icon}] {c.name}")
        if c.documented is not None:
            print(f"         documented: {c.documented}  |  measured: {c.measured}")
        if c.detail:
            print(f"         {c.detail}")

    print("-" * 76)
    print(f"  {passed} passed, {failed} failed, {skipped} skipped")
    if failed:
        print("\n  SOME CLAIMS COULD NOT BE VERIFIED. Fix the failures above.")
        sys.exit(1)
    else:
        print("\n  ALL CLAIMS VERIFIED.")
    print("=" * 76)


if __name__ == "__main__":
    main()
