from __future__ import annotations
import logging
from typing import List

from schema import NormalizedTxn
from ortools.sat.python import cp_model

logger = logging.getLogger(__name__)

def exact_subset_sum_nm(
    candidates: List[NormalizedTxn],
    target_cents_list: List[int],
    tolerance_cents: int = 5,
    time_limit_s: float = 10.0,
    num_search_workers: int = 1,
) -> List[List[NormalizedTxn]] | None:
    """
    N:M solver using CP-SAT.
    Maps a pool of candidates to multiple targets simultaneously.
    Each candidate can be assigned to at most ONE target.
    
    Returns a list of matched subsets (one list of NormalizedTxn per target)
    if an optimal/feasible solution is found, else None.
    """
    if not candidates or not target_cents_list:
        return None

    model = cp_model.CpModel()
    num_c = len(candidates)
    num_t = len(target_cents_list)

    # include[c][t] is 1 if candidate c is assigned to target t.
    include = {}
    for c in range(num_c):
        for t in range(num_t):
            include[(c, t)] = model.NewBoolVar(f"include_c{c}_t{t}")

    # A candidate can be assigned to at most one target
    for c in range(num_c):
        model.AddAtMostOne(include[(c, t)] for t in range(num_t))

    # Each target must be met within tolerance
    for t in range(num_t):
        target = target_cents_list[t]
        total_t = sum(candidates[c].amount_cents * include[(c, t)] for c in range(num_c))
        model.Add(total_t >= target - tolerance_cents)
        model.Add(total_t <= target + tolerance_cents)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = num_search_workers
    
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None

    results = []
    for t in range(num_t):
        matched_for_t = [candidates[c] for c in range(num_c) if solver.Value(include[(c, t)])]
        results.append(matched_for_t)

    return results
