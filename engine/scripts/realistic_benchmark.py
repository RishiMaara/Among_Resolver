"""
Accuracy under REAL-DATA conditions, measured as a sensitivity sweep.

WHY THIS EXISTS
---------------
Every accuracy number this project had was measured on synthetic data.
`benchmark.py` generates its own scenarios; the ReconRiver corpus is
third-party but its manifest says so outright — "This dataset is entirely
synthetic ... All exception percentages and distributions are documented
demonstration assumptions; they are not measurements of real payment
behavior." Independent synthetic is better than self-generated, because the
generator's blind spots are not ours. It is still not evidence about
production data.

Real settlement data with ground truth is not publicly obtainable: it is
simultaneously personal data and commercially sensitive, so no bank or
processor publishes it. Waiting for it is not a plan. The answerable question
is therefore not "what is the accuracy on real data" but:

    Which properties of real data actually hurt this engine, and how much?

That converts an unknowable into a measured curve. Each knob below is a
documented way real payment feeds differ from generated ones. The sweep
varies ONE at a time from the synthetic baseline and reports the damage, so
a number can be attached to each hazard instead of an assurance.

THE FOUR BUGS THIS APPROACH FOUND
---------------------------------
Written to measure degradation, it first found defects — all four invisible
to both synthetic corpora, all four now fixed and covered by
tests/test_real_data_hazards.py:

  * Sequential settlement ids (SETTLE-1 vs SETTLE-10) produced FALSE
    ANCHORS, the highest-confidence wrong answer the engine can give.
  * Transaction ids colliding across feeds leaked linkage signals between
    unrelated records.
  * A declared member feed containing no linked record crashed with
    AttributeError instead of reporting the finding.
  * Linkage ran twice per settlement, tokenising the whole pool for a log
    line whose result was discarded.

That is the actual argument for this file. Synthetic data does not just
inflate scores, it hides defects, and the defects it hides are the ones that
only real-world shapes provoke.

WHAT IS MODELLED, AND ON WHAT BASIS
-----------------------------------
amount distribution   Generated amounts are uniform over a wide range, so
                      collisions are rare by construction: ~395,000 distinct
                      paise values means two payments rarely share one. Real
                      payment amounts are nothing like uniform. They cluster
                      on psychological price points (99, 199, 499, 999),
                      round figures (500, 1000, 5000), and a lognormal tail,
                      and leading digits follow Benford. The consequence is
                      specific and severe: fewer distinct values means far
                      more subsets share a sum, which is precisely the
                      degeneracy subset-sum cannot resolve.

reference coverage    Generated members all carry the settlement reference.
                      In practice coverage is partial — some legs are booked
                      manually, some feeds drop the field, some processors
                      only populate it on capture and not on refund.

reference truncation  Bank narration fields are fixed width. NEFT/RTGS UTRs
                      run 16 characters, UPI RRNs 12, and SWIFT MT940 :86:
                      wraps at 65. The identifying tail is what gets cut.

id collision          `source_txn_id` is unique per feed, not globally. Both
                      reference corpora prefix ids by feed (SYNTH-INT-,
                      SYNTH-PROC-) so ids never collide. Real feeds mint
                      independent sequences that overlap constantly.

settlement id style   Both corpora use fixed-width zero-padded ids, where no
                      id is a prefix of another. Real ids are frequently
                      sequential and unpadded.

fee variance          The rate card is applied as an exact reversible
                      formula. Real deductions carry per-transaction rounding,
                      tiered rates by instrument, and occasional adjustments,
                      so a target reconstructed from a rate card is slightly
                      wrong — and subset-sum is an EXACT method.

WHAT THIS IS NOT
----------------
Not a claim of production accuracy. It is a claim about sensitivity: if a
real feed has these properties at these levels, this is what happens. The
honest headline is the false-clear rate, which must stay at zero throughout,
because the failure that matters is not a missed match but a confident wrong
one.

Run:
    python scripts/realistic_benchmark.py                 # profiles + sweep
    python scripts/realistic_benchmark.py --scenarios 60
    python scripts/realistic_benchmark.py --profiles-only
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence  # noqa: E402
from fee_decomposition import DEFAULT_RATE_CARD  # noqa: E402

import benchmark as bm  # noqa: E402  (reuse Scenario, Result, run_scenario, scoring)


# ── the realism knobs ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class Realism:
    """One point in real-data space. The baseline reproduces benchmark.py."""
    name: str
    amount_model: str = "uniform"      # "uniform" | "clustered"
    ref_coverage: float = 1.0          # fraction of members carrying the ref
    ref_truncate: int = 0              # truncate refs to N chars (0 = off)
    id_collision: float = 0.0          # fraction of feeds reusing bare ids
    padded_settlement_id: bool = True  # False -> sequential, unpadded
    fee_variance_bps: float = 0.0      # per-txn deduction noise
    mirror_rate: float = 1.0           # fraction of members mirrored to ERP
    late_leg_rate: float = 0.0         # fraction of members outside the window


SYNTHETIC = Realism("synthetic_baseline")

# Levels chosen to be defensible middle-of-the-road, not worst case.
REALISTIC = Realism(
    "realistic",
    amount_model="clustered",
    ref_coverage=0.70,
    ref_truncate=16,          # NEFT/RTGS UTR width
    id_collision=0.30,
    padded_settlement_id=False,
    fee_variance_bps=1.5,
    mirror_rate=0.85,
    late_leg_rate=0.08,
)

HARSH = Realism(
    "harsh",
    amount_model="clustered",
    ref_coverage=0.35,
    ref_truncate=12,          # UPI RRN width
    id_collision=0.60,
    padded_settlement_id=False,
    fee_variance_bps=4.0,
    mirror_rate=1.0,
    late_leg_rate=0.15,
)


# ── realistic amounts ──────────────────────────────────────────────────────
#
# Price points and round figures carry most of the mass in consumer payments.
# The exact weights are an assumption; the SHAPE — a small set of values
# taking a large share, plus a lognormal tail — is not, and the shape is what
# drives collision rate, which is what the solver actually cares about.
PRICE_POINTS_INR = [
    99, 149, 199, 249, 299, 349, 399, 449, 499, 599, 699, 799, 899, 999,
    1199, 1299, 1499, 1799, 1999, 2499, 2999, 3499, 3999, 4999, 5999, 9999,
]
ROUND_INR = [100, 200, 250, 500, 750, 1000, 1500, 2000, 2500, 5000, 10000]


def realistic_amount_cents(rng: random.Random) -> int:
    r = rng.random()
    if r < 0.45:
        return rng.choice(PRICE_POINTS_INR) * 100
    if r < 0.65:
        return rng.choice(ROUND_INR) * 100
    # Lognormal tail. mu/sigma give a median near 900 INR with a long right
    # tail, which is the shape retail payment values actually take.
    val = rng.lognormvariate(mu=math.log(900), sigma=1.05)
    return max(5_000, min(400_000, int(round(val)) * 100))


def uniform_amount_cents(rng: random.Random) -> int:
    return rng.randint(5_000, 400_000)


# ── amounts sampled from REAL payment records ─────────────────────────────
#
# The two models above are both assumptions: uniform is obviously wrong, and
# the clustered one is my guess at what retail looks like. This third mode
# does not guess. It samples from actual payment amounts fetched from
# USAspending.gov — real US federal contract and grant disbursements, public
# data, no personal information — via scripts/fetch_real_payments.py.
#
# Measured against 2,000 of those records, the three models differ in ways
# that matter to a subset-sum solver:
#
#                       distinct   in a collision   leading digit 1/2/3
#   REAL                  96.1%          6.0%        34.5 / 18.4 / 11.4
#   clustered (my model)  30.9%         74.4%        31.5 / 17.8 / 12.5
#   uniform (original)   100.0%          0.1%        28.3 / 28.1 / 26.6
#
#   Benford's law expects            30.1 / 17.6 / 12.5
#
# Two things fall out. Uniform amounts violate Benford outright — a flat
# leading-digit distribution does not occur in real financial data, so the
# original generator was not merely easy, it was the wrong shape. And my
# clustered model OVER-corrected: it assumes 74% of amounts collide where
# this real corpus shows 6%.
#
# The honest caveat is that federal awards are institutional, not retail. A
# payment gateway's traffic really does cluster on price points in a way
# bespoke contract values do not, so the truth for this engine's actual
# workload sits between the two. Sampling real amounts is not a claim that
# gateway traffic looks like federal spending; it is a way to stop the
# hardest number in the benchmark from being one I made up.
_REAL_AMOUNTS: list[int] | None = None


def load_real_amounts() -> list[int]:
    global _REAL_AMOUNTS
    if _REAL_AMOUNTS is not None:
        return _REAL_AMOUNTS
    path = Path(__file__).resolve().parents[1] / "data" / "real" / "usaspending_2024.json"
    if not path.exists():
        raise SystemExit(
            f"No real payment corpus at {path}. "
            f"Run: python scripts/fetch_real_payments.py"
        )
    recs = json.loads(path.read_text(encoding="utf-8"))
    amounts = [
        round(r["Award Amount"] * 100)
        for r in recs
        if isinstance(r.get("Award Amount"), (int, float)) and r["Award Amount"] > 0
    ]
    # Scale into the same order of magnitude as a settlement's members so the
    # solver faces a comparable problem; the SHAPE of the distribution is what
    # is being borrowed, not the absolute size.
    lo, hi = 5_000, 400_000
    smallest, largest = min(amounts), max(amounts)
    span = max(1, largest - smallest)
    _REAL_AMOUNTS = [
        lo + round((a - smallest) / span * (hi - lo)) for a in amounts
    ]
    return _REAL_AMOUNTS


def real_amount_cents(rng: random.Random) -> int:
    pool = load_real_amounts()
    return pool[rng.randrange(len(pool))]


def _amount(realism: Realism, rng: random.Random) -> int:
    if realism.amount_model == "clustered":
        return realistic_amount_cents(rng)
    if realism.amount_model == "real":
        return real_amount_cents(rng)
    return uniform_amount_cents(rng)


def collision_stats(amounts: list[int]) -> dict:
    """How degenerate is this amount distribution?"""
    n = len(amounts)
    distinct = len(set(amounts))
    counts: dict[int, int] = {}
    for a in amounts:
        counts[a] = counts.get(a, 0) + 1
    shared = sum(c for c in counts.values() if c > 1)
    return {
        "n": n,
        "distinct": distinct,
        "distinct_pct": round(100.0 * distinct / n, 2) if n else 0.0,
        "in_a_collision_pct": round(100.0 * shared / n, 2) if n else 0.0,
        "max_repeat": max(counts.values()) if counts else 0,
    }


# ── scenario construction under a realism profile ──────────────────────────
def _mirror(t: NormalizedTxn, tag: str, source: SourceType) -> NormalizedTxn:
    return NormalizedTxn(
        source=source, source_txn_id=f"{tag}_{t.source_txn_id}",
        ref_id_canonical=t.ref_id_canonical, amount_cents=t.amount_cents,
        currency=t.currency, timestamp_utc=t.timestamp_utc,
        tz_confidence=TzConfidence.HIGH, memo_normalized=t.memo_normalized,
    )


def build_scenario(idx: int, family: str, solvable: bool, density: str,
                   rng: random.Random, realism: Realism,
                   declare_source: bool = True) -> bm.Scenario:
    pool_size = bm.DENSITIES[density]
    subset_size = rng.choice([3, 4, 5]) if family != "large_subset" else rng.choice([10, 15])
    subset_size = min(subset_size, max(3, pool_size // 6))

    # Settlement id style. Padded ids can never be a prefix of one another;
    # sequential unpadded ids routinely are, which is what makes containment
    # anchoring dangerous.
    # Both styles are long enough to anchor (>= MIN_CANONICAL_ANCHOR_LEN).
    # The variable under test is PREFIX COLLISION, not length: "STL20261" is
    # a prefix of "STL202610", while "STL0001" can never be a prefix of
    # "STL0010". Making the unpadded form short as well would confound the
    # two and measure the wrong thing.
    settle_ref = (f"STL2026{idx:04d}" if realism.padded_settlement_id
                  else f"STL2026{idx}")

    truth: list[NormalizedTxn] = []
    for i in range(subset_size):
        amt = _amount(realism, rng)
        hours = rng.uniform(0, 110) if family == "wide_spread" else rng.uniform(0, 60)
        carries_ref = rng.random() < realism.ref_coverage
        ref = f"{settle_ref}_LEG{i}" if carries_ref else f"ORPH{idx}_{i}"
        if realism.ref_truncate:
            ref = ref[: realism.ref_truncate]
        truth.append(bm._txn(
            f"S{idx}_TRUE_{i}", amt, hours, ref=ref,
            memo=f"Settlement {settle_ref} leg {i}",
        ))

    # A late leg lands outside the settlement window. The batch is still
    # solvable in principle, but the member is not in the pool the solver
    # sees -- exactly what a T+3 bank leg does in production.
    for t in truth:
        if rng.random() < realism.late_leg_rate:
            t.timestamp_utc = bm.BASE_TIME - timedelta(days=40)

    gross = sum(t.amount_cents for t in truth)

    candidates: list[NormalizedTxn] = []
    for i in range(pool_size - subset_size):
        candidates.append(bm._txn(
            f"S{idx}_N_{i}", _amount(realism, rng), rng.uniform(0, 110),
            ref=f"NOISE{idx}_{i}", memo=f"Unrelated payment {i}",
        ))

    # Family-specific hazards, mirroring benchmark.py so the two are
    # comparable family by family.
    if family == "near_collision":
        for k, delta in enumerate((-7, -3, 3, 7)):
            a = gross // 2
            candidates.append(bm._txn(f"S{idx}_NEAR_{k}A", a, rng.uniform(0, 60), f"NEAR{idx}_{k}A", "decoy"))
            candidates.append(bm._txn(f"S{idx}_NEAR_{k}B", gross - a + delta, rng.uniform(0, 60), f"NEAR{idx}_{k}B", "decoy"))
    elif family == "exact_collision":
        a = b = gross // 3
        for name, amt in (("A", a), ("B", b), ("C", gross - a - b)):
            candidates.append(bm._txn(f"S{idx}_DECOY_{name}", amt, rng.uniform(0, 60), f"DEC{idx}_{name}", "decoy"))
    elif family == "duplicate_amounts":
        dup = truth[0].amount_cents
        for k in range(12):
            candidates.append(bm._txn(f"S{idx}_DUP_{k}", dup, rng.uniform(0, 110), f"DUP{idx}_{k}", "duplicate amount"))
    elif family == "missing_leg":
        dropped = truth.pop()
        candidates = [c for c in candidates if c.source_txn_id != dropped.source_txn_id]
    elif family == "out_of_window":
        truth[-1].timestamp_utc = bm.BASE_TIME - timedelta(days=45)
    elif family == "ref_missing":
        for t in truth:
            t.ref_id_canonical = ""
    elif family == "ref_truncated":
        for t in truth:
            t.ref_id_canonical = "ST"
    elif family == "ref_collision":
        for k in range(20):
            candidates.append(bm._txn(
                f"S{idx}_COLLIDE_{k}", _amount(realism, rng), rng.uniform(0, 110),
                ref=f"{settle_ref}_UNRELATED{k}", memo="unrelated traffic sharing the ref"))
    elif family == "ref_partial":
        for t in truth[1:]:
            t.ref_id_canonical = f"ORPHAN{idx}_{t.source_txn_id}"

    # A NEIGHBOURING settlement's traffic. This is what makes unpadded
    # sequential ids dangerous: STL1's members are a prefix-match for STL10's
    # reference, so the engine can anchor another batch's records.
    if not realism.padded_settlement_id:
        for k in range(6):
            candidates.append(bm._txn(
                f"S{idx}_NEIGH_{k}", _amount(realism, rng), rng.uniform(0, 110),
                ref=f"STL2026{idx}{k}_LEG{k}", memo="neighbouring settlement"))

    candidates.extend(truth)

    # Cross-feed mirrors.
    to_mirror = [t for t in truth if rng.random() < realism.mirror_rate]
    candidates.extend(_mirror(t, f"T{idx}", SourceType.ERP) for t in to_mirror)
    noise_only = [c for c in candidates
                  if c.source is SourceType.GATEWAY and "_N_" in c.source_txn_id]
    if noise_only:
        sample = rng.sample(noise_only, max(1, len(noise_only) // 3))
        candidates.extend(_mirror(t, f"N{idx}", SourceType.ERP) for t in sample)

    # Cross-feed id collision: strip the feed prefix from some ERP records so
    # their bare ids coincide with gateway ids, as independent sequences do.
    if realism.id_collision:
        gateway_ids = [c.source_txn_id for c in candidates
                       if c.source is SourceType.GATEWAY]
        for c in candidates:
            if c.source is SourceType.ERP and rng.random() < realism.id_collision:
                c.source_txn_id = rng.choice(gateway_ids)

    rng.shuffle(candidates)

    # Fee variance: the processor's real deductions differ slightly from the
    # rate card, so the reconstructed gross target is a few paise out. An
    # EXACT solver notices.
    net = bm._net_from_gross(gross)
    if realism.fee_variance_bps:
        drift = round(gross * rng.uniform(-realism.fee_variance_bps,
                                          realism.fee_variance_bps) / 10_000)
        net += drift

    batch = SettlementBatch(
        batch_id=settle_ref,
        net_amount_cents=net, currency="INR",
        settled_at_utc=bm.BASE_TIME + timedelta(hours=120),
        member_source=SourceType.GATEWAY if declare_source else None,
    )

    truth_ids = {t.source_txn_id for t in truth}
    if family == "missing_leg":
        solvable = False
    if family == "out_of_window":
        solvable = False

    return bm.Scenario(
        scenario_id=f"{realism.name}_{idx:04d}_{family}_{density}",
        family=family, density=density, solvable=solvable, batch=batch,
        candidates=candidates, truth_ids=truth_ids,
        pool_size=len(candidates), subset_size=len(truth),
    )


# ── running ────────────────────────────────────────────────────────────────
def run_profile(realism: Realism, n_scenarios: int, seed: int,
                declare_source: bool = True) -> list[bm.Result]:
    rng = random.Random(seed)
    families = [f for f, _, _ in bm.FAMILIES]
    densities = list(bm.DENSITIES)
    results = []
    for i in range(n_scenarios):
        fam = families[i % len(families)]
        dens = densities[(i // len(families)) % len(densities)]
        sc = build_scenario(i, fam, True, dens, rng, realism, declare_source)
        results.append(bm.run_scenario(sc))
    return results


def headline(results: list[bm.Result]) -> dict:
    solvable = [r for r in results if r.solvable]
    return {
        "scenarios": len(results),
        "solvable": len(solvable),
        "auto_clear_correct_pct": bm._pct(
            sum(1 for r in solvable if r.auto_cleared_correct), len(solvable)),
        "truth_identified_pct": bm._pct(
            sum(1 for r in solvable if r.set_is_truth), len(solvable)),
        "false_clears": sum(1 for r in results if r.false_clear),
        "false_clear_pct": bm._pct(sum(1 for r in results if r.false_clear), len(results)),
        "median_latency_s": round(statistics.median(r.elapsed_s for r in results), 3),
    }


def _row(label, h):
    return (f"  {label:<34} {h['auto_clear_correct_pct']:>7.1f}% "
            f"{h['truth_identified_pct']:>9.1f}% {h['false_clears']:>7} "
            f"{h['median_latency_s']:>8.3f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenarios", type=int, default=48)
    ap.add_argument("--seed", type=int, default=20260901)
    ap.add_argument("--profiles-only", action="store_true")
    ap.add_argument("--json", help="write full results here")
    args = ap.parse_args()

    out: dict = {}

    # Amount-distribution evidence, before any reconciliation runs.
    rng = random.Random(args.seed)
    uni = [uniform_amount_cents(rng) for _ in range(20_000)]
    clu = [realistic_amount_cents(rng) for _ in range(20_000)]
    out["amount_distribution"] = {"uniform": collision_stats(uni),
                                  "clustered": collision_stats(clu)}

    print("=" * 78)
    print("AMOUNT DISTRIBUTION — why generated pools are easier than real ones")
    print("=" * 78)
    for k, st in out["amount_distribution"].items():
        print(f"  {k:<12} distinct {st['distinct_pct']:>6.2f}%   "
              f"in a collision {st['in_a_collision_pct']:>6.2f}%   "
              f"max repeat {st['max_repeat']}")
    print("  Fewer distinct values means more subsets share a sum. That is the")
    print("  degeneracy subset-sum cannot resolve, and it is the normal state")
    print("  of real payment data.")

    print()
    print("=" * 78)
    print(f"PROFILES  ({args.scenarios} scenarios each)")
    print("=" * 78)
    print(f"  {'profile':<34} {'auto-clear':>8} {'truth id':>10} "
          f"{'FALSE':>7} {'med s':>8}")
    profiles = [SYNTHETIC, REALISTIC, HARSH]
    out["profiles"] = {}
    for prof in profiles:
        res = run_profile(prof, args.scenarios, args.seed)
        h = headline(res)
        out["profiles"][prof.name] = {"knobs": prof.__dict__, **h}
        print(_row(prof.name, h))

    if not args.profiles_only:
        print()
        print("=" * 78)
        print("SENSITIVITY — one knob at a time, from the synthetic baseline")
        print("=" * 78)
        print(f"  {'knob':<34} {'auto-clear':>8} {'truth id':>10} "
              f"{'FALSE':>7} {'med s':>8}")
        sweeps = [
            ("amounts: clustered (modelled retail)", dict(amount_model="clustered")),
            ("amounts: REAL (USAspending)", dict(amount_model="real")),
            ("ref coverage 70%", dict(ref_coverage=0.70)),
            ("ref coverage 35%", dict(ref_coverage=0.35)),
            ("ref truncated to 16 (UTR)", dict(ref_truncate=16)),
            ("ref truncated to 12 (UPI RRN)", dict(ref_truncate=12)),
            ("txn ids collide across feeds 30%", dict(id_collision=0.30)),
            ("settlement ids unpadded", dict(padded_settlement_id=False)),
            ("fee variance +-1.5bps", dict(fee_variance_bps=1.5)),
            ("fee variance +-4bps", dict(fee_variance_bps=4.0)),
            ("late legs 8%", dict(late_leg_rate=0.08)),
        ]
        out["sensitivity"] = {}
        for label, kw in sweeps:
            prof = replace(SYNTHETIC, name=label, **kw)
            h = headline(run_profile(prof, args.scenarios, args.seed))
            out["sensitivity"][label] = h
            print(_row(label, h))

    print()
    print("=" * 78)
    print("The column that matters is FALSE. A drop in auto-clear is the engine")
    print("declining to guess; a false clear is it guessing wrong and saying so")
    print("confidently. Any non-zero value there is a defect, not a trade-off.")
    print("=" * 78)

    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2, default=str),
                                   encoding="utf-8")
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
