"""
An enterprise-style matcher, given every advantage such tools are normally
configured with, run on the SAME blind cases as AmongResolver (blind_test.py).
A model of how rule-based tools are typically configured, not any vendor's code.

    python scripts/enterprise_baseline.py   # reads docs/benchmarks/blind_test_*.json

E-rules  (rule-based auto-match, BlackLine / ARCS style)
  - status filter (captured, processed), unique-ID dedupe
  - persisted matched-item ledger: an item matched once is never reused
  - reference key: settlement id, normalised (alphanumeric, upper case),
    found in the reference or memo; many-to-one group by that key
  - FX via a rate table when the settlement declares a rate
  - target = net + declared deductions, or the rate-card gross-up
  - tolerance +/-5 paise; a key group that does not tie is an exception
E-auto   (E-rules, plus amount-based sum matching, cash-application style)
  - when NO item carries the key, auto-match any open in-window subset that
    sums to the target, oldest first (FIFO), and clear it
"""
import json
import os
import sys
from bisect import bisect_left, bisect_right
from decimal import ROUND_HALF_UP, Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import blind_test as blind  # noqa: E402  (same cases, same answer key)

BENCH = os.path.join(os.path.dirname(HERE), "docs", "benchmarks")

TOL = 5
norm = lambda s: "".join(ch for ch in (s or "") if ch.isalnum()).upper()


def target_of(c):
    if c.deductions is not None:
        return c.net + c.deductions
    bps = int(c.extra_form.get("gateway_fee_bps", 300)) + int(c.extra_form.get("tax_withholding_bps", 0))
    return round(c.net / (1 - bps / 10000))


def open_items(c, ledger):
    seen, out = set(), []
    for r in c.rows:
        if not isinstance(r["amount"], int) or r["amount"] == 0:
            continue                                   # import rejects unreadable rows
        if r["status"] not in ("captured", "processed") or r["txn_id"] in seen or r["txn_id"] in ledger:
            continue
        seen.add(r["txn_id"]); out.append(r)
    return out


def inr(r, c):
    if r["currency"] == "INR":
        return r["amount"]
    rates = dict(p.split("=") for p in c.fx.split(",") if "=" in p)
    if r["currency"] not in rates:
        return None
    return int((Decimal(r["amount"]) / 100 * Decimal(rates[r["currency"]]) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def fifo_subset(items, target):
    """Oldest-first subset that ties, by meet in the middle (pools up to 36)."""
    items = sorted(items, key=lambda r: r["timestamp"])
    if len(items) > 36:
        return None
    h = len(items) // 2
    A, B = items[:h], items[h:]

    def sums(xs):
        out = [(0, 0)]
        for i, x in enumerate(xs):
            out += [(s + x["_inr"], m | (1 << i)) for s, m in out]
        return out
    sb = sorted(sums(B))
    keys = [s for s, _ in sb]
    best = None
    for s, ma in sums(A):
        lo, hi = bisect_left(keys, target - TOL - s), bisect_right(keys, target + TOL - s)
        for k in range(lo, hi):
            mb = sb[k][1]
            chosen = [A[i] for i in range(len(A)) if ma >> i & 1] + [B[i] for i in range(len(B)) if mb >> i & 1]
            newest = max(r["timestamp"] for r in chosen)
            if best is None or newest < best[0]:
                best = (newest, chosen)
    return best[1] if best else None


def run(c, ledger, auto):
    items = []
    for r in open_items(c, ledger):
        v = inr(r, c)
        if v is not None:
            items.append(dict(r, _inr=v))
    key = norm(c.stl)
    group = [r for r in items if key and (key in norm(r["ref_id"]) or key in norm(r["memo"]))]
    target = target_of(c)
    if group:
        if abs(sum(r["_inr"] for r in group) - target) <= TOL:
            return {r["txn_id"] for r in group}
        return None                                    # key group does not tie: exception
    if auto:
        chosen = fifo_subset(items, target)
        return {r["txn_id"] for r in chosen} if chosen else None
    return None


def evaluate(seed, engine_json):
    blind.SEQ = iter(range(10**9))
    cases = blind.build(seed)
    with open(os.path.join(BENCH, engine_json), encoding="utf-8") as f:
        eng = json.load(f)["cases"]
    assert len(eng) == len(cases)
    out = {}
    for name, auto in (("E-rules", False), ("E-auto", True)):
        ledger, res = set(), []
        for c in cases:
            got = run(c, ledger, auto)
            if got:
                ledger |= got
            res.append(blind.judge(c, got))
        out[name] = res
    out["AmongResolver"] = [e["engine"]["class"] for e in eng]
    fam = {}
    for i, c in enumerate(cases):
        f = fam.setdefault(c.family, {k: {"CLEAR_OK": 0, "FALSE_CLEAR": 0, "REFUSED": 0, "REJECTED": 0} for k in out})
        for k in out:
            f[k][out[k][i] if out[k][i] in f[k] else "REFUSED"] += 1
    totals = {k: {x: v.count(x) for x in ("CLEAR_OK", "FALSE_CLEAR", "REFUSED", "REJECTED")} for k, v in out.items()}
    return totals, fam


if __name__ == "__main__":
    report = {}
    for seed in (20260923, 777001):
        fn = f"blind_test_{seed}.json"
        totals, fam = evaluate(seed, fn)
        report[seed] = {"totals": totals, "families": fam}
        print(f"\n=== seed {seed} ({fn}) ===")
        for k, v in totals.items():
            print(f"  {k:14s} correct clears {v['CLEAR_OK']:3d}  FALSE CLEARS {v['FALSE_CLEAR']:3d}  sent to a person {v['REFUSED']:3d}  file rejected {v['REJECTED']}")
        print("  family                                     AmongResolver   E-rules   E-auto   (ok/false)")
        for f, v in fam.items():
            cell = lambda k: f"{v[k]['CLEAR_OK']:2d}/{v[k]['FALSE_CLEAR']:<2d}"
            print(f"  {f:42s} {cell('AmongResolver'):>8s}     {cell('E-rules'):>6s}   {cell('E-auto'):>6s}")
    with open(os.path.join(BENCH, "enterprise_comparison.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)
