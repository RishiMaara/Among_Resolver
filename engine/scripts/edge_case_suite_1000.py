#!/usr/bin/env python3
"""
1,000 adversarial edge-case scenarios against the reconciliation engine.

WHY THIS EXISTS
---------------
scripts/benchmark.py measures accuracy on 12 families of well-shaped
settlement data plus linkage degradation. It does not probe the degenerate
ends: targets that cannot be hit, pools with nothing in them, amounts with no
entropy, currency mixed into the arithmetic, refunds, fee drift, window
boundaries, id collisions across feeds, or several settlements competing for
the same payments.

Those are where a reconciliation engine actually fails in production, and the
failure that matters is never a missed match — it is a CONFIDENT WRONG ONE.
So the headline number here is the false-clear count: auto-cleared a set that
is not the truth. Everything else is secondary.

Each family declares what should happen:

    clear     the truth is uniquely recoverable from the evidence given;
              auto-clearing it is correct, withholding is a coverage loss
    no_clear  the truth is NOT recoverable (absent leg, wrong target,
              no distinguishing evidence); auto-clearing ANYTHING is wrong
    any       either outcome is defensible; only a wrong clear is a failure

    python scripts/edge_case_suite_1000.py
    python scripts/edge_case_suite_1000.py --cases 1000 --seed 20260912         --json benchmarks/edge_1000.json

Unlike scripts/edge_case_suite.py, which drives a LIVE engine over HTTP and
asserts behaviour on 50 hand-written awkward files, this runs in-process
against reconcile_batch / reconcile_many and SCORES accuracy against known
ground truth. The two are complements: that one covers Agent 0 and the API
surface, this one covers the matching decision itself at volume.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from schema import (  # noqa: E402
    NormalizedTxn, SettlementBatch, SourceType, TzConfidence,
)
from subset_sum import SubsetSumConfig  # noqa: E402
from orchestrator import reconcile_batch, reconcile_many  # noqa: E402
from fee_decomposition import FeeRateCard, DEFAULT_RATE_CARD  # noqa: E402

SETTLED = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
WINDOW_DAYS = 5

CFG = SubsetSumConfig(
    tolerance_cents=10,
    solver_time_limit_s=5.0,
    ambiguity_probe_limit=2,
    # Single worker: CP-SAT's parallel search is not reproducible, and a
    # measurement harness that cannot reproduce its own number cannot back a
    # claim made from it. Same reasoning as scripts/benchmark.py.
    num_search_workers=1,
)

# Psychological price points, in paise. Real merchant traffic clusters here;
# uniform-random amounts (what benchmark.py draws) make collisions rare by
# construction and flatter the solver.
PRICE_POINTS = [9900, 19900, 29900, 49900, 99900, 199900, 49900,
                99900, 149900, 199900, 249900, 499900, 999900]


def txn(tid: str, cents: int, age_h: float, ref: str, memo: str = "",
        source: SourceType = SourceType.GATEWAY, currency: str = "INR",
        stated: bool = True, tz: TzConfidence = TzConfidence.HIGH) -> NormalizedTxn:
    """age_h = hours BEFORE the settlement timestamp."""
    return NormalizedTxn(
        source=source,
        source_txn_id=tid,
        ref_id_canonical=ref,
        amount_cents=cents,
        currency=currency,
        currency_stated=stated,
        timestamp_utc=SETTLED - timedelta(hours=age_h),
        tz_confidence=tz,
        memo_raw=memo or f"txn {tid}",
        memo_normalized=(memo or f"txn {tid}").lower(),
    )


def batch_exact(bid: str, gross: int, ref: str = "") -> SettlementBatch:
    """
    A settlement whose target is a FACT, not an inference.

    declared_deductions_cents=0 makes the gross target equal the net figure
    exactly, which isolates the edge under test from fee-estimation drift.
    Families that mean to test fee drift build their batch the other way.
    """
    return SettlementBatch(
        batch_id=bid, net_amount_cents=gross, currency="INR",
        settled_at_utc=SETTLED, source=SourceType.BANK,
        ref_id=ref, memo=f"Settlement {bid}",
        declared_deductions_cents=0,
    )


def noise(idx: int, n: int, rng: random.Random, lo: int = 5_000,
          hi: int = 400_000, tag: str = "N") -> list[NormalizedTxn]:
    return [txn(f"E{idx}_{tag}{i}", rng.randint(lo, hi), rng.uniform(2, 110),
                f"NOISE{idx}_{i}", "unrelated payment") for i in range(n)]


@dataclass
class Case:
    case_id: str
    family: str
    expect: str                      # clear | no_clear | any
    batch: SettlementBatch
    candidates: list[NormalizedTxn]
    truth_ids: set[str]
    truth_alt: set[str] = field(default_factory=set)
    joint: list[SettlementBatch] = field(default_factory=list)
    joint_truth: dict = field(default_factory=dict)
    note: str = ""


@dataclass
class Res:
    case_id: str
    family: str
    expect: str
    cleared: bool
    ambiguous: bool
    confidence: float
    method: str
    withheld_reason: str
    matched: set
    truth: set
    pool: int
    elapsed: float
    error: str = ""
    double_claim: bool = False
    overlap_withheld: bool = False
    alt: set = field(default_factory=set)

    @property
    def set_is_truth(self) -> bool:
        if not self.matched:
            return False
        return self.matched == self.truth or (
            bool(self.alt) and self.matched == self.alt)

    @property
    def used_alt(self) -> bool:
        """Right payments, identified through their mirror rather than the original."""
        return bool(self.alt) and self.matched == self.alt

    @property
    def false_clear(self) -> bool:
        """Auto-cleared a set that is not the truth. The one that matters."""
        return self.cleared and not self.set_is_truth

    @property
    def missed_clear(self) -> bool:
        """Truth was recoverable and it did not auto-clear. Coverage loss."""
        return self.expect == "clear" and not self.cleared

    @property
    def precision(self) -> float:
        return len(self.matched & self.truth) / len(self.matched) if self.matched else 0.0

    @property
    def recall(self) -> float:
        return len(self.matched & self.truth) / len(self.truth) if self.truth else 0.0


# ── family builders ───────────────────────────────────────────────────────
# Each returns (candidates, truth_ids, net_cents, batch_override_or_None).

def f_single_member(i, rng, bid):
    """A settlement of exactly one payment. Subset-sum's degenerate case."""
    amt = rng.randint(10_000, 900_000)
    t = txn(f"E{i}_T0", amt, rng.uniform(2, 100), bid, "the only leg")
    pool = noise(i, rng.choice([30, 80, 150]), rng) + [t]
    rng.shuffle(pool)
    return pool, {t.source_txn_id}, amt, None


def f_whole_pool(i, rng, bid):
    """Target equals the sum of every candidate in the pool."""
    n = rng.choice([8, 15, 30])
    ts = [txn(f"E{i}_T{k}", rng.randint(5_000, 200_000), rng.uniform(2, 100),
              bid, "leg") for k in range(n)]
    return ts, {t.source_txn_id for t in ts}, sum(t.amount_cents for t in ts), None


def f_huge_subset(i, rng, bid):
    """40-60 small transactions compose one settlement. Combinatorial worst case."""
    n = rng.randint(40, 60)
    ts = [txn(f"E{i}_T{k}", rng.randint(2_000, 60_000), rng.uniform(2, 100),
              bid, "leg") for k in range(n)]
    pool = ts + noise(i, 140, rng)
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, sum(t.amount_cents for t in ts), None


def f_micro_amounts(i, rng, bid):
    """Every amount 1-60 paise and no reference. Arithmetically hopeless."""
    ts = [txn(f"E{i}_T{k}", rng.randint(1, 60), rng.uniform(2, 100), "", "micro")
          for k in range(rng.choice([4, 5, 6]))]
    pool = ts + [txn(f"E{i}_N{k}", rng.randint(1, 60), rng.uniform(2, 110), "", "micro")
                 for k in range(80)]
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, sum(t.amount_cents for t in ts), None


def f_same_amount_anchored(i, rng, bid):
    """Every sale the same price; only the true legs carry the settlement ref."""
    amt = rng.choice([49900, 99900, 150000])
    k = rng.choice([3, 4, 5])
    ts = [txn(f"E{i}_T{j}", amt, rng.uniform(2, 100), bid, "leg") for j in range(k)]
    pool = ts + [txn(f"E{i}_N{j}", amt, rng.uniform(2, 110), f"OTHER{i}_{j}", "same price")
                 for j in range(40)]
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, amt * k, None


def f_same_amount_blind(i, rng, bid):
    """Same price, NO reference anywhere. Truth is not identifiable, by design."""
    amt = rng.choice([49900, 99900, 150000])
    k = rng.choice([3, 4, 5])
    ts = [txn(f"E{i}_T{j}", amt, rng.uniform(2, 100), "", "leg") for j in range(k)]
    pool = ts + [txn(f"E{i}_N{j}", amt, rng.uniform(2, 110), "", "same price")
                 for j in range(40)]
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, amt * k, None


def f_price_points(i, rng, bid):
    """Amounts drawn from a price ladder: few distinct values, heavy degeneracy."""
    k = rng.choice([3, 4, 5])
    ts = [txn(f"E{i}_T{j}", rng.choice(PRICE_POINTS), rng.uniform(2, 100),
              bid, "leg") for j in range(k)]
    pool = ts + [txn(f"E{i}_N{j}", rng.choice(PRICE_POINTS), rng.uniform(2, 110),
                     f"NOISE{i}_{j}", "ladder") for j in range(rng.choice([60, 150]))]
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, sum(t.amount_cents for t in ts), None


def f_lognormal(i, rng, bid):
    """Lognormal amounts — the real shape of merchant traffic."""
    def amt():
        return max(100, int(math.exp(rng.gauss(10.5, 1.1))))
    k = rng.choice([3, 4, 5])
    ts = [txn(f"E{i}_T{j}", amt(), rng.uniform(2, 100), bid, "leg") for j in range(k)]
    pool = ts + [txn(f"E{i}_N{j}", amt(), rng.uniform(2, 110), f"NOISE{i}_{j}", "traffic")
                 for j in range(rng.choice([80, 200]))]
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, sum(t.amount_cents for t in ts), None


def f_refund_in_pool(i, rng, bid):
    """A refund (negative) sits inside the settled set and must be netted."""
    k = rng.choice([3, 4])
    ts = [txn(f"E{i}_T{j}", rng.randint(50_000, 300_000), rng.uniform(20, 100),
              bid, "sale") for j in range(k)]
    rf = txn(f"E{i}_RF", -rng.randint(10_000, 40_000), rng.uniform(2, 18),
             bid, "Refund")
    ts.append(rf)
    pool = ts + noise(i, 60, rng)
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, sum(t.amount_cents for t in ts), None


def f_refund_unanchored(i, rng, bid):
    """Refunds in the pool that are NOT part of this settlement."""
    k = rng.choice([3, 4])
    ts = [txn(f"E{i}_T{j}", rng.randint(50_000, 300_000), rng.uniform(20, 100),
              bid, "sale") for j in range(k)]
    strays = [txn(f"E{i}_RX{j}", -rng.randint(5_000, 50_000), rng.uniform(2, 110),
                  f"OTHER{i}_{j}", "Refund") for j in range(6)]
    pool = ts + strays + noise(i, 60, rng)
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, sum(t.amount_cents for t in ts), None


def f_currency_mixed(i, rng, bid):
    """
    USD rows whose face values would complete the sum.

    This is the 100 INR + 100 INR + 100 USD = 300 bug, which once cleared at
    0.97 confidence. Any USD id in the matched set is a hard failure.
    """
    k = rng.choice([3, 4])
    ts = [txn(f"E{i}_T{j}", rng.randint(50_000, 200_000), rng.uniform(2, 100),
              bid, "leg") for j in range(k)]
    usd = [txn(f"E{i}_USD{j}", t.amount_cents, rng.uniform(2, 110),
               f"USD{i}_{j}", "dollar leg", currency="USD") for j, t in enumerate(ts)]
    pool = ts + usd + noise(i, 50, rng)
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, sum(t.amount_cents for t in ts), None


def f_currency_unstated(i, rng, bid):
    """Half the pool never said what currency it was; the value is a default."""
    k = rng.choice([3, 4, 5])
    ts = [txn(f"E{i}_T{j}", rng.randint(20_000, 300_000), rng.uniform(2, 100),
              bid, "leg", stated=(j % 2 == 0)) for j in range(k)]
    pool = ts + [txn(f"E{i}_N{j}", rng.randint(5_000, 400_000), rng.uniform(2, 110),
                     f"NOISE{i}_{j}", "n", stated=False) for j in range(60)]
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, sum(t.amount_cents for t in ts), None


def f_window_edge_in(i, rng, bid):
    """A true leg sitting exactly on the inclusive window boundary."""
    k = rng.choice([3, 4])
    ts = [txn(f"E{i}_T{j}", rng.randint(20_000, 300_000), rng.uniform(2, 90),
              bid, "leg") for j in range(k - 1)]
    edge = txn(f"E{i}_EDGE", rng.randint(20_000, 300_000),
               WINDOW_DAYS * 24 - 0.001, bid, "boundary leg")
    ts.append(edge)
    pool = ts + noise(i, 60, rng)
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, sum(t.amount_cents for t in ts), None


def f_window_edge_out(i, rng, bid):
    """A true leg one hour OUTSIDE the window. Unrecoverable by construction."""
    k = rng.choice([3, 4])
    ts = [txn(f"E{i}_T{j}", rng.randint(20_000, 300_000), rng.uniform(2, 90),
              bid, "leg") for j in range(k - 1)]
    out = txn(f"E{i}_OUT", rng.randint(20_000, 300_000),
              WINDOW_DAYS * 24 + 1, bid, "stale leg")
    ts.append(out)
    pool = ts + noise(i, 60, rng)
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, sum(t.amount_cents for t in ts), None


def f_tz_low(i, rng, bid):
    """Timestamps the parser could not place in a timezone with confidence."""
    k = rng.choice([3, 4])
    ts = [txn(f"E{i}_T{j}", rng.randint(20_000, 300_000), rng.uniform(6, 100),
              bid, "leg", tz=TzConfidence.LOW) for j in range(k)]
    pool = ts + noise(i, 60, rng)
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, sum(t.amount_cents for t in ts), None


def f_duplicate_row(i, rng, bid):
    """The same payment present twice under different ids — a re-exported feed."""
    k = rng.choice([3, 4])
    ts = [txn(f"E{i}_T{j}", rng.randint(20_000, 300_000), rng.uniform(2, 100),
              bid, "leg") for j in range(k)]
    clone = txn(f"E{i}_CLONE", ts[0].amount_cents,
                (SETTLED - ts[0].timestamp_utc).total_seconds() / 3600,
                "", "re-export of the same payment")
    pool = ts + [clone] + noise(i, 60, rng)
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, sum(t.amount_cents for t in ts), None


def f_id_collision(i, rng, bid):
    """
    Two feeds minting the same source_txn_id for different payments.

    source_txn_id is unique per feed, not globally; matching on id alone
    leaks signal between unrelated records.
    """
    k = rng.choice([3, 4])
    ts = [txn(f"E{i}_T{j}", rng.randint(20_000, 300_000), rng.uniform(2, 100),
              bid, "leg") for j in range(k)]
    collide = [txn(t.source_txn_id, rng.randint(5_000, 400_000), rng.uniform(2, 110),
                   f"ERP{i}_{j}", "different payment, same id",
                   source=SourceType.ERP) for j, t in enumerate(ts)]
    pool = ts + collide + noise(i, 60, rng)
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, sum(t.amount_cents for t in ts), None


def f_sequential_ids(i, rng, bid):
    """
    Unpadded sequential settlement ids: STL7 is a prefix of STL70.

    This produced false anchors — the highest-confidence wrong answer the
    engine can give — on the real-data sweep.
    """
    k = rng.choice([3, 4])
    # The settlement's own id is short and unpadded, so every neighbouring
    # settlement's id CONTAINS it as a prefix. That is the false-anchor trap.
    base = f"STL{1000 + i % 9}"
    ts = [txn(f"E{i}_T{j}", rng.randint(20_000, 300_000), rng.uniform(2, 100),
              base, "leg") for j in range(k)]
    sibs = [txn(f"E{i}_SEQ{j}", rng.randint(5_000, 400_000), rng.uniform(2, 110),
                f"{base}{j}", "neighbouring settlement") for j in range(12)]
    pool = ts + sibs + noise(i, 50, rng)
    rng.shuffle(pool)
    gross = sum(t.amount_cents for t in ts)
    return pool, {t.source_txn_id for t in ts}, gross, batch_exact(base, gross, ref=base)


def f_fee_estimated(i, rng, bid):
    """No declared deductions: the target is reconstructed from the rate card."""
    k = rng.choice([3, 4, 5])
    ts = [txn(f"E{i}_T{j}", rng.randint(20_000, 300_000), rng.uniform(2, 100),
              bid, "leg") for j in range(k)]
    pool = ts + noise(i, 60, rng)
    rng.shuffle(pool)
    gross = sum(t.amount_cents for t in ts)
    gw = round(gross * DEFAULT_RATE_CARD.gateway_fee_bps / 10_000)
    tax = round(gross * DEFAULT_RATE_CARD.tax_withholding_bps / 10_000)
    b = SettlementBatch(
        batch_id=bid, net_amount_cents=gross - gw - tax,
        currency="INR", settled_at_utc=SETTLED, source=SourceType.BANK,
        ref_id=bid, memo="rate-card settlement")
    return pool, {t.source_txn_id for t in ts}, b.net_amount_cents, b


def f_fee_variance(i, rng, bid):
    """
    The processor's actual deductions differ from the card by 20-60bps.

    Subset-sum is exact. A target reconstructed from the wrong card is a
    wrong target, and the true subset no longer sums to it.
    """
    k = rng.choice([3, 4, 5])
    ts = [txn(f"E{i}_T{j}", rng.randint(50_000, 400_000), rng.uniform(2, 100),
              bid, "leg") for j in range(k)]
    pool = ts + noise(i, 60, rng)
    rng.shuffle(pool)
    gross = sum(t.amount_cents for t in ts)
    actual_bps = 300 + rng.choice([20, 35, 50, 60])       # card says 300
    net = gross - round(gross * actual_bps / 10_000)
    b = SettlementBatch(
        batch_id=bid, net_amount_cents=net, currency="INR",
        settled_at_utc=SETTLED, source=SourceType.BANK,
        ref_id=bid, memo="off-card settlement")
    return pool, {t.source_txn_id for t in ts}, net, b


def f_tolerance_inside(i, rng, bid):
    """Target off by 1-10 paise: inside the tolerance band."""
    k = rng.choice([3, 4])
    ts = [txn(f"E{i}_T{j}", rng.randint(20_000, 300_000), rng.uniform(2, 100),
              bid, "leg") for j in range(k)]
    pool = ts + noise(i, 60, rng)
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, \
        sum(t.amount_cents for t in ts) - rng.randint(1, 10), None


def f_tolerance_outside(i, rng, bid):
    """Target off by 11-200 paise: outside tolerance. Nothing should sum."""
    k = rng.choice([3, 4])
    ts = [txn(f"E{i}_T{j}", rng.randint(20_000, 300_000), rng.uniform(2, 100),
              bid, "leg") for j in range(k)]
    pool = ts + noise(i, 60, rng)
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, \
        sum(t.amount_cents for t in ts) - rng.randint(11, 200), None


def f_target_exceeds_pool(i, rng, bid):
    """A settlement larger than every payment in the pool combined."""
    pool = noise(i, rng.choice([40, 100]), rng)
    total = sum(t.amount_cents for t in pool)
    return pool, set(), total + rng.randint(100_000, 900_000), None


def f_empty_pool(i, rng, bid):
    """No candidates at all. Must report, not crash."""
    return [], set(), rng.randint(50_000, 500_000), None


def f_magnitude_spread(i, rng, bid):
    """One ₹5,00,000 leg and a crowd of 1-5 paise dust."""
    big = txn(f"E{i}_T0", rng.randint(40_000_000, 60_000_000), rng.uniform(2, 90),
              bid, "the big one")
    smalls = [txn(f"E{i}_T{j+1}", rng.randint(1, 500), rng.uniform(2, 90),
                  bid, "dust") for j in range(rng.choice([2, 3]))]
    ts = [big] + smalls
    dust = [txn(f"E{i}_D{j}", rng.randint(1, 500), rng.uniform(2, 110),
                f"NOISE{i}_{j}", "dust") for j in range(70)]
    pool = ts + dust
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, sum(t.amount_cents for t in ts), None


def f_decoy_exact(i, rng, bid):
    """
    An unanchored noise subset that sums EXACTLY to the target.

    The arithmetic cannot separate it from the truth; only the reference
    evidence can. Clearing the decoy is the failure this family hunts.
    """
    k = rng.choice([3, 4])
    ts = [txn(f"E{i}_T{j}", rng.randint(50_000, 300_000), rng.uniform(2, 100),
              bid, "leg") for j in range(k)]
    gross = sum(t.amount_cents for t in ts)
    a = gross // 3
    b = gross // 3
    c = gross - a - b
    decoys = [txn(f"E{i}_DEC{n}", amt, rng.uniform(2, 110), f"DEC{i}_{n}", "decoy")
              for n, amt in enumerate((a, b, c))]
    pool = ts + decoys + noise(i, 60, rng)
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, gross, None


def f_mirror_inverted(i, rng, bid):
    """The ERP mirror carries the settlement reference; the gateway original does not."""
    k = rng.choice([3, 4])
    ts = [txn(f"E{i}_T{j}", rng.randint(20_000, 300_000), rng.uniform(2, 100),
              "", "gateway leg, no ref") for j in range(k)]
    mirrors = [txn(f"{t.source_txn_id}_MIR", t.amount_cents,
                   (SETTLED - t.timestamp_utc).total_seconds() / 3600 + 1,
                   bid, "ERP entry", source=SourceType.ERP) for t in ts]
    pool = ts + mirrors + noise(i, 60, rng)
    rng.shuffle(pool)
    return (pool, {t.source_txn_id for t in ts},
            sum(t.amount_cents for t in ts), None,
            {m.source_txn_id for m in mirrors})


def f_missing_leg(i, rng, bid):
    """A true member never made it into the feed."""
    k = rng.choice([3, 4, 5])
    ts = [txn(f"E{i}_T{j}", rng.randint(20_000, 300_000), rng.uniform(2, 100),
              bid, "leg") for j in range(k)]
    gross = sum(t.amount_cents for t in ts)
    dropped = ts.pop()
    pool = ts + noise(i, 60, rng)
    rng.shuffle(pool)
    truth = {t.source_txn_id for t in ts} | {dropped.source_txn_id}
    return pool, truth, gross, None


def f_zero_target(i, rng, bid):
    """A settlement that nets to zero — refunds cancelling sales."""
    k = rng.choice([2, 3])
    ts = []
    for j in range(k):
        amt = rng.randint(20_000, 200_000)
        ts.append(txn(f"E{i}_T{j}a", amt, rng.uniform(20, 100), bid, "sale"))
        ts.append(txn(f"E{i}_T{j}b", -amt, rng.uniform(2, 18), bid, "Refund"))
    pool = ts + noise(i, 50, rng)
    rng.shuffle(pool)
    return pool, {t.source_txn_id for t in ts}, 0, None


SINGLE_FAMILIES = [
    ("single_member",        f_single_member,        "clear",    "settlement of exactly one payment"),
    ("whole_pool",           f_whole_pool,           "clear",    "target is the entire pool"),
    ("huge_subset",          f_huge_subset,          "any",      "40-60 legs in one settlement"),
    ("micro_amounts",        f_micro_amounts,        "no_clear", "1-60 paise amounts, no references"),
    ("same_amount_anchored", f_same_amount_anchored, "clear",    "identical prices, refs on true legs"),
    ("same_amount_blind",    f_same_amount_blind,    "no_clear", "identical prices, no references at all"),
    ("price_points",         f_price_points,         "any",      "amounts from a price ladder"),
    ("lognormal",            f_lognormal,            "any",      "lognormal merchant traffic"),
    ("refund_in_pool",       f_refund_in_pool,       "any",      "a refund inside the settled set"),
    ("refund_unanchored",    f_refund_unanchored,    "any",      "stray refunds not in this settlement"),
    ("currency_mixed",       f_currency_mixed,       "clear",    "USD rows that would complete the sum"),
    ("currency_unstated",    f_currency_unstated,    "any",      "currency was never stated by the file"),
    ("window_edge_in",       f_window_edge_in,       "clear",    "a leg exactly on the window boundary"),
    ("window_edge_out",      f_window_edge_out,      "no_clear", "a leg one hour outside the window"),
    ("tz_low_confidence",    f_tz_low,               "any",      "timestamps of low timezone confidence"),
    ("duplicate_row",        f_duplicate_row,        "any",      "the same payment exported twice"),
    ("id_collision",         f_id_collision,         "any",      "two feeds reusing one txn id"),
    ("sequential_ids",       f_sequential_ids,       "any",      "STL7 vs STL70 prefix anchors"),
    ("fee_estimated",        f_fee_estimated,        "clear",    "target reconstructed from the rate card"),
    ("fee_variance",         f_fee_variance,         "no_clear", "actual deductions 20-60bps off the card"),
    ("tolerance_inside",     f_tolerance_inside,     "clear",    "target off by 1-10 paise"),
    ("tolerance_outside",    f_tolerance_outside,    "no_clear", "target off by 11-200 paise"),
    ("target_exceeds_pool",  f_target_exceeds_pool,  "no_clear", "settlement bigger than the whole pool"),
    ("empty_pool",           f_empty_pool,           "no_clear", "no candidates at all"),
    ("magnitude_spread",     f_magnitude_spread,     "any",      "one huge leg among paise dust"),
    ("decoy_exact",          f_decoy_exact,          "any",      "unanchored noise summing exactly to target"),
    ("mirror_inverted",      f_mirror_inverted,      "any",      "the ERP mirror holds the reference"),
    ("missing_leg",          f_missing_leg,          "no_clear", "a true member absent from the feed"),
    ("zero_target",          f_zero_target,          "any",      "sales and refunds netting to zero"),
]


def build_single(i: int, family: str, fn, expect: str, rng: random.Random) -> Case:
    # Linkage anchors on tokens of batch_id (>=4 chars), not on batch.ref_id,
    # so the settlement's own identifier is what a true leg must carry in its
    # reference. Noise carries NOISE<i>_<k>, which shares no token with it.
    bid = f"EDGE{i:05d}"
    built = fn(i, rng, bid)
    pool, truth, net, override = built[:4]
    alt = built[4] if len(built) > 4 else set()
    batch = override or batch_exact(bid, net, ref=bid)
    return Case(case_id=f"{family}#{i}", family=family, expect=expect,
                batch=batch, candidates=pool, truth_ids=truth, truth_alt=alt)


def build_joint(i: int, rng: random.Random) -> Case:
    """
    Two or three settlements competing for one pool, solved jointly.

    Contention is real, not decorative: one leg of each batch has a twin in
    another batch with the SAME amount, so swapping them leaves every target
    satisfied. Arithmetic cannot choose; only the references can. A payment
    assigned to two batches at once is a hard failure regardless of sums.
    """
    nb = rng.choice([2, 2, 3])
    batches, truth_map = [], {}
    pool: list[NormalizedTxn] = []
    twin = rng.randint(40_000, 200_000)
    starve = rng.random() < 0.25          # one batch made unsatisfiable
    for b in range(nb):
        k = rng.choice([3, 4])
        # Digit-run deliberately disjoint from the noise refs' own
        # (NOISE<i>_<k> -> token "<i>"), so the settlement anchors its own
        # legs and nothing else. Sharing it made every noise record anchored.
        bid_b = f"NMB{700000 + (i % 10000) * 10 + b}"
        legs = [txn(f"E{i}_B{b}_T{j}", rng.randint(20_000, 300_000),
                    rng.uniform(2, 100), bid_b, f"batch {b} leg")
                for j in range(k - 1)]
        legs.append(txn(f"E{i}_B{b}_TWIN", twin, rng.uniform(2, 100),
                        bid_b, "the contested leg"))
        gross = sum(t.amount_cents for t in legs)
        if starve and b == 0:
            missing = legs.pop()           # target still counts it
            starved_bid = bid_b
        pool.extend(legs)
        batches.append(batch_exact(bid_b, gross, ref=bid_b))
        truth_map[bid_b] = {t.source_txn_id for t in legs} | (
            {missing.source_txn_id} if starve and b == 0 else set())
    pool.extend(noise(i, 50, rng))
    rng.shuffle(pool)
    return Case(case_id=f"nm_contention#{i}", family="nm_contention",
                expect="no_clear" if starve else "any",
                batch=batches[0], candidates=pool, truth_ids=set(),
                joint=batches, joint_truth=truth_map,
                note=f"starved:{starved_bid}" if starve else "")


def run_case(c: Case) -> list[Res]:
    t0 = time.perf_counter()
    try:
        if c.joint:
            reports = reconcile_many(c.joint, c.candidates,
                                     settlement_window_days=WINDOW_DAYS,
                                     subset_config=CFG)
            el = time.perf_counter() - t0
            claimed: Counter = Counter()
            for r in reports:
                if r.match_result.cleared:
                    claimed.update(r.match_result.matched_txn_ids)
            dc = any(v > 1 for v in claimed.values())
            proposed: Counter = Counter()
            for r in reports:
                proposed.update(r.match_result.matched_txn_ids)
            overlap_held = any(v > 1 for v in proposed.values()) and not dc
            out = []
            for r in reports:
                m = r.match_result
                truth = c.joint_truth.get(m.batch_id, set())
                expect = "any"
                if c.note.startswith("starved:") and m.batch_id == c.note.split(":", 1)[1]:
                    expect = "no_clear"
                out.append(Res(
                    case_id=f"{c.case_id}/{m.batch_id}", family=c.family,
                    expect=expect, cleared=m.cleared, ambiguous=m.ambiguous,
                    confidence=m.confidence, method=m.method.value,
                    withheld_reason=m.withheld_reason or "",
                    matched=set(m.matched_txn_ids), truth=truth,
                    pool=len(c.candidates), elapsed=el / max(1, len(reports)),
                    double_claim=dc, overlap_withheld=overlap_held))
            return out

        rep = reconcile_batch(c.batch, c.candidates, subset_config=CFG,
                              settlement_window_days=WINDOW_DAYS)
        m = rep.match_result
        return [Res(case_id=c.case_id, family=c.family, expect=c.expect,
                    cleared=m.cleared, ambiguous=m.ambiguous,
                    confidence=m.confidence, method=m.method.value,
                    withheld_reason=m.withheld_reason or "",
                    matched=set(m.matched_txn_ids), truth=c.truth_ids,
                    pool=len(c.candidates), elapsed=time.perf_counter() - t0,
                    alt=c.truth_alt)]
    except Exception:
        return [Res(case_id=c.case_id, family=c.family, expect=c.expect,
                    cleared=False, ambiguous=False, confidence=0.0, method="ERROR",
                    withheld_reason="", matched=set(), truth=c.truth_ids,
                    pool=len(c.candidates), elapsed=time.perf_counter() - t0,
                    error=traceback.format_exc(limit=4))]


BUCKETS = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 0.93), (0.93, 1.0), (1.0, 1.01)]


def pct(n, d):
    return round(100.0 * n / d, 2) if d else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=20260912)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    n_joint = max(1, args.cases // 26)
    n_single = args.cases - n_joint

    cases: list[Case] = []
    for i in range(n_single):
        fam, fn, expect, _ = SINGLE_FAMILIES[i % len(SINGLE_FAMILIES)]
        cases.append(build_single(i, fam, fn, expect, rng))
    for i in range(n_joint):
        cases.append(build_joint(100_000 + i, rng))

    print(f"{len(cases)} edge cases "
          f"({n_single} single-batch across {len(SINGLE_FAMILIES)} families, "
          f"{n_joint} joint N:M)\n")

    results: list[Res] = []
    t0 = time.perf_counter()
    for n, c in enumerate(cases, 1):
        results.extend(run_case(c))
        if n % 100 == 0:
            print(f"  {n}/{len(cases)}  ({time.perf_counter() - t0:.0f}s)")

    # ── scoring ───────────────────────────────────────────────────────────
    total = len(results)
    errors = [r for r in results if r.error]
    cleared = [r for r in results if r.cleared]
    fc = [r for r in results if r.false_clear]
    must_not = [r for r in results if r.expect == "no_clear"]
    should = [r for r in results if r.expect == "clear"]
    dc = [r for r in results if r.double_claim]

    # Currency contamination is checked directly: a USD id inside a matched
    # set is a hard failure no matter what the sums say.
    usd_leak = [r for r in results if any("_USD" in m for m in r.matched)]

    print("\n" + "=" * 74)
    print("EDGE-CASE SUITE — 1,000 ADVERSARIAL SCENARIOS")
    print("=" * 74)
    print(f"Scenarios scored       : {total}   (batches; joint cases score per batch)")
    print(f"Crashes / exceptions   : {len(errors)}")
    print("-" * 74)
    print(f"FALSE CLEARS           : {len(fc)}  ({pct(len(fc), total)}%)   <- cleared a WRONG set")
    print(f"Double-CLEARED txns    : {len(dc)}   <- one payment released to two settlements")
    print(f"Overlaps caught+held   : {sum(1 for r in results if r.overlap_withheld)}"
          f"   <- contested payment, both sides sent to review")
    print(f"Currency contamination : {len(usd_leak)}   <- USD row inside an INR match")
    print(f"Mirror substitutions   : {sum(1 for r in results if r.used_alt)}"
          f"   <- right payments, matched via their ERP mirror")
    print("-" * 74)
    print(f"Auto-cleared           : {len(cleared)}  ({pct(len(cleared), total)}%)")
    print(f"  of those, correct    : {pct(sum(1 for r in cleared if r.set_is_truth), len(cleared))}%")
    print(f"Must-not-clear held    : {pct(sum(1 for r in must_not if not r.cleared), len(must_not))}%"
          f"   ({len(must_not)} scenarios)")
    print(f"Should-clear cleared   : {pct(sum(1 for r in should if r.cleared), len(should))}%"
          f"   ({len(should)} scenarios)")
    truth_found = [r for r in results if r.set_is_truth]
    print(f"Truth set identified   : {pct(len(truth_found), total)}%   (cleared or flagged)")
    print(f"Mean precision/recall  : {round(statistics.mean([r.precision for r in results]), 4)}"
          f" / {round(statistics.mean([r.recall for r in results]), 4)}")
    lat = sorted(r.elapsed for r in results)
    print(f"Latency p50/p95/max    : {lat[len(lat)//2]:.3f}s / "
          f"{lat[int(len(lat)*0.95)]:.3f}s / {lat[-1]:.3f}s")
    print(f"Total wall clock       : {time.perf_counter() - t0:.0f}s")

    print("\n" + "-" * 74)
    print("CONFIDENCE CALIBRATION  (does the number mean what it says?)")
    print(f"{'band':<14} {'n':>5} {'mean conf':>10} {'actually right':>15} {'gap':>8}")
    print("-" * 74)
    calib = {}
    for lo, hi in BUCKETS:
        band = [r for r in results if lo <= r.confidence < hi and r.matched]
        if not band:
            continue
        mc = statistics.mean(r.confidence for r in band)
        acc = sum(1 for r in band if r.set_is_truth) / len(band)
        label = f"[{lo:.2f},{hi:.2f})" if hi <= 1.0 else "[1.00]"
        calib[label] = {"n": len(band), "mean_confidence": round(mc, 3),
                        "observed_accuracy": round(acc, 3)}
        print(f"{label:<14} {len(band):>5} {mc:>10.3f} {acc:>14.1%} {acc-mc:>+8.3f}")

    print("\n" + "-" * 74)
    print(f"{'family':<22} {'n':>4} {'expect':>9} {'held/clr':>9} {'truth':>7} {'FALSE':>6} {'med s':>7}")
    print("-" * 74)
    by_fam = defaultdict(list)
    for r in results:
        by_fam[r.family].append(r)
    fam_json = {}
    order = [f[0] for f in SINGLE_FAMILIES] + ["nm_contention"]
    for fam in order:
        rs = by_fam.get(fam, [])
        if not rs:
            continue
        exp = rs[0].expect if fam != "nm_contention" else "mixed"
        if exp == "no_clear":
            behav = pct(sum(1 for r in rs if not r.cleared), len(rs))
        else:
            behav = pct(sum(1 for r in rs if r.cleared), len(rs))
        tf = pct(sum(1 for r in rs if r.set_is_truth), len(rs))
        nfc = sum(1 for r in rs if r.false_clear)
        med = statistics.median(r.elapsed for r in rs)
        fam_json[fam] = {"n": len(rs), "expect": exp, "behaved_pct": behav,
                         "truth_identified_pct": tf, "false_clears": nfc,
                         "median_latency_s": round(med, 4)}
        print(f"{fam:<22} {len(rs):>4} {exp:>9} {str(behav)+'%':>9} "
              f"{str(tf)+'%':>7} {nfc:>6} {med:>7.3f}")

    wr = Counter(r.withheld_reason for r in results if not r.cleared and r.withheld_reason)
    print("\n" + "-" * 74)
    print("WHY IT WITHHELD")
    for reason, n in wr.most_common():
        print(f"  {reason:<28} {n}")

    if fc:
        print("\n" + "!" * 74)
        print("FALSE CLEARS IN DETAIL")
        for r in fc[:25]:
            print(f"  {r.case_id}  conf={r.confidence:.3f} method={r.method}")
            print(f"    matched not true : {sorted(r.matched - r.truth)[:6]}")
            print(f"    true not matched : {sorted(r.truth - r.matched)[:6]}")
    if errors:
        print("\n" + "!" * 74)
        print("EXCEPTIONS")
        for r in errors[:10]:
            print(f"  {r.case_id}\n{r.error}")

    print("=" * 74)

    if args.json:
        payload = {
            "seed": args.seed, "scenarios_scored": total,
            "headline": {
                "false_clears": len(fc), "false_clear_pct": pct(len(fc), total),
                "crashes": len(errors), "double_claims": len(dc),
                "currency_contamination": len(usd_leak),
                "auto_clear_pct": pct(len(cleared), total),
                "auto_clear_correct_pct": pct(
                    sum(1 for r in cleared if r.set_is_truth), len(cleared)),
                "truth_identified_pct": pct(len(truth_found), total),
                "must_not_clear_held_pct": pct(
                    sum(1 for r in must_not if not r.cleared), len(must_not)),
                "should_clear_cleared_pct": pct(
                    sum(1 for r in should if r.cleared), len(should)),
            },
            "calibration": calib, "by_family": fam_json,
            "withheld_reasons": dict(wr),
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote {args.json}")

    return 1 if (fc or errors or dc or usd_leak) else 0


if __name__ == "__main__":
    sys.exit(main())
