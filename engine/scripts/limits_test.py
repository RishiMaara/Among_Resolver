#!/usr/bin/env python3
"""
Where the engine stops: each dimension pushed until it can no longer clear.

A stress test that clears is a lower bound. This pushes one thing at a time
past the point where the engine can still prove an answer, and records, for
every case, the correct answer beside what the engine returned. Past its edge
the engine is expected to refuse, never to clear a wrong set; a wrong clear
anywhere here is a failure of the engine, not of the test.

  members     payments in one payout, every one naming the settlement, in a
              pool of 5,000 other settlements' payments
  no_refs     payouts whose payments carry no reference at all, in pools where
              exactly one set adds up (checked by counting every subset)
  fee_drift   deductions not declared: the target is estimated from the rate
              card, and each payment's fee was rounded on its own, so the
              error grows with the member count
  tolerance   a credit short by a few paise, around the 5-paise tolerance

The pool-size ceiling (records in one file) is run_scale_proof.py --records N,
and docs/benchmarks/limits.json also records the hosted engine's request limit.

    python scripts/limits_test.py                       # everything
    python scripts/limits_test.py --only members
    python scripts/limits_test.py --json docs/benchmarks/limits.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import tempfile
import time
import uuid
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone

ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AUDIT_DB_PATH", os.path.join(tempfile.gettempdir(), f"limits_{uuid.uuid4().hex}.sqlite3"))
os.environ["GEMINI_API_KEY"] = ""
sys.path.insert(0, os.path.join(ENGINE, "src"))
logging.disable(logging.WARNING)

import settlement_cycle  # noqa: E402
from fee_decomposition import FeeRateCard  # noqa: E402
from pipeline import reconcile_settlement  # noqa: E402
from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence  # noqa: E402

T0 = datetime(2026, 9, 17, 11, 30, tzinfo=timezone.utc)
TOL = 5


def _txn(rng: random.Random, txn_id: str, ref: str, amount: int) -> NormalizedTxn:
    return NormalizedTxn(source=SourceType.GATEWAY, source_txn_id=txn_id,
                         ref_id_canonical="".join(ch for ch in ref if ch.isalnum()).upper(),
                         amount_cents=amount, currency="INR",
                         timestamp_utc=T0 - timedelta(hours=rng.uniform(26, 70)),
                         tz_confidence=TzConfidence.HIGH, extra={"ref_raw": ref})


def _amount(rng: random.Random) -> int:
    return max(100, int(rng.lognormvariate(12.1, 0.9)))


def _fee(gross: int) -> int:
    fee = round(gross * 0.02)
    return fee + round(fee * 0.18)          # 2% plus 18% GST on the fee, per payment


def _run(batch: SettlementBatch, pool: list, truth: set[str] | None, rate_card=None) -> dict:
    settlement_cycle.reset()
    t = time.perf_counter()
    try:
        kwargs = {"rate_card": rate_card} if rate_card else {}
        report = reconcile_settlement(batch, pool, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a crash is a result here
        return {"outcome": "CRASH", "detail": f"{type(exc).__name__}: {exc}"[:200],
                "secs": round(time.perf_counter() - t, 2)}
    m = report.match_result
    got = set(m.matched_txn_ids) if m.cleared else None
    if got is None:
        outcome = "REFUSED"
    elif truth is not None and got == truth:
        outcome = "CLEARED_RIGHT"
    else:
        outcome = "CLEARED_WRONG"
    return {"outcome": outcome, "withheld_reason": m.withheld_reason,
            "matched": len(m.matched_txn_ids), "truth": len(truth) if truth else 0,
            "residual_paise": report.tie_out_residual_cents, "confidence": round(m.confidence, 3),
            "secs": round(time.perf_counter() - t, 2),
            "reason": m.reasoning[:240]}


def members(sizes) -> list[dict]:
    out = []
    for n in sizes:
        rng = random.Random(n)
        stl = f"STL{n}LIM{rng.randint(10**5, 10**6)}"
        mem = [_txn(rng, f"m{n}_{i}", f"{stl}-{i}", _amount(rng)) for i in range(n)]
        others = [_txn(rng, f"o{n}_{i}", f"STLX{i % 97}Q{rng.randint(10**5, 10**6)}", _amount(rng))
                  for i in range(5000)]
        gross = sum(t.amount_cents for t in mem)
        ded = sum(_fee(t.amount_cents) for t in mem)
        batch = SettlementBatch(batch_id=stl, net_amount_cents=gross - ded, currency="INR",
                                settled_at_utc=T0, declared_deductions_cents=ded,
                                member_source=SourceType.GATEWAY)
        r = _run(batch, mem + others, {t.source_txn_id for t in mem})
        out.append({"members": n, "pool": n + 5000, **r})
        print(f"members {n:>6}  pool {n + 5000:>6}  -> {r['outcome']:14s} {r['secs']:7.2f}s  "
              f"{r.get('withheld_reason') or ''}", flush=True)
    return out


def _subset_count(amounts: list[int], target: int, cap: int = 2) -> int:
    h = len(amounts) // 2

    def sums(xs):
        s = [0]
        for x in xs:
            s = s + [v + x for v in s]
        return s
    sa, sb = sums(amounts[:h]), sorted(sums(amounts[h:]))
    n = 0
    for v in sa:
        n += bisect_right(sb, target + TOL - v) - bisect_left(sb, target - TOL - v)
        if n >= cap:
            break
    return n


def no_refs(pools) -> list[dict]:
    out = []
    for size in pools:
        rng = random.Random(1000 + size)
        for attempt in range(200):
            pool = [_txn(rng, f"n{size}_{attempt}_{i}", f"order_{rng.randint(10**9, 10**10)}", _amount(rng))
                    for i in range(size)]
            k = max(2, size // 4)
            mem = rng.sample(pool, k)
            target = sum(t.amount_cents for t in mem)
            if _subset_count([t.amount_cents for t in pool], target) == 1:
                break
        batch = SettlementBatch(batch_id=f"NOREF{size}X{attempt}", net_amount_cents=target, currency="INR",
                                settled_at_utc=T0, declared_deductions_cents=0,
                                member_source=SourceType.GATEWAY)
        r = _run(batch, pool, {t.source_txn_id for t in mem})
        out.append({"pool": size, "members": k, "unique_by_count": True, **r})
        print(f"no_refs pool {size:>3} ({k} members, the only set that adds up) -> {r['outcome']:14s} "
              f"{r.get('withheld_reason') or ''}", flush=True)
    return out


def fee_drift(sizes) -> list[dict]:
    out = []
    card = FeeRateCard(gateway_fee_bps=236, flat_fee_cents=0, tax_withholding_bps=0)
    for n in sizes:
        rng = random.Random(7000 + n)
        stl = f"FEE{n}DRIFT{rng.randint(10**5, 10**6)}"
        mem = [_txn(rng, f"f{n}_{i}", f"{stl}-{i}", _amount(rng)) for i in range(n)]
        gross = sum(t.amount_cents for t in mem)
        net = gross - sum(_fee(t.amount_cents) for t in mem)
        estimate = round(net / (1 - 236 / 10000))
        batch = SettlementBatch(batch_id=stl, net_amount_cents=net, currency="INR", settled_at_utc=T0,
                                declared_deductions_cents=None, member_source=SourceType.GATEWAY)
        r = _run(batch, mem, {t.source_txn_id for t in mem}, rate_card=card)
        out.append({"members": n, "estimate_error_paise": estimate - gross, **r})
        print(f"fee_drift {n:>4} members, rate-card estimate off by {estimate - gross:+6d} paise -> "
              f"{r['outcome']:14s}", flush=True)
    return out


def tolerance(shorts) -> list[dict]:
    out = []
    for short in shorts:
        rng = random.Random(9000 + short)
        stl = f"TOL{short}P{rng.randint(10**5, 10**6)}"
        mem = [_txn(rng, f"t{short}_{i}", f"{stl}-{i}", _amount(rng)) for i in range(20)]
        gross = sum(t.amount_cents for t in mem)
        ded = sum(_fee(t.amount_cents) for t in mem)
        batch = SettlementBatch(batch_id=stl, net_amount_cents=gross - ded - short, currency="INR",
                                settled_at_utc=T0, declared_deductions_cents=ded,
                                member_source=SourceType.GATEWAY)
        r = _run(batch, mem, {t.source_txn_id for t in mem})
        out.append({"credit_short_paise": short, **r})
        print(f"tolerance: credit short by {short} paise -> {r['outcome']:14s} residual "
              f"{r.get('residual_paise')}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--members", default="1000,2000,5000,10000,15000,20000,25000,50000,100000,200000",
                    help="payout sizes to push, comma-separated")
    args = ap.parse_args()
    runs = {
        "tolerance": lambda: tolerance([0, 3, 5, 6, 10, 100]),
        "fee_drift": lambda: fee_drift([10, 25, 50, 100, 200, 400, 800, 1600]),
        "no_refs": lambda: no_refs([4, 6, 8, 12, 16, 20, 24]),
        "members": lambda: members([int(x) for x in args.members.split(",")]),
    }
    result = {}
    for name, fn in runs.items():
        if args.only and name != args.only:
            continue
        result[name] = fn()
    wrong = sum(1 for rows in result.values() for r in rows if r["outcome"] in ("CLEARED_WRONG", "CRASH"))
    result["wrong_clears_or_crashes"] = wrong
    print(f"\nwrong clears or crashes anywhere: {wrong}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)


if __name__ == "__main__":
    main()
