"""
Blind adversarial test of the AmongResolver engine, written after it.

Written for the final review, from scratch: its own generator, its own answer
key, nothing taken from the engine's benchmarks. Its first run found two wrong
clears (FAILURE_LOG 46); the results committed beside it are from the engine
as it now stands, on the original seed and on a fresh one.

    python scripts/blind_test.py                    # seed 777001
    python scripts/blind_test.py --seed 20260923    # the original seed
    python scripts/blind_test.py --only 10          # one family, as a smoke run

Results: docs/benchmarks/blind_test_<seed>.json. scripts/enterprise_baseline.py
runs two enterprise-style matchers on the same cases.

Nothing here comes from the engine's own generators or benchmarks. Every
scenario carries its ground truth (the true member set, or "no set ties"),
built by construction and, where arithmetic alone decides, checked by an
independent exhaustive subset count. The engine is driven through the same
HTTP endpoint the website uses (/reconcile/upload with investigate=true),
in-process, with the model OFF so every result is deterministic.

Outcome classes per case
    CLEAR_OK      cleared with exactly the true set
    FALSE_CLEAR   cleared with any other set, or cleared when no set ties
    REFUSED       not cleared (escalated / withheld / no match)
    REJECTED      the file was refused with a 4xx and a reason
    CRASH         5xx or exception

Two "traditional engine" baselines run on the same data:
    T1  one-to-one: a single payment whose amount equals the credit
    T2  rule-based grouping: payments whose reference/memo contains the
        settlement id (case-insensitive), status captured, summed with
        +/-5 paise tolerance; no ledger, no FX, no dedupe
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import string
import sys
import tempfile
import time
import uuid
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["AUDIT_DB_PATH"] = os.path.join(tempfile.gettempdir(), f"blind_{uuid.uuid4().hex}.sqlite3")
os.environ["GEMINI_API_KEY"] = ""          # model OFF: dotenv does not override a set key
os.environ.setdefault("FX_REFERENCE", "0")  # offline: the ECB note is advisory, never an outcome
sys.path.insert(0, os.path.join(ENGINE, "src"))
os.chdir(ENGINE)

import logging  # noqa: E402
logging.disable(logging.WARNING)

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402
import settlement_cycle  # noqa: E402

CLIENT = TestClient(main.app)
TOL = 5
B62 = string.ascii_letters + string.digits
SKU = [49900, 99900, 149900, 199900, 299900, 49900, 99900, 59900, 129900]
T0 = datetime(2026, 9, 17, 11, 30, tzinfo=timezone.utc)   # settlement credit time
SEQ = iter(range(10**9))


# ── generator ────────────────────────────────────────────────────────────────
def rid(rng, prefix, n=14):
    return prefix + "".join(rng.choice(B62) for _ in range(n))


def amount(rng, sku_share=0.0):
    if rng.random() < sku_share:
        return rng.choice(SKU)
    # lognormal around ~Rs 1,800, paise precision
    return max(100, int(rng.lognormvariate(12.1, 0.9)))


def fee_of(g):
    fee = round(g * 0.02)
    return fee, round(fee * 0.18)


def pay(rng, ref, amt, *, ts=None, status="captured", memo="", cur="INR", pid=None):
    ts = ts or (T0 - timedelta(hours=rng.uniform(26, 70)))
    fee, gst = fee_of(abs(amt)) if amt > 0 else (0, 0)
    return {"txn_id": pid or f"pay_{next(SEQ):06d}{rid(rng, '', 8)}", "ref_id": ref,
            "amount": amt, "currency": cur, "timestamp": ts, "status": status,
            "memo": memo, "fee": fee, "tax": gst}


def fmt_amt(minor, cur="INR"):
    return f"{minor / 100:.2f}"


def to_csv(rows, ts_style="iso"):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["txn_id", "ref_id", "amount", "currency", "timestamp", "status", "memo", "fee", "tax"])
    for r in rows:
        a = r["amount"]
        amt = a if isinstance(a, str) else fmt_amt(a)
        ts = r["timestamp"]
        if isinstance(ts, datetime):
            if ts_style == "ist_naive":
                ts = (ts + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
            elif ts_style == "ist_offset":
                ts = (ts + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%dT%H:%M:%S+05:30")
            else:
                ts = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        w.writerow([r["txn_id"], r["ref_id"], amt, r["currency"], ts, r["status"], r["memo"],
                    fmt_amt(r["fee"]), fmt_amt(r["tax"])])
    return buf.getvalue().encode()


class Case:
    def __init__(self, family, idx, rows, members, net, deductions, *, expect, stl,
                 fx="", extra_form=None, ts_style="iso", note="", window_pool=None,
                 truth_ties=True):
        self.family, self.idx, self.rows, self.members = family, idx, rows, set(members)
        self.net, self.deductions, self.expect, self.stl = net, deductions, expect, stl
        self.fx, self.extra_form, self.ts_style, self.note = fx, extra_form or {}, ts_style, note
        self.window_pool, self.truth_ties = window_pool, truth_ties


def settle(members, extra_neg=()):
    gross = sum(m["amount"] for m in members)
    ded = sum(sum(fee_of(m["amount"])) for m in members if m["amount"] > 0)
    return gross + sum(extra_neg) - ded, ded


def stl_id(rng):
    return rid(rng, "setl_", 14)


def named(rng, stl, n, sku_share=0.0):
    return [pay(rng, f"{stl}", amount(rng, sku_share), memo=f"Settlement {stl}") for _ in range(n)]


def decoys(rng, n, sku_share=0.0, named_other=True):
    out = []
    others = [stl_id(rng) for _ in range(max(1, n // 8))]
    for _ in range(n):
        ref = rng.choice(others) if named_other else rid(rng, "order_", 14)
        out.append(pay(rng, ref, amount(rng, sku_share),
                       memo=f"Settlement {ref}" if named_other else "Order"))
    return out


# ── independent ground-truth: count subsets within tolerance (meet in middle) ─
def subset_count(amounts, target, cap=2):
    n = len(amounts)
    if n > 40:
        return None
    h = n // 2
    A, B = amounts[:h], amounts[h:]

    def sums(xs):
        s = [0]
        for x in xs:
            s = s + [v + x for v in s]
        return s
    sa, sb = sums(A), sorted(sums(B))
    count = 0
    for v in sa:
        lo, hi = target - TOL - v, target + TOL - v
        count += bisect_right(sb, hi) - bisect_left(sb, lo)
        if count >= cap:
            return count
    return count


# ── scenario families ────────────────────────────────────────────────────────
def build(seed):
    rng = random.Random(seed)
    cases = []
    R = 12  # seeds per family

    for i in range(R):  # 1 named, no noise
        s = stl_id(rng); m = named(rng, s, rng.randint(3, 40))
        net, ded = settle(m)
        cases.append(Case("01 named_clean", i, m, [x["txn_id"] for x in m], net, ded, expect="clear", stl=s))

    for i in range(R):  # 2 named + many other settlements in the same window
        s = stl_id(rng); m = named(rng, s, rng.randint(5, 30), 0.4)
        d = decoys(rng, rng.randint(20, 150), 0.4)
        net, ded = settle(m)
        rows = m + d; rng.shuffle(rows)
        cases.append(Case("02 named_with_decoys", i, rows, [x["txn_id"] for x in m], net, ded, expect="clear", stl=s))

    for i in range(R):  # 3 settlement id written differently in the feed
        core = f"{rng.randint(2026, 2026)}{rng.randint(1, 12):02d}{rng.randint(1, 28):02d}{rid(rng, '', 4).upper()}"
        s = f"STL-{core[:4]}-{core[4:8]}-{core[8:]}"
        variants = [f"stl{core}".lower(), f"STL/{core[:4]}/{core[4:8]}/{core[8:]}",
                    f"stl_{core[:4]}_{core[4:8]}_{core[8:]}".lower(), f"STL {core[:4]} {core[4:8]} {core[8:]}"]
        v = variants[i % len(variants)]
        m = [pay(rng, v, amount(rng)) for _ in range(rng.randint(4, 20))]
        d = decoys(rng, rng.randint(10, 60))
        net, ded = settle(m); rows = m + d; rng.shuffle(rows)
        cases.append(Case("03 named_mangled_separators", i, rows, [x["txn_id"] for x in m], net, ded,
                          expect="clear", stl=s, note=f"feed writes {v!r}"))

    for i in range(R):  # 4 unnamed, small pool: arithmetic may or may not be unique
        s = stl_id(rng)
        m = [pay(rng, rid(rng, "order_", 14), amount(rng), memo="Order") for _ in range(rng.randint(2, 6))]
        d = [pay(rng, rid(rng, "order_", 14), amount(rng), memo="Order") for _ in range(rng.randint(3, 12))]
        net, ded = settle(m); rows = m + d; rng.shuffle(rows)
        cnt = subset_count([r["amount"] for r in rows], net + ded)
        cases.append(Case("04 unnamed_small_pool", i, rows, [x["txn_id"] for x in m], net, ded,
                          expect="clear_if_unique" if cnt == 1 else "no_clear", stl=s,
                          note=f"{cnt} subset(s) tie in the window"))

    for i in range(R):  # 5 unnamed, fixed-price catalogue: dense ambiguity
        s = stl_id(rng)
        m = [pay(rng, rid(rng, "order_", 14), rng.choice(SKU), memo="Order") for _ in range(rng.randint(4, 10))]
        d = [pay(rng, rid(rng, "order_", 14), rng.choice(SKU), memo="Order") for _ in range(rng.randint(10, 24))]
        net, ded = settle(m); rows = m + d; rng.shuffle(rows)
        cnt = subset_count([r["amount"] for r in rows], net + ded)
        cases.append(Case("05 unnamed_fixed_price_catalogue", i, rows, [x["txn_id"] for x in m], net, ded,
                          expect="clear_if_unique" if cnt == 1 else "no_clear", stl=s,
                          note=f"{cnt}+ subset(s) tie"))

    for i in range(R):  # 6 twin: one member unnamed, two identical unnamed payments could fill it
        s = stl_id(rng); m = named(rng, s, rng.randint(4, 15))
        amt = amount(rng); ts = T0 - timedelta(hours=40)
        a = pay(rng, rid(rng, "order_", 14), amt, ts=ts, memo="Order")
        b = pay(rng, rid(rng, "order_", 14), amt, ts=ts + timedelta(minutes=3), memo="Order")
        members = m + [a]
        net, ded = settle(members); rows = m + [a, b] + decoys(rng, 20); rng.shuffle(rows)
        cases.append(Case("06 twin_interchangeable", i, rows, [x["txn_id"] for x in members], net, ded,
                          expect="no_clear", stl=s, note="two identical unnamed payments; either fills"))

    for i in range(R):  # 7 bank short-paid (unexplained shortfall)
        s = stl_id(rng); m = named(rng, s, rng.randint(4, 25))
        net, ded = settle(m); short = rng.choice([500, 1000, 2500, 10000, 50000])
        rows = m + decoys(rng, 30); rng.shuffle(rows)
        cases.append(Case("07 short_paid", i, rows, [], net - short, ded, expect="no_clear", stl=s,
                          truth_ties=False, note=f"credit short by Rs {short/100:.2f}"))

    for i in range(R):  # 8 over-paid
        s = stl_id(rng); m = named(rng, s, rng.randint(4, 25))
        net, ded = settle(m); over = rng.choice([100, 700, 5000])
        rows = m + decoys(rng, 30); rng.shuffle(rows)
        cases.append(Case("08 over_paid", i, rows, [], net + over, ded, expect="no_clear", stl=s,
                          truth_ties=False, note=f"credit over by Rs {over/100:.2f}"))

    for i in range(R):  # 9 a member missing from the feed, nothing can fill it
        s = stl_id(rng); m = named(rng, s, rng.randint(4, 20))
        net, ded = settle(m); gone = m.pop(rng.randrange(len(m)))
        rows = m + decoys(rng, 25); rng.shuffle(rows)
        cases.append(Case("09 missing_member", i, rows, [], net, ded, expect="no_clear", stl=s,
                          truth_ties=False, note=f"{gone['txn_id']} absent"))

    for i in range(R):  # 10 missing member, but an unnamed payment of the SAME amount could fill it
        s = stl_id(rng); m = named(rng, s, rng.randint(4, 20))
        net, ded = settle(m); gone = m.pop(rng.randrange(len(m)))
        filler = pay(rng, rid(rng, "order_", 14), gone["amount"], memo="Order")
        rows = m + [filler] + decoys(rng, 25); rng.shuffle(rows)
        cases.append(Case("10 missing_member_same_amount_decoy", i, rows, [], net, ded, expect="no_clear",
                          stl=s, truth_ties=False,
                          note="truth: member absent; an unrelated unnamed payment has its exact amount"))

    for i in range(R):  # 11 refunds netted in the settlement
        s = stl_id(rng); m = named(rng, s, rng.randint(5, 20))
        refunds = []
        for _ in range(rng.randint(1, 3)):
            src = rng.choice(m); ra = -min(src["amount"], rng.choice([src["amount"], src["amount"] // 2, 20000]))
            refunds.append(pay(rng, s, ra, status="processed", memo=f"Refund {s}",
                               ts=T0 - timedelta(hours=rng.uniform(20, 30))))
        net, ded = settle(m, [r["amount"] for r in refunds])
        rows = m + refunds + decoys(rng, 30); rng.shuffle(rows)
        cases.append(Case("11 refunds_netted", i, rows, [x["txn_id"] for x in m + refunds], net, ded,
                          expect="clear", stl=s))

    for i in range(R):  # 12 failed / pending / authorized rows naming the settlement
        s = stl_id(rng); m = named(rng, s, rng.randint(5, 20))
        junk = [pay(rng, s, amount(rng), status=rng.choice(["failed", "authorized", "pending", "created"]),
                    memo=f"Settlement {s}") for _ in range(rng.randint(2, 6))]
        net, ded = settle(m); rows = m + junk + decoys(rng, 20); rng.shuffle(rows)
        cases.append(Case("12 non_captured_rows_named", i, rows, [x["txn_id"] for x in m], net, ded,
                          expect="clear", stl=s))

    for i in range(R):  # 13 duplicated export row
        s = stl_id(rng); m = named(rng, s, rng.randint(4, 20))
        net, ded = settle(m); dup = dict(rng.choice(m))
        rows = m + [dup] + decoys(rng, 20); rng.shuffle(rows)
        cases.append(Case("13 duplicate_export_row", i, rows, [x["txn_id"] for x in m], net, ded,
                          expect="clear_or_refuse", stl=s, note=f"{dup['txn_id']} appears twice"))

    for i in range(R):  # 14 double claim across two settlements
        s1, s2 = stl_id(rng), stl_id(rng)
        m1 = named(rng, s1, rng.randint(4, 12)); n1, d1 = settle(m1)
        reused = dict(rng.choice(m1)); reused["ref_id"] = s2; reused["memo"] = f"Settlement {s2}"
        m2 = named(rng, s2, rng.randint(3, 10)) + [reused]; n2, d2 = settle(m2)
        cases.append(Case("14a double_claim_first", i, m1 + decoys(rng, 10), [x["txn_id"] for x in m1], n1, d1,
                          expect="clear", stl=s1))
        cases.append(Case("14b double_claim_second", i, m2 + decoys(rng, 10), [], n2, d2, expect="no_clear",
                          stl=s2, truth_ties=False, note=f"{reused['txn_id']} already settled in {s1}"))

    for i in range(R):  # 15 foreign currency
        s = stl_id(rng); rate = f"{rng.uniform(82.0, 86.0):.4f}"
        inr = named(rng, s, rng.randint(3, 10))
        usd = [pay(rng, s, rng.randint(500, 40000), cur="USD", memo=f"Settlement {s}") for _ in range(rng.randint(1, 3))]
        conv = [dict(u, amount=int((Decimal(u["amount"]) / 100 * Decimal(rate) * 100)
                                   .quantize(Decimal(1), rounding=ROUND_HALF_UP))) for u in usd]
        net, ded = settle(inr)          # deductions declared on the INR legs only
        net += sum(c["amount"] for c in conv)
        rows = inr + usd + decoys(rng, 15)
        ids = [x["txn_id"] for x in inr + usd]
        variant = i % 3
        if variant == 0:
            cases.append(Case("15a fx_rate_declared", i, rows, ids, net, ded, expect="clear", stl=s, fx=f"USD={rate}"))
        elif variant == 1:
            cases.append(Case("15b fx_no_rate", i, rows, [], net, ded, expect="no_clear", stl=s, truth_ties=False,
                              note="USD legs cannot be summed without a rate"))
        else:
            wrong = f"{float(rate) * 1.01:.4f}"
            cases.append(Case("15c fx_wrong_rate", i, rows, [], net, ded, expect="no_clear", stl=s, fx=f"USD={wrong}",
                              truth_ties=False, note="declared rate is 1% off the one used"))

    for i in range(R):  # 16 timestamps in IST, naive or with offset, near midnight
        s = stl_id(rng)
        m = [pay(rng, s, amount(rng), memo=f"Settlement {s}",
                 ts=T0.replace(hour=18, minute=25) - timedelta(days=rng.randint(1, 2), minutes=rng.randint(0, 20)))
             for _ in range(rng.randint(3, 15))]
        net, ded = settle(m); rows = m + decoys(rng, 15)
        cases.append(Case("16 ist_timestamps_near_midnight", i, rows, [x["txn_id"] for x in m], net, ded,
                          expect="clear", stl=s, ts_style="ist_naive" if i % 2 else "ist_offset"))

    for i in range(R):  # 17 same-amount unnamed decoy next to a named member (no swap allowed)
        s = stl_id(rng); m = named(rng, s, rng.randint(4, 15))
        twin = pay(rng, rid(rng, "order_", 14), rng.choice(m)["amount"], memo="Order")
        net, ded = settle(m); rows = m + [twin] + decoys(rng, 20); rng.shuffle(rows)
        cases.append(Case("17 named_member_with_same_amount_stranger", i, rows, [x["txn_id"] for x in m], net, ded,
                          expect="clear", stl=s))

    for i in range(R):  # 18 garbage rows mixed in
        s = stl_id(rng); m = named(rng, s, rng.randint(4, 12))
        bad = [dict(pay(rng, s, 100), amount=v) for v in ["", "abc", "1,00,0.00.1", "NaN"]]
        net, ded = settle(m); rows = m + [bad[i % 4]] + decoys(rng, 10)
        cases.append(Case("18 unreadable_amount_row", i, rows, [x["txn_id"] for x in m], net, ded,
                          expect="clear_or_reject", stl=s, note=f"amount {bad[i % 4]['amount']!r}"))

    for i in range(R // 2):  # 19 no declared deductions, rate card from the form
        s = stl_id(rng); m = named(rng, s, rng.randint(3, 25))
        net, ded = settle(m)
        cases.append(Case("19 fees_estimated_from_rate_card", i, m + decoys(rng, 20), [x["txn_id"] for x in m],
                          net, None, expect="clear_or_refuse", stl=s,
                          extra_form={"gateway_fee_bps": "236", "tax_withholding_bps": "0", "flat_fee_cents": "0"},
                          note="2% + 18% GST per payment vs a 2.36% card: rounding drift may break the tie"))

    for i in range(3):  # 20 scale
        s = stl_id(rng); m = named(rng, s, [150, 400, 800][i]); d = decoys(rng, [1000, 3000, 6000][i])
        net, ded = settle(m); rows = m + d; rng.shuffle(rows)
        cases.append(Case("20 scale", i, rows, [x["txn_id"] for x in m], net, ded, expect="clear", stl=s,
                          note=f"{len(m)} members in {len(rows)} rows"))
    return cases


# ── baselines ────────────────────────────────────────────────────────────────
def baseline_1to1(c):
    target = c.net + (c.deductions or 0)
    hits = [r for r in c.rows if isinstance(r["amount"], int) and r["amount"] in (c.net, target)]
    return {hits[0]["txn_id"]} if len(hits) == 1 else None


def baseline_rule_group(c):
    key = c.stl.lower()
    grp = [r for r in c.rows if isinstance(r["amount"], int) and r["status"] in ("captured", "processed")
           and (key in r["ref_id"].lower() or key in r["memo"].lower())]
    if not grp:
        return None
    total = sum(r["amount"] for r in grp)   # raw minor units; no FX, no dedupe
    if abs(total - (c.net + (c.deductions or 0))) <= TOL:
        return {r["txn_id"] for r in grp}
    return None


def judge(c, cleared_set):
    """What a clear (or a refusal) means against the truth."""
    if cleared_set is None:
        return "REFUSED"
    if c.truth_ties and cleared_set == c.members:
        return "CLEAR_OK"
    return "FALSE_CLEAR"


# ── run ──────────────────────────────────────────────────────────────────────
def run_engine(c):
    settlement_cycle.reset()
    form = {"batch_id": c.stl, "net_amount": f"{c.net / 100:.2f}", "currency": "INR",
            "settled_at": T0.strftime("%Y-%m-%dT%H:%M:%SZ"), "member_source": "gateway",
            "investigate": "true", "fx_rates": c.fx}
    if c.deductions is not None:
        form["declared_deductions"] = f"{c.deductions / 100:.2f}"
    form.update(c.extra_form)
    t = time.perf_counter()
    try:
        r = CLIENT.post("/reconcile/upload", data=form,
                        files={"gateway_file": ("gateway.csv", to_csv(c.rows, c.ts_style))})
    except Exception as e:  # noqa: BLE001
        return {"class": "CRASH", "detail": repr(e)[:300], "secs": time.perf_counter() - t}
    secs = time.perf_counter() - t
    if r.status_code >= 500:
        return {"class": "CRASH", "code": r.status_code, "detail": r.text[:300], "secs": secs}
    if r.status_code >= 400:
        return {"class": "REJECTED", "code": r.status_code, "detail": str(r.json().get("detail"))[:300], "secs": secs}
    j = r.json(); s = j["summary"]
    cleared = bool(s.get("cleared"))
    got = set(j.get("matched_txn_ids") or []) if cleared else None
    inv = j.get("investigation") or {}
    return {"class": judge(c, got), "cleared": cleared, "approval": s.get("requires_human_approval"),
            "ambiguous": s.get("ambiguous"), "withheld": s.get("withheld_reason"), "ties_out": s.get("ties_out"),
            "residual": s.get("tie_out_residual_cents"), "method": s.get("method"),
            "confidence": s.get("calibrated_confidence"),
            "matched": len(j.get("matched_txn_ids") or []), "truth": len(c.members),
            "extra": sorted((got or set()) - c.members)[:5], "missing": sorted(c.members - (got or set()))[:5] if cleared else [],
            "decision": (inv.get("verification") or {}).get("decision") or inv.get("decision"),
            "deciding_question": bool(inv.get("deciding_question")),
            "plain": (j.get("plain_summary") or "")[:220], "secs": round(secs, 3)}


def main():
    ap = argparse.ArgumentParser(description="Blind adversarial test, written after the engine.")
    ap.add_argument("--seed", type=int, default=777001)
    ap.add_argument("--only", default="", help="run only families whose label starts with this")
    ap.add_argument("--json", default="", help="default: docs/benchmarks/blind_test_<seed>.json")
    args = ap.parse_args()
    cases = [c for c in build(args.seed) if c.family.startswith(args.only)]
    out = []
    t_all = time.perf_counter()
    for c in cases:
        e = run_engine(c)
        b1, b2 = baseline_1to1(c), baseline_rule_group(c)
        rec = {"family": c.family, "idx": c.idx, "expect": c.expect, "note": c.note, "rows": len(c.rows),
               "members": len(c.members), "engine": e, "T1": judge(c, b1), "T2": judge(c, b2)}
        out.append(rec)
        print(f"{c.family:42s} #{c.idx:<2d} exp={c.expect:16s} eng={e['class']:11s} "
              f"T1={rec['T1']:11s} T2={rec['T2']:11s} {e.get('secs', 0):6.2f}s "
              f"{(e.get('withheld') or e.get('detail') or '')[:70]}", flush=True)
    result = {"seed": args.seed, "cases_run": len(out), "totals": totals(out), "cases": out,
              "total_secs": round(time.perf_counter() - t_all, 1)}
    print(json.dumps(result["totals"]))
    if args.only:
        return      # a smoke run is not a result
    path = args.json or os.path.join(ENGINE, "docs", "benchmarks", f"blind_test_{args.seed}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, default=str)


def totals(out: list[dict]) -> dict:
    """Outcome counts for the engine and the two baselines."""
    count = lambda xs: {k: xs.count(k) for k in ("CLEAR_OK", "FALSE_CLEAR", "REFUSED", "REJECTED", "CRASH")}
    return {"engine": count([r["engine"]["class"] for r in out]),
            "one_to_one": count([r["T1"] for r in out]),
            "rule_grouping": count([r["T2"] for r in out])}


if __name__ == "__main__":
    main()
