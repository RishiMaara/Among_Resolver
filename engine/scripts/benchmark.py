"""
Reconciliation accuracy benchmark.

WHY THIS EXISTS
---------------
The 50K stress test proves throughput. It does not prove accuracy: it runs
ONE settlement batch, finds ONE subset, and its ground truth records only
`true_subset_count: 5` — so it asserts count-and-sum, not identity. A
different 5-transaction subset summing to the same rupee value would have
passed it. That is a cherry-picked match, and a cherry-picked match proves
nothing.

This harness measures accuracy the way it has to be measured: many
independent scenarios, ground truth recorded as explicit transaction IDs,
and the engine's answer compared by IDENTITY against those IDs.

THE DENSITY FINDING THIS BENCHMARK IS BUILT AROUND
---------------------------------------------------
Subset-sum does not uniquely identify a settlement at realistic pool
density, and no amount of solver sophistication changes that. For a pool of
n transactions and a settlement of k, there are C(n,k) candidate subsets
competing for a target that can only take ~2e6 distinct paise values:

    pool   60, k=5  ->  5.5e6 subsets  ~     2.8 subsets per target value
    pool  200, k=5  ->  2.5e9 subsets  ~   1,284 per target value
    pool 1200, k=5  ->  2.1e13 subsets ~ 10.4 MILLION per target value

At the dense end the true subset is not recoverable from arithmetic at all
— millions of subsets sum to exactly the same rupee value. An engine that
confidently clears one of them is not accurate, it is guessing with a
confident face. The correct behaviour is to detect the ambiguity, refuse to
auto-clear, and use non-arithmetic signal (reference IDs, memo text) to
propose the most plausible candidate for a human.

So this benchmark sweeps density deliberately and scores the two regimes
differently, because the right answer differs between them.

WHAT IT MEASURES, AND WHY
-------------------------
Reconciliation is asymmetric. Failing to clear is an inconvenience — a human
looks at it. Clearing the WRONG set silently books money against the wrong
invoices and nobody finds out until a customer complains. So the headline
metric is deliberately not "match rate":

  false_clear          auto-cleared, but the set is not the true set.
                       The dangerous error. Must be ~0.
  auto_cleared_correct unambiguous and right — the ideal outcome.
  ambiguous_correct    correctly refused to auto-clear, AND the candidate
                       it surfaced for review is the true set. This is the
                       tiebreak (Agent 3b) doing its job.
  correct_abstention   scenario is genuinely unsolvable (a leg is missing);
                       engine correctly did not clear. A system that always
                       clears scores 0 here while looking great on match rate.

Scenarios include cases the engine is EXPECTED to fail or refuse. That is
the point — a benchmark containing only winnable cases measures nothing.

Run:
    python scripts/benchmark.py                  # default 120 scenarios
    python scripts/benchmark.py --scenarios 320
    python scripts/benchmark.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence
from subset_sum import SubsetSumConfig
from orchestrator import reconcile_batch
from fee_decomposition import DEFAULT_RATE_CARD

BASE_TIME = datetime(2026, 8, 20, tzinfo=timezone.utc)

# Pool sizes chosen to straddle the density threshold computed above.
DENSITIES = {
    "sparse": 60,     # ~2.8 competing subsets per target — usually identifiable
    "medium": 200,    # ~1.3e3 competing — usually ambiguous
    "dense": 700,     # ~1e6 competing — arithmetically hopeless by design
}

# `solvable=False` means the true subset cannot be recovered from the data
# the engine is given; the correct behaviour is to NOT clear.
FAMILIES = [
    ("clean", True, "Well-formed batch, ordinary noise."),
    ("near_collision", True, "Decoy subsets summing to within a few paise of the target."),
    ("exact_collision", True, "A second subset sums EXACTLY to the target."),
    ("duplicate_amounts", True, "Many identical amounts in the pool."),
    ("wide_spread", True, "True members spread across the full settlement window."),
    ("large_subset", True, "Settlement composed of many small transactions."),
    ("missing_leg", False, "A true member is absent from the data entirely."),
    ("out_of_window", False, "A true member sits outside the settlement window."),
    # ── linkage-degradation families ──────────────────────────────────────
    # The families above all hand every true member a clean settlement
    # reference. Real feeds do not: references go missing, get truncated by
    # field limits, and collide with unrelated traffic. Without these the
    # benchmark only proves linkage works when linkage is easy, which is
    # not the claim worth making.
    ("ref_missing", True, "True members carry NO reference — linkage must fall back to amount/cross-source."),
    ("ref_truncated", True, "True members' references truncated below the token threshold."),
    ("ref_collision", True, "Unrelated noise shares the settlement's reference token — false anchors."),
    ("ref_partial", True, "Only some true members carry the settlement reference."),
]


@dataclass
class Scenario:
    scenario_id: str
    family: str
    density: str
    solvable: bool
    batch: SettlementBatch
    candidates: list[NormalizedTxn]
    truth_ids: set[str]
    pool_size: int
    subset_size: int


@dataclass
class Result:
    scenario_id: str
    family: str
    density: str
    solvable: bool
    pool_size: int
    subset_size: int
    cleared: bool
    ambiguous: bool
    method: str
    confidence: float = 0.0
    matched_ids: set[str] = field(default_factory=set)
    truth_ids: set[str] = field(default_factory=set)
    elapsed_s: float = 0.0
    exception_count: int = 0

    @property
    def set_is_truth(self) -> bool:
        return bool(self.matched_ids) and self.matched_ids == self.truth_ids

    @property
    def auto_cleared_correct(self) -> bool:
        return self.cleared and self.set_is_truth

    @property
    def false_clear(self) -> bool:
        """Auto-cleared with a set that is not the truth. The dangerous one."""
        return self.cleared and not self.set_is_truth

    @property
    def ambiguous_correct(self) -> bool:
        """Refused to auto-clear, but surfaced the true set for review."""
        return (not self.cleared) and self.solvable and self.set_is_truth

    @property
    def correct_abstention(self) -> bool:
        return (not self.solvable) and (not self.cleared)

    @property
    def precision(self) -> float:
        if not self.matched_ids:
            return 0.0
        return len(self.matched_ids & self.truth_ids) / len(self.matched_ids)

    @property
    def recall(self) -> float:
        if not self.truth_ids:
            return 0.0
        return len(self.matched_ids & self.truth_ids) / len(self.truth_ids)


def _txn(txn_id: str, amount_cents: int, hours: float, ref: str, memo: str,
         source: SourceType = SourceType.GATEWAY) -> NormalizedTxn:
    return NormalizedTxn(
        source=source,
        source_txn_id=txn_id,
        ref_id_canonical=ref,
        amount_cents=amount_cents,
        currency="INR",
        timestamp_utc=BASE_TIME + timedelta(hours=hours),
        tz_confidence=TzConfidence.HIGH,
        memo_raw=memo,
        memo_normalized=memo.lower(),
    )


def _add_mirrors(
    members: list[NormalizedTxn],
    tag: str,
    rng: random.Random,
) -> list[NormalizedTxn]:
    """
    Produce the SAME payments as they appear in a second feed.

    Real reconciliation is inherently multi-source: a gateway payment also
    lands as an ERP ledger entry, and matching across the two is the core of
    the job. Modelling it matters here for two reasons.

    First, it is the only way `cross_source_amount_peer` is exercised at all
    — with a single-source pool that linkage signal is dead code, which made
    the previous ref_missing / ref_truncated zeros unrepresentative.

    Second, it is genuinely adversarial. A mirror carries the same amount as
    its original, so substituting one for the other leaves the arithmetic
    untouched. The engine has to pick the real member on non-arithmetic
    grounds — which is exactly the capability being claimed. Mirrors
    deliberately do NOT carry the settlement reference; the original does.
    """
    return [
        _txn(
            f"{m.source_txn_id}_MIRROR", m.amount_cents,
            (m.timestamp_utc - BASE_TIME).total_seconds() / 3600 + rng.uniform(0, 4),
            ref=f"ERP{tag}_{m.source_txn_id}",
            memo=f"Ledger entry for {m.source_txn_id}",
            source=SourceType.ERP,
        )
        for m in members
    ]


def _net_from_gross(gross_cents: int) -> int:
    """Apply the default rate card forward, mirroring how the orchestrator
    reverses it (2% gateway + 1% withholding)."""
    gw = round(gross_cents * DEFAULT_RATE_CARD.gateway_fee_bps / 10_000)
    tax = round(gross_cents * DEFAULT_RATE_CARD.tax_withholding_bps / 10_000)
    return gross_cents - gw - tax - DEFAULT_RATE_CARD.flat_fee_cents


def build_scenario(idx: int, family: str, solvable: bool, density: str,
                   rng: random.Random, declare_source: bool = False) -> Scenario:
    pool_size = DENSITIES[density]
    subset_size = rng.choice([3, 4, 5]) if family != "large_subset" else rng.choice([10, 15])
    subset_size = min(subset_size, max(3, pool_size // 6))

    candidates: list[NormalizedTxn] = []
    truth: list[NormalizedTxn] = []

    # True members carry a shared settlement reference, as a real settlement
    # batch does. This is the non-arithmetic signal the tiebreak exists to
    # exploit when the arithmetic alone is degenerate.
    settle_ref = f"STL{idx:04d}"

    for i in range(subset_size):
        amt = rng.randint(5_000, 400_000)
        hours = rng.uniform(0, 110) if family == "wide_spread" else rng.uniform(0, 60)
        truth.append(_txn(
            f"S{idx}_TRUE_{i}", amt, hours,
            ref=f"{settle_ref}_LEG{i}",
            memo=f"Settlement {settle_ref} leg {i}",
        ))

    gross = sum(t.amount_cents for t in truth)

    for i in range(pool_size - subset_size):
        candidates.append(_txn(
            f"S{idx}_N_{i}", rng.randint(5_000, 400_000), rng.uniform(0, 110),
            ref=f"NOISE{idx}_{i}",
            memo=f"Unrelated payment {i}",
        ))

    if family == "near_collision":
        for k, delta in enumerate((-7, -3, 3, 7)):
            a = gross // 2
            b = gross - a + delta
            candidates.append(_txn(f"S{idx}_NEAR_{k}A", a, rng.uniform(0, 60), f"NEAR{idx}_{k}A", "decoy"))
            candidates.append(_txn(f"S{idx}_NEAR_{k}B", b, rng.uniform(0, 60), f"NEAR{idx}_{k}B", "decoy"))

    elif family == "exact_collision":
        a, b = gross // 3, gross // 3
        c = gross - a - b
        for name, amt in (("A", a), ("B", b), ("C", c)):
            candidates.append(_txn(f"S{idx}_DECOY_{name}", amt, rng.uniform(0, 60), f"DEC{idx}_{name}", "decoy"))

    elif family == "duplicate_amounts":
        dup = truth[0].amount_cents
        for k in range(12):
            candidates.append(_txn(f"S{idx}_DUP_{k}", dup, rng.uniform(0, 110), f"DUP{idx}_{k}", "duplicate amount"))

    elif family == "missing_leg":
        dropped = truth.pop()
        candidates = [c for c in candidates if c.source_txn_id != dropped.source_txn_id]

    elif family == "out_of_window":
        truth[-1].timestamp_utc = BASE_TIME - timedelta(days=45)

    elif family == "ref_missing":
        # No reference at all on the true legs. Linkage has nothing to anchor
        # or cluster on and must fall back to weaker signal or degrade to a
        # no-op — either is acceptable, silently dropping them is not.
        for t in truth:
            t.ref_id_canonical = ""

    elif family == "ref_truncated":
        # Field-length truncation is a real and common corruption: the
        # identifying tail of the reference is exactly what gets cut.
        for i, t in enumerate(truth):
            t.ref_id_canonical = "ST"

    elif family == "ref_collision":
        # Unrelated traffic carrying the settlement's own identifier token —
        # the false-anchor case. These must not drag the solver into
        # clearing a wrong set.
        for k in range(20):
            candidates.append(_txn(
                f"S{idx}_COLLIDE_{k}", rng.randint(5_000, 400_000), rng.uniform(0, 110),
                ref=f"{settle_ref}_UNRELATED{k}", memo="unrelated traffic sharing the ref",
            ))

    elif family == "ref_partial":
        # Only the first leg keeps the settlement reference.
        for t in truth[1:]:
            t.ref_id_canonical = f"ORPHAN{idx}_{t.source_txn_id}"

    candidates.extend(truth)

    # Multi-source reality. Every true member is mirrored into the ERP feed,
    # and a minority of noise is too — if only true members had mirrors, the
    # cross-source signal would be a giveaway rather than a signal.
    candidates.extend(_add_mirrors(truth, f"T{idx}", rng))
    noise_only = [c for c in candidates
                  if c.source is SourceType.GATEWAY and "_N_" in c.source_txn_id]
    mirrored_noise = rng.sample(noise_only, min(len(noise_only), max(1, len(noise_only) // 3)))
    candidates.extend(_add_mirrors(mirrored_noise, f"N{idx}", rng))

    rng.shuffle(candidates)

    batch = SettlementBatch(
        batch_id=f"BENCH_{idx:04d}_{family}_{density}",
        net_amount_cents=_net_from_gross(gross),
        currency="INR",
        settled_at_utc=BASE_TIME + timedelta(hours=120),
        # True members are always GATEWAY records; the ERP entries are
        # mirrors. Declaring that is the configuration a production system
        # has — you know which ledger you are reconciling. The flag exists so
        # the benefit of the declaration can be MEASURED rather than assumed.
        member_source=SourceType.GATEWAY if declare_source else None,
    )

    truth_ids = {t.source_txn_id for t in truth}
    if family == "missing_leg":
        # the target still includes the dropped leg, so this is what SHOULD
        # have matched but provably cannot
        truth_ids.add(f"S{idx}_TRUE_{subset_size - 1}")

    return Scenario(
        scenario_id=batch.batch_id, family=family, density=density, solvable=solvable,
        batch=batch, candidates=candidates, truth_ids=truth_ids,
        pool_size=len(candidates), subset_size=subset_size,
    )


def run_scenario(sc: Scenario) -> Result:
    t0 = time.perf_counter()
    report = reconcile_batch(
        sc.batch, sc.candidates,
        subset_config=SubsetSumConfig(
            tolerance_cents=10, solver_time_limit_s=10.0, ambiguity_probe_limit=2,
            # One worker, so a published figure can be reproduced.
            #
            # CP-SAT's parallel search is not deterministic: with several
            # workers, whichever finds an optimal solution first wins, and on
            # a scenario where more than one subset is genuinely valid the
            # answer differs between runs. Three of these 180 scenarios are
            # like that - ref_collision, ref_missing, out_of_window - and they
            # flipped run to run, moving ECE between 0.070 and 0.086. Every
            # calibration figure ever quoted in the docs came from whichever
            # run happened to be watched.
            #
            # The engine is not wrong on those three: it reports 0.19-0.54
            # confidence, which is what "the data does not determine this"
            # should look like. But a measurement harness that cannot
            # reproduce its own number cannot back a claim made from it.
            #
            # Production keeps the default worker count. Speed matters there
            # and determinism does not, because the low confidence already
            # tells a reviewer the choice was not evidence-led.
            num_search_workers=1,
        ),
        settlement_window_days=5,
    )
    elapsed = time.perf_counter() - t0
    return Result(
        scenario_id=sc.scenario_id, family=sc.family, density=sc.density,
        solvable=sc.solvable, pool_size=sc.pool_size, subset_size=sc.subset_size,
        cleared=report.match_result.cleared,
        ambiguous=report.match_result.ambiguous,
        method=report.match_result.method.value,
        confidence=report.match_result.confidence,
        matched_ids=set(report.match_result.matched_txn_ids),
        truth_ids=sc.truth_ids, elapsed_s=elapsed,
        exception_count=len(report.exceptions),
    )


def _pct(n, d):
    return round(100.0 * n / d, 2) if d else 0.0


def summarise(results: list[Result]) -> dict:
    solvable = [r for r in results if r.solvable]
    unsolvable = [r for r in results if not r.solvable]
    false_clears = [r for r in results if r.false_clear]
    latencies = sorted(r.elapsed_s for r in results)

    by_density = {}
    for d in DENSITIES:
        rs = [r for r in results if r.density == d and r.solvable]
        if not rs:
            continue
        by_density[d] = {
            "pool_size": DENSITIES[d],
            "solvable_scenarios": len(rs),
            "auto_cleared_correct": sum(1 for r in rs if r.auto_cleared_correct),
            "auto_cleared_correct_pct": _pct(sum(1 for r in rs if r.auto_cleared_correct), len(rs)),
            "ambiguous_correct": sum(1 for r in rs if r.ambiguous_correct),
            "truth_identified_pct": _pct(sum(1 for r in rs if r.set_is_truth), len(rs)),
            "false_clears": sum(1 for r in rs if r.false_clear),
        }

    by_family = {}
    for fam, _, _ in FAMILIES:
        rs = [r for r in results if r.family == fam]
        if not rs:
            continue
        solv = [r for r in rs if r.solvable]
        by_family[fam] = {
            "scenarios": len(rs),
            "solvable": rs[0].solvable,
            "truth_identified_pct": _pct(sum(1 for r in solv if r.set_is_truth), len(solv)) if solv else None,
            "auto_cleared_correct": sum(1 for r in rs if r.auto_cleared_correct),
            "false_clears": sum(1 for r in rs if r.false_clear),
            "correct_abstentions": sum(1 for r in rs if r.correct_abstention),
            "median_latency_s": round(statistics.median([r.elapsed_s for r in rs]), 3),
        }

    return {
        "scenarios_total": len(results),
        "solvable_scenarios": len(solvable),
        "unsolvable_scenarios": len(unsolvable),
        "headline": {
            "false_clear_count": len(false_clears),
            "false_clear_pct": _pct(len(false_clears), len(results)),
            "auto_cleared_correct_pct": _pct(sum(1 for r in solvable if r.auto_cleared_correct), len(solvable)),
            "truth_identified_pct": _pct(sum(1 for r in solvable if r.set_is_truth), len(solvable)),
            "correct_abstention_pct": _pct(sum(1 for r in unsolvable if r.correct_abstention), len(unsolvable)),
        },
        "partial_credit": {
            "mean_precision": round(statistics.mean([r.precision for r in solvable]), 4) if solvable else 0.0,
            "mean_recall": round(statistics.mean([r.recall for r in solvable]), 4) if solvable else 0.0,
        },
        "throughput": {
            "median_latency_s": round(statistics.median(latencies), 3),
            "p95_latency_s": round(latencies[max(0, int(len(latencies) * 0.95) - 1)], 3) if latencies else 0.0,
            "max_latency_s": round(max(latencies), 3) if latencies else 0.0,
            "total_txns_processed": sum(r.pool_size for r in results),
        },
        "by_density": by_density,
        "by_family": by_family,
        "false_clear_detail": [
            {
                "scenario": r.scenario_id, "family": r.family, "density": r.density,
                "matched_not_truth": sorted(r.matched_ids - r.truth_ids)[:6],
                "truth_not_matched": sorted(r.truth_ids - r.matched_ids)[:6],
            }
            for r in false_clears[:10]
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", type=int, default=120)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", type=str, default="")
    ap.add_argument("--declare-source", action="store_true",
                    help="Tell the engine which feed the settlement members live in.")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    densities = list(DENSITIES)
    scenarios = []
    for i in range(args.scenarios):
        family, solvable, _ = FAMILIES[i % len(FAMILIES)]
        density = densities[(i // len(FAMILIES)) % len(densities)]
        scenarios.append(build_scenario(i, family, solvable, density, rng,
                                        declare_source=args.declare_source))

    print(f"Running {len(scenarios)} scenarios "
          f"({len(FAMILIES)} families x {len(densities)} pool densities)…\n")
    results = []
    for i, sc in enumerate(scenarios, 1):
        results.append(run_scenario(sc))
        if i % 20 == 0:
            print(f"  {i}/{len(scenarios)}")

    s = summarise(results)
    h = s["headline"]

    print("\n" + "=" * 72)
    print("RECONCILIATION ACCURACY BENCHMARK")
    print("=" * 72)
    print(f"Scenarios              : {s['scenarios_total']} "
          f"({s['solvable_scenarios']} solvable, {s['unsolvable_scenarios']} unsolvable by design)")
    print("-" * 72)
    print(f"FALSE CLEARS           : {h['false_clear_count']}  ({h['false_clear_pct']}%)   <- cleared a WRONG set")
    print(f"Auto-cleared & correct : {h['auto_cleared_correct_pct']}%   (of solvable)")
    print(f"Truth identified       : {h['truth_identified_pct']}%   (of solvable; incl. flagged-for-review)")
    print(f"Correct abstentions    : {h['correct_abstention_pct']}%   (of unsolvable)")
    print(f"Mean precision / recall: {s['partial_credit']['mean_precision']} / {s['partial_credit']['mean_recall']}")
    print("-" * 72)
    t = s["throughput"]
    print(f"Latency median/p95/max : {t['median_latency_s']}s / {t['p95_latency_s']}s / {t['max_latency_s']}s")
    print(f"Transactions processed : {t['total_txns_processed']:,}")

    print("\n" + "-" * 72)
    print("BY POOL DENSITY  (how many subsets compete for the same target)")
    print(f"{'density':<10} {'pool':>6} {'n':>5} {'auto-clear ok':>14} {'truth found':>13} {'false':>7}")
    print("-" * 72)
    for d, v in s["by_density"].items():
        print(f"{d:<10} {v['pool_size']:>6} {v['solvable_scenarios']:>5} "
              f"{str(v['auto_cleared_correct_pct'])+'%':>14} {str(v['truth_identified_pct'])+'%':>13} "
              f"{v['false_clears']:>7}")

    print("\n" + "-" * 72)
    print(f"{'family':<20} {'n':>4} {'truth found':>13} {'false':>7} {'abstain':>8} {'med s':>7}")
    print("-" * 72)
    for fam, v in s["by_family"].items():
        tf = "n/a" if v["truth_identified_pct"] is None else f"{v['truth_identified_pct']}%"
        print(f"{fam:<20} {v['scenarios']:>4} {tf:>13} {v['false_clears']:>7} "
              f"{v['correct_abstentions']:>8} {v['median_latency_s']:>7}")

    if s["false_clear_detail"]:
        print("\nFALSE CLEARS (auto-cleared a set that is not the truth):")
        for d in s["false_clear_detail"]:
            print(f"  {d['scenario']}")
            print(f"    matched but not true : {d['matched_not_truth']}")
            print(f"    true but not matched : {d['truth_not_matched']}")
    print("=" * 72)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
