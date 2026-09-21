#!/usr/bin/env python3
"""
Does the investigator help on settlements the engine withheld — and what does
its verifier buy?

THE SET
-------
The benchmark's 120 scenarios (benchmark.py, seed 7). Every one the engine
did NOT clear is a case: some are solvable (the true set is in the pool and
something made it ambiguous), some are not (a member is missing or out of
the window — the right answer there is anything but a match).

SCORING
-------
  right      solvable: a MATCH_PROPOSAL naming exactly the true set.
             unsolvable: any action other than a match.
  harmful    a MATCH_PROPOSAL naming a set that is not the truth — the thing
             that, rubber-stamped, becomes a wrong approval.

Each proposer is scored twice. With the verifier, a proposal that fails its
checks never reaches a reviewer (it is shown as rejected); without it, every
proposal does. The difference in harmful proposals reaching a reviewer is
what the verifier is worth.

"model" scores only the cases the model answered. "investigator" is what is
deployed and the figure to quote: every withheld case, the model's proposal
where it gave one and the rules' where it did not, as investigate() falls
back in production.

THE CASE THE MODEL SEES IS REDACTED
-----------------------------------
Ids and settlement names are replaced by opaque aliases, and reference and
memo text is removed, leaving a per-record flag for whether its reference
names the settlement. The benchmark's labels live in exactly those places —
members are named S19_TRUE_0, memos say "decoy" — and the first run, before
this, showed the model reading them. So this measures reasoning over
amounts, dates and linkage evidence, not reading real narrations, which a
synthetic corpus cannot test fairly.

The model pass spends one call per withheld settlement from the
GEMINI_API_KEY holder's quota; --cache keeps the answers so re-scoring is free.

From engine/:
    python scripts/investigation_eval.py
    python scripts/investigation_eval.py --llm --cache /tmp/inv.json --json docs/benchmarks/investigation_eval.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import sys
import time

os.environ.setdefault("SETTLEMENT_CYCLE_STORE", "memory")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

import investigation_agent as inv  # noqa: E402
from benchmark import DENSITIES, FAMILIES, build_scenario  # noqa: E402
from orchestrator import reconcile_batch  # noqa: E402
from subset_sum import SubsetSumConfig  # noqa: E402

CFG = SubsetSumConfig(tolerance_cents=10, solver_time_limit_s=10.0,
                      ambiguity_probe_limit=2, num_search_workers=1)


def scenarios(n, seed):
    rng = random.Random(seed)
    densities = list(DENSITIES)
    out = []
    for i in range(n):
        family, solvable, _ = FAMILIES[i % len(FAMILIES)]
        density = densities[(i // len(FAMILIES)) % len(densities)]
        out.append(build_scenario(i, family, solvable, density, rng))
    return out


def judge(p: dict, verdict: dict, sc) -> dict:
    is_match = p["action"] == "MATCH_PROPOSAL"
    exact = is_match and set(p["txn_ids"]) == sc.truth_ids
    right = exact if sc.solvable else not is_match
    harmful = is_match and not exact
    return {"right": right, "harmful": harmful, "valid": verdict["valid"],
            "reaches_reviewer_with_verifier": verdict["valid"],
            "right_with_verifier": right and verdict["valid"] if is_match else right}


def summarise(rows: list[dict]) -> dict:
    n = len(rows) or 1
    return {
        "cases": len(rows),
        "right": sum(r["right"] for r in rows),
        "right_pct": round(100 * sum(r["right"] for r in rows) / n, 1),
        "rejected_by_verifier": sum(not r["valid"] for r in rows),
        # What a reviewer actually receives that is right: a proposal the
        # verifier passed and that is the correct action.
        "right_and_reaching_reviewer": sum(r["right"] and r["valid"] for r in rows),
        "harmful_reaching_reviewer_without_verifier": sum(r["harmful"] for r in rows),
        "harmful_reaching_reviewer_with_verifier": sum(r["harmful"] and r["valid"] for r in rows),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--scenarios", type=int, default=120)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--cache", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--pace", type=float, default=6.5, help="seconds between model calls")
    ap.add_argument("--offline", action="store_true",
                    help="score from the cache only; never call the model")
    args = ap.parse_args()
    logging.disable(logging.WARNING)
    if args.llm:
        try:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(HERE, "..", ".env"), override=False)
        except ImportError:
            pass

    cache = {}
    if args.cache and os.path.exists(args.cache):
        with open(args.cache, encoding="utf-8") as f:
            cache = json.load(f)

    rule_rows, model_rows, deployed_rows, detail = [], [], [], []
    calls = 0
    for sc in scenarios(args.scenarios, args.seed):
        report = reconcile_batch(sc.batch, sc.candidates, subset_config=CFG, settlement_window_days=5)
        if report.match_result.cleared:
            continue
        case = inv.build_case(sc.batch, sc.candidates, report,
                              tolerance_cents=CFG.tolerance_cents, workers=1)
        rp = inv.propose_rules(case)
        rv = inv.verify(rp, case)
        rule_rows.append(judge(rp.__dict__, rv, sc))
        row = {"scenario": sc.scenario_id, "family": sc.family, "solvable": sc.solvable,
               "alternatives": len(case["alternatives"]),
               "rules": {"action": rp.action, **rule_rows[-1]}}
        if args.llm:
            # Keyed by what the model is shown, not only the scenario: when the
            # case changes (FAILURE_LOG 30 changed what it contains), an answer
            # to the old case must not be scored against the new one.
            shown = json.dumps(inv._aliased(case, True)[0], sort_keys=True, default=str)
            key = f"{sc.scenario_id}:" + hashlib.sha256(
                (shown + inv._SYSTEM).encode("utf-8")).hexdigest()[:16]
            if cache.get(key):
                mp = inv.Proposal(**cache[key])
            elif args.offline:
                mp = None
            else:
                # A missing or failed answer is asked for again; only answers
                # are cached, so a rate-limited call does not become "no answer".
                if calls:
                    time.sleep(args.pace)       # stay under a free tier's per-minute limit
                mp = inv.propose_model(case, redact_text=True)
                calls += 1
                cache[key] = mp.__dict__ if mp else None
            if mp is not None:
                mv = inv.verify(mp, case)
                model_rows.append(judge(mp.__dict__, mv, sc))
                row["model"] = {"action": mp.action, **model_rows[-1],
                                "failed": mv["failed"][:3]}
            # As deployed: investigate() falls back to the rules when the model
            # gives no usable answer, so an unanswered case is scored as the
            # rules scored it, not dropped.
            deployed_rows.append(model_rows[-1] if mp is not None else rule_rows[-1])
        detail.append(row)

    if args.cache and args.llm:
        with open(args.cache, "w", encoding="utf-8") as f:
            json.dump(cache, f)

    result = {"scenarios": args.scenarios, "seed": args.seed,
              "withheld_cases": len(rule_rows),
              "solvable_cases": sum(1 for d in detail if d["solvable"]),
              "rules": summarise(rule_rows)}
    if args.llm:
        result["model"] = summarise(model_rows)
        result["model_answered"] = len(model_rows)
        # The figure to quote: every withheld case, the model where it
        # answered and the rules where it did not — what a reviewer receives.
        result["investigator"] = summarise(deployed_rows)
        result["model_calls_this_run"] = calls
        result["model_name"] = inv.llm_provider.DEFAULT_MODEL
    print(json.dumps(result, indent=1))
    if args.json:
        result["cases"] = detail
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
