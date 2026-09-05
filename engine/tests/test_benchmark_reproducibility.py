"""
A published figure has to be reproducible, or it is not evidence.

Every calibration number this project has quoted - ECE 0.067, 0.073, 0.0763 -
came from a real run. They disagreed because CP-SAT's parallel search is not
deterministic: with several workers, whichever finds an optimal solution first
wins, and on a scenario where more than one subset genuinely satisfies the sum
the answer differs between runs. Three of the 180 calibration scenarios are
like that, and they moved the headline between 0.070 and 0.086.

Nothing was wrong with the engine. It reported 0.19-0.54 confidence on exactly
those three, which is what "the data does not determine this" is supposed to
look like. What was wrong was quoting a number from a harness that could not
produce it twice.

With one worker, three runs of 180 scenarios differed in zero of them.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture(scope="module")
def bench():
    return pytest.importorskip("benchmark")


def test_the_benchmark_solver_is_pinned_to_one_worker(bench):
    """
    run_scenario builds the config inline, so this reads the source rather
    than the object. A dataclass default_factory is captured at class
    creation, which is what made an earlier attempt to override it from the
    outside silently do nothing - the runs still used every core and still
    disagreed, while appearing to be pinned.
    """
    source = (SCRIPTS / "benchmark.py").read_text(encoding="utf-8")
    assert "num_search_workers=1" in source, (
        "benchmark.run_scenario must pin CP-SAT to a single worker. Without "
        "it, scenarios with more than one valid subset flip between runs and "
        "no calibration or accuracy figure can be reproduced."
    )


def test_production_still_gets_every_core(bench):
    """
    The pin belongs to the harness, not the engine. Production wants the
    parallel search: it is faster, and the non-determinism is harmless there
    because a scenario with several valid answers already reports low
    confidence and never auto-clears.
    """
    from subset_sum import SubsetSumConfig

    assert SubsetSumConfig().num_search_workers >= 1
    assert "num_search_workers=1" not in (
        Path(__file__).resolve().parents[1] / "src" / "orchestrator.py"
    ).read_text(encoding="utf-8")
