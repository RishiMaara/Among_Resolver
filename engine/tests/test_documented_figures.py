"""
The documentation's test count has to be the real one.

This project's whole claim is that its numbers are measured. The test count
reached four different values across four documents - 179, 157, 246 and a
README saying 179 while the suite held 275 - because every figure was typed by
hand at a moment when it was briefly true, and nothing ever checked it again.

An external reviewer found all four in about a minute. Their note was the
sharpest thing said about this project: a judge who catches one stops trusting
the rest, and honesty stops being an asset the moment it is only claimed.

So the number is no longer maintained by hand. If it drifts, this fails, and
the failing message says which files to correct.
"""

import functools
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TESTS = Path(__file__).resolve().parent

# Where the backend test count is written down, and the pattern that finds it.
DOCUMENTED = [
    (ROOT / "README.md", r"\*\*(\d+)\*\* backend"),
    (ROOT / "README.md", r"tests/\s+(\d+) tests"),
    (ROOT / "docs" / "ARCHITECTURE.md", r"pytest tests/ -q\s+# (\d+) tests"),
    (ROOT / "docs" / "TEST_REPORT.md", r"Backend\s+(\d+) passed"),
]


@functools.lru_cache(maxsize=1)
def _collected_count() -> int:
    """What pytest actually collects, asked of pytest rather than guessed.

    Cached: this spawns a nested pytest, and four parametrised cases asking
    the same question four times turns a 9-second check into 36.
    """
    out = subprocess.run(
        [sys.executable, "-m", "pytest", str(TESTS), "-q", "--collect-only"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=300, check=False,
    )
    m = re.search(r"(\d+) tests? collected", out.stdout)
    if not m:
        pytest.skip(f"could not read a collection count from pytest: {out.stdout[-300:]}")
    return int(m.group(1))


@pytest.mark.parametrize("path,pattern", DOCUMENTED, ids=lambda v: getattr(v, "name", ""))
def test_the_documented_test_count_is_the_real_one(path, pattern):
    if not path.exists():
        pytest.skip(f"{path.name} is not in this checkout")
    m = re.search(pattern, path.read_text(encoding="utf-8"))
    assert m, f"{path.name} no longer states a test count where this looked for one"

    documented, actual = int(m.group(1)), _collected_count()
    assert documented == actual, (
        f"{path.name} says {documented} backend tests; pytest collects {actual}. "
        f"Update every place the count appears - README.md (twice), "
        f"docs/ARCHITECTURE.md and docs/TEST_REPORT.md - or the four will "
        f"disagree again, which is exactly how they got to 179/157/246/275."
    )
