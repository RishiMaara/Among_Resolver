"""
No model decides which payments compose a settlement.

The README and the judge page say so; this is what makes it a property of the
code rather than of the documentation. With a model "configured" and every
call to it made to fail loudly, the engine still reconciles, and reaches the
answer it reaches with no model at all. Models read language elsewhere —
column headers, narrations, questions — and each of those is opt-in.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import llm_provider
from orchestrator import reconcile_batch
from subset_sum import SubsetSumConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from benchmark import build_scenario  # noqa: E402

CFG = SubsetSumConfig(tolerance_cents=10, solver_time_limit_s=5.0,
                      ambiguity_probe_limit=2, num_search_workers=1)


def test_reconciling_never_calls_a_model(monkeypatch):
    sc = build_scenario(7, "clean", True, "sparse", random.Random(1))
    without = reconcile_batch(sc.batch, sc.candidates, subset_config=CFG,
                              settlement_window_days=5)

    def called(*_a, **_k):
        raise AssertionError("a model was called on the path that decides membership")

    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    monkeypatch.setattr(llm_provider, "generate", called)
    with_model = reconcile_batch(sc.batch, sc.candidates, subset_config=CFG,
                                 settlement_window_days=5)

    assert with_model.match_result.cleared == without.match_result.cleared
    assert sorted(with_model.match_result.matched_txn_ids) == \
        sorted(without.match_result.matched_txn_ids)
