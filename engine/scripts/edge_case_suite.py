#!/usr/bin/env python3
"""
Fifty deliberately awkward settlements, run against a live engine.

WHY THIS EXISTS
---------------
The benchmarks measure accuracy on data shaped like the problem. This measures
BEHAVIOUR on data shaped like the world: a file whose header row sits under a
title banner, a merchant whose every sale is the same price, a settlement net
of refunds, a feed carrying payments that never succeeded.

Three of the worst faults this project has had were found exactly this way and
by nothing else — failed payments entering the pool as spendable money,
refunds making almost every settlement ambiguous, and a crash on an unreadable
amount where a plain-English rejection was intended. None of them showed up in
259 unit tests, because the tests asked the questions their author had already
thought of.

WHAT IT ASSERTS
---------------
Each case declares what SHOULD happen — cleared, withheld, or rejected — and
often which agent must have fired. A case that clears when it should decline
is a false clear, which is the one failure this engine exists to prevent, so
those are reported separately and loudly.

It also tracks which of the twenty agents fired across the whole run, because
an agent nothing exercises is an agent nobody has checked.

    python scripts/edge_case_suite.py            # against localhost:8001
    python scripts/edge_case_suite.py --url ...  # against anything else
"""

from __future__ import annotations

import argparse
import csv
import io
import itertools
import json
import sys
import random
import re
import string
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field

BASE = "http://127.0.0.1:8001"

HEAD = ["txn_id", "ref_id", "amount", "currency", "timestamp", "status",
        "payer_id", "payee_id", "memo", "is_cash", "is_wire_transfer"]


# Set once per run. Without it every fixture reuses ids like "p0000", the
# first case to clear consumes them in the settled ledger, and every later
# case is refused as a double-claim — eight false mismatches on the first
# run, all of them the guard working correctly on sloppy fixture ids.
#
# LETTERS ONLY, and deliberately not derived from the timestamp in the batch
# id. The first version used r<epoch>_, which put the same digits in both the
# transaction ids and the batch id — and since a blank ref_id falls back to
# the txn_id, linkage then found a shared token and reported
# "[settlement_id_anchor] 10 transaction(s) reference settlement ... directly"
# on a fixture built to have NO reference at all. The no-references case
# cleared, and it looked like a false clear by the engine when it was the
# harness handing it an accidental anchor.
RUN_TOKEN = ""

# Unique across CASES as well as runs.
#
# The run token alone was not enough: most fixtures call sales() with the
# default prefix, so case 1 and case 26 both produced p0000..p0009. Case 1
# cleared, the settled ledger recorded those payments as spent, and fourteen
# later cases were refused with already_settled_elsewhere — the double-claim
# guard working exactly as designed on ids that should never have repeated.
# Every row now carries its own number.
_SEQ = itertools.count(1)


def row(tid, ref, amount, *, currency="INR", ts="2026-08-17T10:00:00Z",
        status="captured", payer="cust_1", payee="merch_1", memo="Order",
        cash="false", wire="false"):
    return {"txn_id": f"{RUN_TOKEN}{next(_SEQ):05d}{tid}", "ref_id": ref, "amount": amount, "currency": currency,
            "timestamp": ts, "status": status, "payer_id": payer,
            "payee_id": payee, "memo": memo, "is_cash": cash,
            "is_wire_transfer": wire}


def uid(stem: str) -> str:
    """A globally unique id for fixtures that build rows as raw dicts."""
    return f"{RUN_TOKEN}{next(_SEQ):05d}{stem}"


def to_csv(rows, header=None) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header or HEAD, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def sales(n, ref, amount="1000.00", *, prefix="p", hour=10, **kw):
    """n identical-ish sales, spread across the hour so ordering is stable."""
    return [row(f"{prefix}{i:04d}", ref, amount,
                ts=f"2026-08-17T{hour:02d}:{i % 60:02d}:00Z", **kw)
            for i in range(n)]


# ── a case ────────────────────────────────────────────────────────────────

@dataclass
class Case:
    name: str
    probes: str                      # what edge this is aimed at
    body: str                        # the CSV (or JSON) payload
    net: str                         # net_amount on the form
    deductions: str = ""
    window: int = 5
    settled_at: str = "2026-08-19"
    currency: str = "INR"
    filename: str = "feed.csv"
    expect: str = "any"              # cleared | withheld | rejected | any
    expect_agents: tuple = ()        # agents that MUST have fired
    note: str = ""


def build_cases() -> list[Case]:
    C: list[Case] = []
    a = C.append

    # ── Agent 0: reading files nobody designed for us ─────────────────────
    a(Case("canonical headers", "Agent 0 baseline",
           to_csv(sales(10, "EC01")), "9700.00", "300.00", expect="cleared",
           expect_agents=("file_agent", "ingestion", "linkage", "subset_sum")))

    a(Case("razorpay-style headers", "Agent 0 synonym mapping",
           to_csv([{"payment_id": uid(f"pay{i}"), "order_id": "EC02",
                    "amount": "1000.00", "currency": "INR",
                    "created_at": f"2026-08-17T10:{i:02d}:00Z"} for i in range(10)],
                  header=["payment_id", "order_id", "amount", "currency", "created_at"]),
           "9700.00", "300.00", expect="cleared"))

    a(Case("bank UTR headers", "Agent 0 on a bank statement",
           to_csv([{"utr": uid(f"UTR{i:05d}"), "bank_ref_num": "EC03",
                    "credit": "1000.00", "currency": "INR",
                    "value_date": "2026-08-17", "remitter": "ACME LTD"}
                   for i in range(10)],
                  header=["utr", "bank_ref_num", "credit", "currency",
                          "value_date", "remitter"]),
           "9700.00", "300.00", expect="cleared"))

    a(Case("SAP field names", "Agent 0 on an ERP export",
           to_csv([{"document_no": uid(f"DOC{i}"), "belnr": "EC04", "wrbtr": "1000.00",
                    "waers": "INR", "budat": "20260817", "lifnr": f"VEND{i}"}
                   for i in range(10)],
                  header=["document_no", "belnr", "wrbtr", "waers", "budat", "lifnr"]),
           "9700.00", "300.00", expect="cleared"))

    a(Case("spaced and capitalised headers", "Agent 0 normalisation",
           to_csv([{"Transaction ID": uid(f"T{i}"), "Bank Ref Num": "EC05",
                    "Net Amt": "1000.00", "Currency": "INR",
                    "Transaction Date": f"2026-08-17T10:{i:02d}:00Z"} for i in range(10)],
                  header=["Transaction ID", "Bank Ref Num", "Net Amt",
                          "Currency", "Transaction Date"]),
           "9700.00", "300.00", expect="cleared"))

    a(Case("indian lakh grouping", "amount scrubbing",
           to_csv([row(f"lk{i}", "EC06", "₹1,00,000.00") for i in range(3)]),
           "291000.00", "9000.00", expect="cleared"))

    a(Case("accounting negatives in brackets", "amount scrubbing",
           to_csv(sales(10, "EC07") + [row("rf0", "EC07", "(500.00)",
                                           ts="2026-08-17T18:00:00Z", memo="Refund")]),
           "9215.00", "285.00", expect="cleared"))

    a(Case("four date formats in one column", "timestamp parsing",
           to_csv([row("d0", "EC08", "1000.00", ts="17/08/2026"),
                   row("d1", "EC08", "1000.00", ts="2026-08-17T10:00:00Z"),
                   row("d2", "EC08", "1000.00", ts="Aug 17 2026"),
                   row("d3", "EC08", "1000.00", ts="2026-08-17")]),
           "3880.00", "120.00"))

    a(Case("epoch timestamp", "known gap: epochs are dropped",
           to_csv(sales(9, "EC09") + [row("ep0", "EC09", "1000.00", ts="1755417600")]),
           "9700.00", "300.00",
           note="the epoch row should be DROPPED with a reason, not silently kept"))

    a(Case("header buried under a title preamble", "known gap: preamble",
           "ACME CORP LEDGER EXPORT\nGenerated 2026-08-17\n\n"
           + to_csv(sales(10, "EC10")),
           "9700.00", "300.00", expect="rejected",
           note="real ERP exports do this constantly"))

    a(Case("two columns named amount", "duplicate header names",
           "txn_id,ref_id,amount,amount,currency,timestamp\n"
           + "\n".join(f"dq{i},EC11,1000.00,999.99,INR,2026-08-17T10:{i:02d}:00Z"
                       for i in range(10)),
           "9700.00", "300.00",
           note="must WARN that the rightmost column won"))

    a(Case("composite key (id + line no)", "one payment over several lines",
           "trans_id,trans_line_no,ref_id,amount,currency,timestamp\n"
           + "\n".join(f"AD134,{i},EC12,1000.00,INR,2026-08-17T10:{i:02d}:00Z"
                       for i in range(10)),
           "9700.00", "300.00",
           note="10 rows must remain 10 identities, not collapse to 1"))

    a(Case("ragged rows", "short and long rows",
           to_csv(sales(9, "EC13")) + "rg9,EC13,1000.00\nrg10,EC13,1000.00,INR,2026-08-17T10:00:00Z,captured,c,m,x,false,false,EXTRA\n",
           "9700.00", "300.00"))

    a(Case("empty file", "nothing at all",
           "", "1000.00", expect="rejected"))

    a(Case("header only, no rows", "structure without data",
           to_csv([]), "1000.00", expect="rejected"))

    a(Case("not a ledger at all", "prose instead of data",
           "Dear customer,\n\nThank you for your order.\nTotal due: 500\n",
           "500.00", expect="rejected"))

    a(Case("alien headers", "nothing maps",
           "foo,bar,baz,qux\na,b,c,d\n", "1000.00", expect="rejected"))

    a(Case("no amount column", "the one field it cannot do without",
           to_csv([{"txn_id": uid(f"t{i}"), "ref_id": "EC18", "currency": "INR",
                    "timestamp": "2026-08-17T10:00:00Z"} for i in range(5)],
                  header=["txn_id", "ref_id", "currency", "timestamp"]),
           "1000.00", expect="rejected"))

    a(Case("unreadable amount", "a value that is not a number",
           to_csv([row("bad0", "EC19", "NOT_A_NUMBER")] + sales(5, "EC19")),
           "5000.00", expect="rejected",
           note="must be a plain rejection naming the row, never a crash"))

    a(Case("JSON instead of CSV", "the other input format",
           json.dumps([{"txn_id": uid(f"j{i}"), "ref_id": "EC20", "amount": 1000.00,
                        "currency": "INR", "timestamp": "2026-08-17T10:00:00Z"}
                       for i in range(10)]),
           "9700.00", "300.00", filename="feed.json", expect="cleared"))

    # ── currency and window ───────────────────────────────────────────────
    a(Case("mixed currencies", "currency filter",
           to_csv([row(f"m{i}", "EC21", "1000.00",
                       currency="INR" if i % 2 else "USD") for i in range(10)]),
           "4850.00", "150.00", expect_agents=("currency_filter",)))

    a(Case("every payment outside the window", "window filter",
           to_csv(sales(10, "EC22", ts_override=None) if False else
                  [row(f"w{i}", "EC22", "1000.00", ts="2026-06-01T10:00:00Z")
                   for i in range(10)]),
           "9700.00", "300.00", window=3, expect="withheld",
           expect_agents=("settlement_window_filter",)))

    a(Case("payments after the settlement instant", "window boundary",
           to_csv([row(f"af{i}", "EC23", "1000.00",
                       ts=f"2026-08-25T{10 + i % 8:02d}:00:00Z") for i in range(10)]),
           "9700.00", "300.00", settled_at="2026-08-19", window=3,
           expect="withheld",
           note="a settlement cannot contain payments that had not happened"))

    # ── fees ──────────────────────────────────────────────────────────────
    a(Case("no declared deductions", "fee reconstruction from the rate card",
           to_csv(sales(10, "EC24")), "9700.00", "",
           note="must say out loud that the gross was guessed"))

    a(Case("zero fees", "a real configuration, not a mistake",
           to_csv(sales(10, "EC25")), "10000.00", "0.00", expect="cleared"))

    # ── linkage ───────────────────────────────────────────────────────────
    a(Case("every member anchored by reference", "linkage all_linked tier",
           to_csv(sales(10, "EC26")), "9700.00", "300.00", expect="cleared",
           expect_agents=("linkage",)))

    a(Case("no references anywhere", "linkage has nothing to work with",
           to_csv([row(f"nr{i}", "", "1000.00") for i in range(10)]),
           "9700.00", "300.00", expect="withheld",
           note="arithmetic alone must not clear a settlement"))

    a(Case("truncated references", "the reference is cut short",
           to_csv([row(f"tr{i}", "EC2", "1000.00") for i in range(10)]),
           "9700.00", "300.00"))

    a(Case("half the pool anchored", "mixed evidence",
           to_csv(sales(5, "EC29") + [row(f"x{i}", "OTHER-BATCH", "1000.00")
                                      for i in range(5)]),
           "4850.00", "150.00"))

    # ── subset-sum ────────────────────────────────────────────────────────
    a(Case("unique exact match", "the happy path",
           to_csv([row(f"u{i}", "EC30", f"{100 * (i + 1)}.00") for i in range(10)]),
           "5335.00", "165.00", expect="cleared", expect_agents=("subset_sum", "tie_out")))

    a(Case("wholly fungible pool", "2000 identical payments",
           to_csv(sales(2000, "EC31", "5.00", prefix="ch", hour=6)),
           "494.70", "15.30", window=7, expect="withheld",
           note="must report interchangeability, not ask someone to pick"))

    a(Case("no subset reaches the target", "an impossible ask",
           to_csv(sales(10, "EC32")), "999999.00", "1.00", expect="withheld"))

    a(Case("large pool, one settlement", "1000 candidates",
           to_csv(sales(1000, "EC33", "100.00", prefix="lg", hour=8)),
           "97000.00", "3000.00", window=7))

    a(Case("single payment settlement", "n=1",
           to_csv([row("s0", "EC34", "1000.00")]), "970.00", "30.00", expect="cleared"))

    # ── status ────────────────────────────────────────────────────────────
    a(Case("failed payments in the pool", "status filter",
           to_csv(sales(10, "EC35") + sales(5, "EC35", prefix="f", status="failed")),
           "9700.00", "300.00", expect="cleared", expect_agents=("status_filter",),
           note="failures must not be spendable"))

    a(Case("authorized but not captured", "money not taken",
           to_csv(sales(10, "EC36") + sales(4, "EC36", prefix="au", status="authorized")),
           "9700.00", "300.00", expect="cleared", expect_agents=("status_filter",)))

    a(Case("stripe requires_capture spelling", "same state, another name",
           to_csv(sales(10, "EC37") + sales(3, "EC37", prefix="rc",
                                            status="requires_capture")),
           "9700.00", "300.00", expect="cleared", expect_agents=("status_filter",)))

    a(Case("returned bank credit", "money that arrived and left",
           to_csv(sales(10, "EC38") + sales(2, "EC38", prefix="rt", status="returned")),
           "9700.00", "300.00", expect="cleared", expect_agents=("status_filter",)))

    a(Case("pending and processing", "money in flight",
           to_csv(sales(10, "EC39") + sales(3, "EC39", prefix="pn", status="pending")
                  + sales(2, "EC39", prefix="pr", status="processing")),
           "9700.00", "300.00", expect="cleared", expect_agents=("status_filter",)))

    a(Case("refunded original stays a member", "the double-subtraction trap",
           to_csv(sales(8, "EC40") + sales(2, "EC40", prefix="rd", status="refunded")
                  + [row("rf0", "EC40", "-1000.00", ts="2026-08-17T18:00:00Z",
                         status="processed", memo="Refund"),
                     row("rf1", "EC40", "-1000.00", ts="2026-08-17T18:05:00Z",
                         status="processed", memo="Refund")]),
           "7760.00", "240.00",
           note="target must be Rs 8,000 — not Rs 6,000"))

    a(Case("unrecognised status is kept", "do not guess",
           to_csv(sales(10, "EC41", status="settled_t1")),
           "9700.00", "300.00", expect="cleared",
           note="a status nobody recognises must not cause a drop"))

    # ── negatives ─────────────────────────────────────────────────────────
    a(Case("anchored refunds", "forced members",
           to_csv(sales(20, "EC42") + [row(f"rf{i}", "EC42", "-1000.00",
                                           ts=f"2026-08-17T18:{i:02d}:00Z",
                                           memo="Refund") for i in range(3)]),
           "16490.00", "510.00", expect="cleared",
           note="must not report ambiguous just because negatives exist"))

    a(Case("refund for another settlement", "unanchored negative stays optional",
           to_csv(sales(10, "EC43") + [row("rfx", "SOME-OTHER", "-1000.00",
                                           ts="2026-08-17T18:00:00Z", memo="Refund")]),
           "9700.00", "300.00"))

    a(Case("partial refund", "less back than went out",
           to_csv(sales(10, "EC44") + [row("pr0", "EC44", "-400.00",
                                           ts="2026-08-17T18:00:00Z",
                                           memo="Partial refund")]),
           "9312.00", "288.00", expect="cleared"))

    a(Case("rolling reserve withheld", "the gateway keeps a slice",
           to_csv(sales(10, "EC45") + [row("res0", "EC45", "-500.00",
                                           ts="2026-08-17T19:00:00Z",
                                           memo="Rolling reserve")]),
           "9215.00", "285.00", expect="cleared"))

    a(Case("chargeback", "a clawback after the fact",
           to_csv(sales(10, "EC46") + [row("cb0", "EC46", "-1000.00",
                                           ts="2026-08-17T20:00:00Z",
                                           memo="Chargeback")]),
           "8730.00", "270.00", expect="cleared"))

    a(Case("everything is a refund", "an all-negative pool",
           to_csv([row(f"an{i}", "EC47", "-1000.00",
                       ts=f"2026-08-17T1{i}:00:00Z", memo="Refund") for i in range(5)]),
           "-4850.00", "-150.00", expect="cleared",
           note="a refund-only day genuinely nets negative and those refunds "
                "genuinely are the members — clearing is correct. Worth noting "
                "the summary still says 'no action needed' on a settlement "
                "where the merchant owes money."))

    # ── compliance ────────────────────────────────────────────────────────
    a(Case("cash over the CTR threshold", "statutory rule",
           to_csv(sales(9, "EC48") + [row("ctr0", "EC48", "1200000.00",
                                          cash="true", memo="Cash deposit")]),
           "1174300.00", "35700.00", expect_agents=("compliance_agent",),
           note="a STATUTORY finding must appear"))

    a(Case("structuring pattern", "supervisory guidance",
           to_csv([row(f"st{i}", "EC49", "180000.00", cash="true",
                       payer="cust_same",
                       ts=f"2026-08-17T{9 + i:02d}:00:00Z") for i in range(5)]),
           "873000.00", "27000.00", expect_agents=("compliance_agent",)))

    a(Case("identical duplicate rows", "internal policy rule",
           to_csv(sales(9, "EC50") + [row("dup", "EC50", "1000.00"),
                                      row("dup2", "EC50", "1000.00")]),
           "10670.00", "330.00",
           expect_agents=("compliance_agent", "auto_disposition"),
           note="DUPLICATE_TX must fire and be auto-closed"))

    return C


def exercise_human_agents(url: str, stamp: int) -> dict:
    """
    attribution and human_reviewer fire on human actions, not on a file.

    Running fifty uploads leaves them cold, which reads as "untested agents"
    when in fact nothing in the suite had asked a person to do anything. So
    the suite performs the human half too: names a reviewer on a run, records
    a decision, approves a posting, and clears a compliance finding.
    """
    out = {}
    batch = f"EDGE{stamp}-HUMAN"
    body = to_csv(sales(10, batch, prefix=f"hm{stamp}"))
    case = Case("human actions", "attribution + human_reviewer", body,
                "9700.00", "300.00")
    post(url, case, batch)          # a plain run so the batch exists

    for path, payload in (
        (f"/settlement/{batch}/decision",
         {"decision": "confirmed", "reviewer": "suite@amongresolver.app",
          "note": "Edge-case suite.", "txn_ids": ["a", "b"]}),
        (f"/settlement/{batch}/journal/decision",
         {"decision": "approved", "reviewer": "suite@amongresolver.app",
          "entry_id": "JE-1"}),
        (f"/settlement/{batch}/compliance/decision",
         {"rule_id": "DUPLICATE_TX", "decision": "cleared",
          "reviewer": "suite@amongresolver.app", "basis": "internal_policy"}),
    ):
        req = urllib.request.Request(
            url + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out[path.rsplit("/", 1)[-1]] = json.loads(r.read()).get("decision", "ok")
        except Exception as e:
            out[path.rsplit("/", 1)[-1]] = f"failed: {type(e).__name__}"
    return {"batch": batch, "results": out}


# ── running ───────────────────────────────────────────────────────────────

def post(url: str, case: Case, batch_id: str) -> dict:
    boundary = "----edgecase"
    parts = []
    fields = {
        "batch_id": batch_id, "net_amount": case.net,
        "settled_at": case.settled_at, "currency": case.currency,
        "settlement_window_days": str(case.window), "member_source": "gateway",
    }
    if case.deductions != "":
        fields["declared_deductions"] = case.deductions
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n")
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"gateway_file\"; "
        f"filename=\"{case.filename}\"\r\nContent-Type: text/csv\r\n\r\n{case.body}\r\n")
    parts.append(f"--{boundary}--\r\n")
    data = "".join(parts).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/reconcile/upload", data=data,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"detail": {"message": f"HTTP {e.code}"}}
    except Exception as e:
        return {"detail": {"message": f"{type(e).__name__}: {e}"}}


def agents_for(url: str, batch_id: str) -> Counter:
    try:
        with urllib.request.urlopen(f"{url}/audit/{batch_id}", timeout=60) as r:
            trail = json.loads(r.read()).get("trail", [])
        return Counter(e["agent"] for e in trail)
    except Exception:
        return Counter()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=BASE)
    args = ap.parse_args()

    global RUN_TOKEN
    stamp = int(time.time())
    RUN_TOKEN = "".join(random.choice(string.ascii_lowercase) for _ in range(6)) + "_"
    cases = build_cases()
    # Every fixture id carries the run stamp. Without it, cases share ids like
    # "p0000", the first case to clear consumes them in the settled ledger, and
    # every later case is refused as a double-claim — eight false mismatches on
    # the first run, all of them the guard working correctly on my sloppy ids.
    fired_overall: Counter = Counter()
    results = []
    false_clears = []
    missing_agents = []

    print(f"\n{len(cases)} edge cases against {args.url}\n" + "=" * 78)
    for n, c in enumerate(cases, 1):
        batch_id = f"EDGE{stamp}-{n:02d}"
        # the reference inside each fixture is EC<nn>; point the batch at it
        body = c.body.replace(f"EC{n:02d}", batch_id)
        c2 = Case(**{**c.__dict__, "body": body})
        resp = post(args.url, c2, batch_id)

        if "detail" in resp:
            outcome = "rejected"
            d = resp.get("detail")
            if isinstance(d, dict):
                detail = str(d.get("plain") or d.get("message") or "")
            else:
                detail = str(d)
            detail = detail.replace(chr(10), " ")[:58]
        else:
            s = resp.get("summary", {})
            outcome = "cleared" if s.get("cleared") else "withheld"
            detail = f"{s.get('matched_count')}/{s.get('total_candidates')}"

        why = ""
        if outcome == "withheld" and c.expect == "cleared":
            s2 = resp.get("summary", {})
            why = str(s2.get("withheld_reason") or "")
            try:
                with urllib.request.urlopen(f"{args.url}/audit/{batch_id}", timeout=60) as r:
                    tr = json.loads(r.read()).get("trail", [])
                for e in reversed(tr):
                    d = e.get("detail", "")
                    if any(k in d for k in ("Withheld", "refusing to auto-clear",
                                            "already", "no anchored member",
                                            "alternate")):
                        why = f"{why} | {e['agent']}: {d[:110]}"
                        break
            except Exception:
                pass

        fired = agents_for(args.url, batch_id)
        fired_overall.update(fired)

        ok = c.expect in ("any", outcome)
        # A clear where a decline was required is the only failure that matters
        # more than the others: it is a false clear.
        if c.expect in ("withheld", "rejected") and outcome == "cleared":
            false_clears.append(c.name)
        gap = [a for a in c.expect_agents if not fired.get(a)]
        if gap:
            missing_agents.append((c.name, gap))

        mark = "ok " if ok and not gap else ("FC!" if c.name in false_clears else "..")
        print(f"{mark} {n:>2}. {c.name:<42}{outcome:<10}{detail}")
        results.append((c, outcome, ok, gap, why))

    human = exercise_human_agents(args.url, stamp)
    fired_overall.update(agents_for(args.url, human["batch"]))
    print(f"   + human actions on {human['batch']}: {human['results']}")

    # ── summary ───────────────────────────────────────────────────────────
    print("=" * 78)
    counts = Counter(o for _, o, _, _, _ in results)
    print(f"  cleared {counts['cleared']}   withheld {counts['withheld']}   "
          f"rejected {counts['rejected']}")
    matched = sum(1 for _, _, ok, gap, _ in results if ok and not gap)
    print(f"  behaved as expected: {matched}/{len(results)}")

    print(f"\n  FALSE CLEARS: {len(false_clears)}"
          + ("" if not false_clears else "  <-- " + ", ".join(false_clears)))

    declared = [
        "attribution", "auto_disposition", "cash_position", "compliance_agent",
        "currency_filter", "exception_diagnosis", "fee_decomposition",
        "file_agent", "fuzzy_fallback", "human_reviewer", "ingestion", "linkage",
        "orchestrator", "settled_ledger", "settlement_qa",
        "settlement_window_filter", "status_filter", "subset_sum", "tie_out",
        "tiebreak",
    ]
    hit = [a for a in declared if fired_overall.get(a)]
    cold = [a for a in declared if not fired_overall.get(a)]
    print(f"\n  agent coverage: {len(hit)}/{len(declared)}")
    if cold:
        print("  never fired:   " + ", ".join(cold))

    if missing_agents:
        print("\n  cases whose required agent did not fire:")
        for name, gap in missing_agents:
            print(f"    {name}: {', '.join(gap)}")

    unexplained = [(c, w) for c, o, ok, _, w in results
                   if o == "withheld" and c.expect == "cleared"]
    if unexplained:
        print()
        print(f"  expected cleared, withheld ({len(unexplained)}) — why:")
        for c, w in unexplained:
            print(f"    {c.name}")
            print(f"      {w[:150] if w else '(no reason recorded)'}")

    print()
    return 1 if false_clears else 0


if __name__ == "__main__":
    sys.exit(main())
