#!/usr/bin/env python3
"""
Close a finance-ops loop over a batch, and report what it could NOT close.

THE TASK
--------
Given a batch of settlements and the transaction pool they were paid from,
attempt every one, and produce the report a controller actually needs at
month end:

    how many closed, how many did not, and for each one that did not,
    what specifically stopped it.

The last part is the point. A reconciliation tool that reports 94% and hands
you a shrug for the other 6% has moved the work rather than done it — the
whole reason the 6% is left is that it is the hard part. Every unresolved
settlement here comes back with a reason, a direction, and an amount.

WHY THE FIXTURE CONTAINS PROBLEMS ON PURPOSE
---------------------------------------------
A generated batch where everything reconciles measures nothing. It is the
same failure as quoting one match: you learn that the happy path works, which
was never in doubt.

So `--generate` seeds hazards drawn from the ones this engine has actually
met in real feeds, at roughly the rate real feeds carry them:

    missing_member      a member never made it into the gateway export
    fee_drift           the declared deduction is wrong, so the target is wrong
    identical_amounts   several members share an amount; more than one subset
                        satisfies the total, so no set is identified
    out_of_window       a member settled outside the lookback
    unreferenced        nothing in the pool names this settlement at all

Each is recorded in the fixture's ground truth, so the report can be checked
against what was actually planted rather than against what the engine says.
The engine is never told which is which.

Run from engine/:
    python scripts/close_batch.py --generate
    python scripts/close_batch.py --corpus data/demo_week
    python scripts/close_batch.py --generate --json out.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

# A Windows console defaults to cp1252, which cannot encode the rupee sign, so
# printing an amount killed the run AFTER all the work was done -- the worst
# possible place to lose a report. Ask for UTF-8; if the stream will not take
# it, amounts fall back to "Rs".
try:
    sys.stdout.reconfigure(encoding="utf-8")
    RUPEE = "₹"
except Exception:
    RUPEE = "Rs "


import dateutil.parser as dp                       # noqa: E402

import file_agent                                  # noqa: E402
from fee_decomposition import FeeRateCard          # noqa: E402
from ingestion import normalize_batch              # noqa: E402
from pipeline import reconcile_settlement          # noqa: E402
from schema import NormalizedTxn, SettlementBatch, SourceType  # noqa: E402
from subset_sum import SubsetSumConfig             # noqa: E402

# Deductions are stated per settlement in this corpus, so the target is a fact
# and not a rate-card estimate. Nothing to add back on top.
NET_TO_NET = FeeRateCard(gateway_fee_bps=0, flat_fee_cents=0, tax_withholding_bps=0)

# Single worker so the batch report reproduces. Same reasoning as the
# benchmarks: a throughput and match-rate figure that changes between runs is
# not a figure. See docs/ARCHITECTURE.md, "Why the numbers used to move".
BATCH_CONFIG = SubsetSumConfig(
    tolerance_cents=5, solver_time_limit_s=10.0,
    ambiguity_probe_limit=2, probe_time_limit_s=4.0, num_search_workers=1,
)

HAZARDS = ("missing_member", "fee_drift", "identical_amounts",
           "out_of_window", "unreferenced")


@dataclass
class Outcome:
    settlement_id: str
    planted_hazard: str | None
    members_expected: int
    closed: bool = False
    exact: bool = False
    matched: set = field(default_factory=set)
    truth: set = field(default_factory=set)
    residual_cents: int = 0
    window_days: int = 5
    reason: str = ""
    elapsed_s: float = 0.0

    @property
    def false_clear(self) -> bool:
        return self.closed and not self.exact


# ── generating a batch worth measuring ────────────────────────────────────

def generate(out_dir: str, settlements: int, seed: int,
             hazard_every: int = 16) -> dict:
    """
    A week of settlements with hazards seeded at a realistic rate.

    Amounts come from a retail-shaped distribution — clustered on price points
    rather than uniform — because how often amounts repeat IS the difficulty
    knob for subset-sum, and a uniform draw makes every batch artificially easy.
    """
    rng = random.Random(seed)
    os.makedirs(out_dir, exist_ok=True)
    price_points = [199, 249, 299, 349, 499, 599, 799, 999, 1299, 1999, 2499]

    rows, stl_rows, truth = [], [], {}
    base = datetime(2026, 8, 17, tzinfo=timezone.utc)

    # One settlement in `hazard_every` carries a problem.
    #
    # The default is 1 in 16 -- about 6% -- because that is the order of
    # magnitude a referenced gateway feed actually carries; published
    # auto-match rates for well-referenced payment data sit in the 90s, and an
    # exception rate far above that describes a broken pipeline rather than a
    # normal week.
    #
    # It is a knob, and it has to be said out loud that it is: the match rate
    # of this batch is very nearly 1 minus this rate. Turning it down until the
    # headline reads well would be measuring the fixture, not the engine. The
    # figures that do not move with it are false clears and false alarms, and
    # those are the ones to read.
    hazard_at = {i: HAZARDS[(i // hazard_every) % len(HAZARDS)]
                 for i in range(settlements) if i % hazard_every == 3}

    for i in range(settlements):
        sid = f"STL2026{i:04d}"
        hazard = hazard_at.get(i)
        day = base + timedelta(days=i % 7, hours=6)
        n = rng.randint(6, 14)

        if hazard == "identical_amounts":
            # Several members at one price: more than one subset of the pool
            # satisfies the total, so no particular set is identified.
            amounts = [rng.choice(price_points)] * n
        else:
            amounts = [rng.choice(price_points) for _ in range(n)]

        members = []
        for k, amt in enumerate(amounts):
            ts = day + timedelta(minutes=7 * k)
            if hazard == "out_of_window" and k == 0:
                ts = day - timedelta(days=30)      # long outside any lookback
            members.append({
                "txn_id": f"pay_{i:04d}_{k:03d}",
                "amount": f"{amt}.00",
                "currency": "INR",
                "timestamp": ts.isoformat(),
                "status": "captured",
                # unreferenced: the pool holds the money but nothing names the
                # settlement, so linkage has nothing to anchor on.
                "ref_id": "" if hazard == "unreferenced" else sid,
                "payer_id": f"cust_{rng.randrange(9999):04d}",
                "memo": "Online order",
            })

        gross = sum(int(m["amount"].split(".")[0]) for m in members)
        deductions = round(gross * 0.0236, 2)      # 2% + 18% GST on the fee

        emitted = list(members)
        if hazard == "missing_member":
            # The money moved; the export did not carry it. Nothing in the pool
            # can make the total, and the engine must say so rather than find
            # some other subset that happens to fit.
            emitted = members[:-1]

        declared = deductions
        if hazard == "fee_drift":
            # The advice understates deductions, so the reconstructed gross
            # target is wrong and no honest subset ties out to it.
            declared = round(deductions + rng.uniform(40, 90), 2)

        rows += emitted
        stl_rows.append({
            "settlement_id": sid,
            "net_amount": f"{gross - deductions:.2f}",
            "settled_at": (day + timedelta(days=2)).date().isoformat(),
            "currency": "INR",
            "declared_deductions": f"{declared:.2f}",
        })
        truth[sid] = {
            "members": [m["txn_id"] for m in members],
            "emitted": [m["txn_id"] for m in emitted],
            "hazard": hazard,
        }

    # Some noise that belongs to nothing, because a real export contains
    # payments from outside the settlements you are closing.
    for j in range(settlements * 2):
        ts = base + timedelta(days=rng.randrange(7), minutes=rng.randrange(1440))
        rows.append({
            "txn_id": f"pay_noise_{j:04d}", "amount": f"{rng.choice(price_points)}.00",
            "currency": "INR", "timestamp": ts.isoformat(), "status": "captured",
            "ref_id": "", "payer_id": f"cust_{rng.randrange(9999):04d}",
            "memo": "Online order",
        })
    rng.shuffle(rows)

    with open(os.path.join(out_dir, "gateway.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(out_dir, "settlements.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(stl_rows[0].keys()))
        w.writeheader()
        w.writerows(stl_rows)
    with open(os.path.join(out_dir, "truth.json"), "w", encoding="utf-8") as f:
        json.dump(truth, f, indent=2)

    return {"records": len(rows), "settlements": len(stl_rows),
            "hazards": Counter(v["hazard"] for v in truth.values() if v["hazard"])}


# ── closing the loop ──────────────────────────────────────────────────────

def load(corpus: str) -> tuple[list[NormalizedTxn], list[dict], dict]:
    """Ingest through Agent 0, the same path a human upload takes."""
    with open(os.path.join(corpus, "gateway.csv"), "rb") as f:
        pool = normalize_batch(
            file_agent.parse_file_content(f.read(), "gateway.csv"), SourceType.GATEWAY)
    with open(os.path.join(corpus, "settlements.csv"), encoding="utf-8") as f:
        settlements = list(csv.DictReader(f))
    truth_path = os.path.join(corpus, "truth.json")
    truth = json.load(open(truth_path, encoding="utf-8")) if os.path.exists(truth_path) else {}
    return pool, settlements, truth


def explain(outcome: Outcome, report, pool: list[NormalizedTxn],
            batch: SettlementBatch) -> str:
    """
    Why this one did not close, in the words a controller would use.

    Ordered most-diagnostic first, and the FIRST thing checked is the one a
    human would check: do the payments that actually name this settlement add
    up to it?

    That question is asked first because "more than one set satisfies this
    total" is true of almost every failure and useful for none of them. When a
    member is missing from the export, the honest finding is not that the
    arithmetic is ambiguous — it is that the payments carrying this settlement
    reference come to a number that is not the settlement, by this much, in
    this direction. That is a query a controller can run against their own
    system. "Ambiguous" is not.
    """
    m = report.match_result

    # What the settlement itself claims as members.
    anchor = "".join(ch for ch in batch.batch_id if ch.isalnum()).upper()
    named = [t for t in pool if anchor and anchor in (t.ref_id_canonical or "").upper()]
    if named:
        named_sum = sum(t.amount_cents for t in named)
        gap = report.target_cents - named_sum
        if gap:
            direction = "short of" if gap > 0 else "over"
            return (f"the {len(named)} payment(s) referencing this settlement come "
                    f"to {RUPEE}{named_sum/100:,.2f}, {direction} the "
                    f"{RUPEE}{report.target_cents/100:,.2f} target by "
                    f"{RUPEE}{abs(gap)/100:,.2f} — a member is missing from the "
                    f"export, or the declared deductions are wrong")

        # The named payments DO tie out, so the reference is not the problem.
        # Then the usual cause is that some of them are not reachable: they
        # settled outside the lookback, so the solver was never offered them
        # and had to make the total out of something else.
        window_start = batch.settled_at_utc - timedelta(days=outcome.window_days)
        outside = [t for t in named
                   if not (window_start <= t.timestamp_utc <= batch.settled_at_utc)]
        if outside:
            oldest = min(t.timestamp_utc for t in outside)
            return (f"the payments referencing this settlement add up correctly, "
                    f"but {len(outside)} of them {'falls' if len(outside) == 1 else 'fall'} outside the "
                    f"{outcome.window_days}-day lookback (earliest "
                    f"{oldest.date()}) — widen the window or check the "
                    f"settlement date")

    if m.ambiguous and m.matched_txn_ids:
        return ("more than one set of payments satisfies this total — no single "
                "set is identified, so none is claimed")
    if not m.matched_txn_ids:
        return ("nothing in the window adds up to this settlement; a member is "
                "likely missing from the export")
    if m.withheld_reason == "no_corroborating_evidence":
        return ("the arithmetic works but nothing ties these payments to this "
                "settlement — no reference, no cross-feed match")
    if outcome.residual_cents:
        direction = "over" if outcome.residual_cents < 0 else "short"
        return (f"closest set is {direction} by "
                f"{RUPEE}{abs(outcome.residual_cents)/100:,.2f} - a missing member, or "
                f"the declared deductions are wrong")
    return m.withheld_reason or "withheld for review"


def close_batch(corpus: str, window: int) -> tuple[list[Outcome], dict]:
    pool, settlements, truth = load(corpus)
    outcomes: list[Outcome] = []
    t0 = time.perf_counter()

    for row in settlements:
        sid = row["settlement_id"]
        t = truth.get(sid, {})
        o = Outcome(settlement_id=sid, planted_hazard=t.get("hazard"),
                    members_expected=len(t.get("members", [])),
                    window_days=window,
                    truth=set(t.get("emitted", [])))

        batch = SettlementBatch(
            batch_id=sid,
            net_amount_cents=round(float(row["net_amount"]) * 100),
            currency=row.get("currency", "INR"),
            settled_at_utc=dp.parse(row["settled_at"]).replace(tzinfo=timezone.utc),
            source=SourceType.BANK,
            member_source=SourceType.GATEWAY,
            declared_deductions_cents=round(float(row["declared_deductions"]) * 100),
        )

        s = time.perf_counter()
        report = reconcile_settlement(batch, pool, settlement_window_days=window,
                                      rate_card=NET_TO_NET, subset_config=BATCH_CONFIG)
        o.elapsed_s = time.perf_counter() - s

        m = report.match_result
        o.matched = set(m.matched_txn_ids)
        o.closed = m.cleared
        o.exact = bool(o.truth) and o.matched == o.truth
        o.residual_cents = report.target_cents - m.matched_sum_cents
        if not o.closed:
            o.reason = explain(o, report, pool, batch)
        outcomes.append(o)

    return outcomes, {"records": len(pool), "wall_clock_s": time.perf_counter() - t0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="data/batch_close")
    ap.add_argument("--generate", action="store_true")
    ap.add_argument("--settlements", type=int, default=40)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--hazard-every", type=int, default=16,
                    help="plant a problem in 1 settlement in N. The match rate is "
                         "roughly 1 minus this rate, so it is reported alongside "
                         "the result rather than left implicit.")
    ap.add_argument("--window", type=int, default=5)
    ap.add_argument("--json", type=str, default="")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    if args.generate:
        g = generate(args.corpus, args.settlements, args.seed, args.hazard_every)
        print(f"Generated {g['records']} records across {g['settlements']} settlements "
              f"into {args.corpus}")
        print(f"  hazards planted: {dict(g['hazards'])}\n")

    if not os.path.exists(os.path.join(args.corpus, "gateway.csv")):
        print(f"No corpus at {args.corpus}. Run with --generate.")
        return 1

    outcomes, stats = close_batch(args.corpus, args.window)
    n = len(outcomes)
    closed = [o for o in outcomes if o.closed]
    correct = [o for o in closed if o.exact]
    unresolved = [o for o in outcomes if not o.closed]
    false_clears = [o for o in outcomes if o.false_clear]

    print("=" * 74)
    print("BATCH CLOSE")
    print("=" * 74)
    print(f"records ingested        {stats['records']:>6}")
    print(f"settlements attempted   {n:>6}")
    print(f"closed and correct      {len(correct):>6}   "
          f"({100*len(correct)/n:.1f}% match rate)")
    print(f"unresolved              {len(unresolved):>6}   "
          f"({100*len(unresolved)/n:.1f}%)")
    print(f"FALSE CLEARS            {len(false_clears):>6}   "
          f"<- cleared a set that was not the true one")
    print("-" * 74)
    print(f"wall clock              {stats['wall_clock_s']:>6.1f}s   "
          f"({stats['records']/max(stats['wall_clock_s'], 1e-9):,.0f} records/sec)")
    print(f"median per settlement   "
          f"{sorted(o.elapsed_s for o in outcomes)[n//2]:>6.2f}s")

    if unresolved:
        print("\n" + "-" * 74)
        print(f"EXCEPTIONS — {len(unresolved)} settlement(s) this agent could not close")
        print("-" * 74)
        for o in unresolved:
            planted = f"  [planted: {o.planted_hazard}]" if o.planted_hazard else ""
            print(f"\n  {o.settlement_id}{planted}")
            print(f"    {o.reason}")
            if o.matched:
                print(f"    best set: {len(o.matched)} payment(s), "
                      f"residual {RUPEE}{o.residual_cents/100:,.2f}")

    # Did the exceptions land where the hazards were planted? This is the
    # check that separates "declined the right ones" from "declined at random".
    planted = {o.settlement_id for o in outcomes if o.planted_hazard}
    declined = {o.settlement_id for o in unresolved}
    if planted:
        print("\n" + "-" * 74)
        print(f"planted hazards           {len(planted)}")
        print(f"of those, declined        {len(planted & declined)}")
        print(f"declined without a hazard {len(declined - planted)}")
        print(f"closed despite a hazard   {len(planted - declined)}")

    print("=" * 74)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({
                "records": stats["records"], "settlements": n,
                "match_rate_pct": round(100 * len(correct) / n, 2),
                "false_clears": len(false_clears),
                "wall_clock_s": round(stats["wall_clock_s"], 2),
                "records_per_sec": round(stats["records"] / max(stats["wall_clock_s"], 1e-9)),
                "exceptions": [
                    {"settlement_id": o.settlement_id, "reason": o.reason,
                     "planted_hazard": o.planted_hazard,
                     "residual_cents": o.residual_cents}
                    for o in unresolved
                ],
            }, f, indent=2)
        print(f"Wrote {args.json}")

    return 1 if false_clears else 0


if __name__ == "__main__":
    sys.exit(main())
