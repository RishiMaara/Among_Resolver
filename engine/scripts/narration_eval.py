#!/usr/bin/env python3
"""
Does a model read bank narrations better than rules — and does it make things up?

THE SET
-------
Narrations generated from nine bank formats, seeded so every run sees the
same ones. Four are the formats the regex reader was written against
("dev"); five it never saw ("held_out"). The model is shown neither — no
examples, only the instruction. Damage is what real statements do: cut at
40-70 characters, lower-cased, separators stripped, remarks dropped.

A field's truth is only what SURVIVES in the damaged text. A UTR cut in half
is not recoverable, so the right answer for it is "none", and a reader that
returns the half — or a whole one it invented — is scored wrong.

WHAT IS REPORTED
----------------
Per field and split: accuracy (right value, or rightly none), wrong values,
misses. For the model, values that were not in the narration at all and were
dropped by the grounding rule are counted on their own — the rate at which
it would have invented evidence had nothing checked.

The model run spends the GEMINI_API_KEY holder's quota: about one call per
25 narrations (ten calls for the default 240).

From engine/:
    python scripts/narration_eval.py                 # regex only
    python scripts/narration_eval.py --llm --json docs/benchmarks/narration_eval.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import narration_reader as nr  # noqa: E402

GATEWAYS = ["RAZORPAY SOFTWARE PVT LTD", "RAZORPAY PAYMENTS PVT LTD",
            "CASHFREE PAYMENTS INDIA PVT LTD", "PAYU PAYMENTS PVT LTD"]
OTHERS = ["SHARMA TRADERS", "KAVERI ENTERPRISES", "ANJALI MEHTA", "NILGIRI FOODS LLP"]
BANKS = ["HDFC", "ICIC", "SBIN", "UTIB", "YESB", "KKBK", "CNRB", "FDRL"]


def utr16(rng):
    return rng.choice(BANKS) + "N" + "".join(rng.choice("0123456789") for _ in range(11))


def utr22(rng):
    return rng.choice(BANKS) + "R" + "".join(rng.choice("0123456789") for _ in range(17))


def rrn(rng):
    return str(rng.randrange(10**11, 10**12))


def ifsc(rng):
    return rng.choice(BANKS) + "0" + "".join(rng.choice("0123456789") for _ in range(6))


def setl(rng):
    kind = rng.random()
    if kind < 0.5:
        return "setl_" + "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz0123456789")
                                 for _ in range(14))
    if kind < 0.8:
        return f"SETTLE-{rng.randrange(1, 9999):04d}"
    return None


def remark(ref):
    return f"SETTL {ref}" if ref else "PAYMENT"


# Each template returns (text, truth). truth holds what the reader should find.
def t_hdfc(rng):
    u, n, r, i = utr16(rng), rng.choice(GATEWAYS + OTHERS), setl(rng), ifsc(rng)
    return f"NEFT CR-{i}-{n}-{remark(r)}-{u}", {"utr": u, "settlement_ref": r, "counterparty": n, "rail": "NEFT"}


def t_icici(rng):
    u, n, r = rrn(rng), rng.choice(GATEWAYS + OTHERS), setl(rng)
    return f"MMT/IMPS/{u}/{remark(r)}/{n}", {"utr": u, "settlement_ref": r, "counterparty": n, "rail": "IMPS"}


def t_sbi(rng):
    u, n = utr22(rng), rng.choice(GATEWAYS + OTHERS)
    return f"BY TRANSFER-RTGS UTR NO: {u}-{n}", {"utr": u, "settlement_ref": None, "counterparty": n, "rail": "RTGS"}


def t_upi(rng):
    u, n = rrn(rng), rng.choice(OTHERS)
    vpa = n.split()[0].lower() + "@okhdfcbank"
    return f"UPI/{u}/{n}/{vpa}/HDFC BANK", {"utr": u, "settlement_ref": None, "counterparty": n, "rail": "UPI"}


def t_kotak(rng):
    u, n, r = utr16(rng), rng.choice(GATEWAYS + OTHERS), setl(rng)
    return f"NEFT INWARD/{u}/{n}/{remark(r)}", {"utr": u, "settlement_ref": r, "counterparty": n, "rail": "NEFT"}


def t_axis(rng):
    u, n, i = utr16(rng), rng.choice(GATEWAYS + OTHERS), ifsc(rng)
    short = n[:15].strip()
    return f"NEFT/{u}/{short}/{i}", {"utr": u, "settlement_ref": None, "counterparty": short, "rail": "NEFT"}


def t_canara(rng):
    u, n, r = utr16(rng), rng.choice(GATEWAYS + OTHERS), setl(rng)
    return f"NEFT CREDIT {n} {u} {remark(r)}", {"utr": u, "settlement_ref": r, "counterparty": n, "rail": "NEFT"}


def t_yes(rng):
    u, n, r = rrn(rng), rng.choice(GATEWAYS + OTHERS), setl(rng)
    return f"IMPS-CR-{u}-{n}-{remark(r)}", {"utr": u, "settlement_ref": r, "counterparty": n, "rail": "IMPS"}


def t_federal(rng):
    u, n = rrn(rng), rng.choice(OTHERS)
    return f"MOB-IMPS-CR/{n}/{u}", {"utr": u, "settlement_ref": None, "counterparty": n, "rail": "IMPS"}


DEV = [t_hdfc, t_icici, t_sbi, t_upi]
HELD_OUT = [t_kotak, t_axis, t_canara, t_yes, t_federal]


def damage(text, rng):
    if rng.random() < 0.35:
        text = text[:rng.randrange(40, 71)]
    if rng.random() < 0.25:
        text = text.lower()
    if rng.random() < 0.25:
        text = text.replace("-", " ").replace("/", " ")
    if rng.random() < 0.15:
        text = re.sub(r"\s+", "", text)
    return text


def surviving(truth, text):
    """Only what the damaged text still contains is a fair thing to expect."""
    out = {}
    for f, v in truth.items():
        if v is None:
            out[f] = None
        elif f == "rail":
            out[f] = v if nr._rail_in(v, text) else None
        elif f == "counterparty":
            # A name cut short is still a name; its surviving prefix is the truth.
            norm_text, norm_v = nr._norm(text), nr._norm(v)
            keep = ""
            for k in range(len(norm_v), 4, -1):
                if norm_v[:k] in norm_text:
                    keep = norm_v[:k]
                    break
            out[f] = keep or None
        else:
            out[f] = v if nr.grounded(v, text) else None
    return out


def build(n_per_split, seed):
    rng = random.Random(seed)
    items = []
    for split, templates in (("dev", DEV), ("held_out", HELD_OUT)):
        for k in range(n_per_split):
            text, truth = rng.choice(templates)(rng)
            text = damage(text, rng)
            items.append({"split": split, "text": text, "truth": surviving(truth, text)})
    return items


def score(items, preds):
    out = {}
    for split in ("dev", "held_out"):
        rows = [(it, p) for it, p in zip(items, preds) if it["split"] == split]
        fields = {}
        for f in nr.FIELDS:
            right = wrong = missed = 0
            for it, p in rows:
                t, v = it["truth"][f], p.get(f)
                nt, nv = (nr._norm(t) if t else ""), (nr._norm(v) if v else "")
                if nt == nv:
                    right += 1
                elif nv and not nt:
                    wrong += 1
                elif nt and not nv:
                    missed += 1
                else:
                    wrong += 1
            fields[f] = {"accuracy": round(right / len(rows), 4), "wrong": wrong, "missed": missed}
        out[split] = {"n": len(rows), "fields": fields,
                      "mean_accuracy": round(sum(v["accuracy"] for v in fields.values()) / len(fields), 4)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--n", type=int, default=120, help="narrations per split")
    ap.add_argument("--seed", type=int, default=20260921)
    ap.add_argument("--json", default="")
    ap.add_argument("--cache", default="",
                    help="reuse the model's answers from this file (and save them to it), "
                         "so re-scoring does not spend quota again")
    args = ap.parse_args()
    if args.llm:
        try:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(HERE, "..", ".env"), override=False)
        except ImportError:
            pass

    items = build(args.n, args.seed)
    texts = [it["text"] for it in items]
    result = {"narrations": len(items), "seed": args.seed,
              "regex": score(items, [nr.regex_read(t).__dict__ for t in texts])}

    if args.llm:
        stats = {}
        model_only = None
        if args.cache and os.path.exists(args.cache):
            with open(args.cache, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("texts") == texts:
                model_only = [nr.Narration(**m) for m in cached["answers"]]
                stats = cached["stats"]
        if model_only is None:
            model_only = nr.llm_read(texts, stats)
            if model_only is not None and args.cache:
                with open(args.cache, "w", encoding="utf-8") as f:
                    json.dump({"texts": texts, "stats": stats,
                               "answers": [m.__dict__ for m in model_only]}, f)
        if model_only is None:
            print("No model configured (GEMINI_API_KEY); regex only.")
        else:
            # Regex first, the model filling only what it left empty — the
            # product's order — built from the one model pass already made.
            combined = []
            for t, m in zip(texts, model_only):
                row = nr.regex_read(t).__dict__.copy()
                for f in nr.FIELDS:
                    if row[f] is None and getattr(m, f) is not None:
                        row[f] = getattr(m, f)
                combined.append(row)
            # The other order: the grounded model answer first, the regex only
            # where the model found nothing. What the product does when a model
            # is configured, because this arm measured best.
            model_first = []
            for t, m in zip(texts, model_only):
                rx = nr.regex_read(t).__dict__
                model_first.append({f: (getattr(m, f) if getattr(m, f) is not None else rx[f])
                                    for f in nr.FIELDS})
            result["model_then_regex"] = score(items, model_first)
            result["model_only"] = score(items, [m.__dict__ for m in model_only])
            result["regex_then_model"] = score(items, combined)
            result["model_ungrounded_values_dropped"] = stats.get("ungrounded", 0)
            result["model_calls"] = stats.get("calls", 0)
            result["model"] = nr.llm_provider.DEFAULT_MODEL

    print(json.dumps({k: v for k, v in result.items()}, indent=1))
    if args.json:
        result["examples"] = [{"split": it["split"], "text": it["text"], "truth": it["truth"]}
                              for it in items[:10] + items[args.n:args.n + 10]]
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
