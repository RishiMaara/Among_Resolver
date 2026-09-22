"""
Accuracy on real payments, with ground truth recorded by someone else.

WHY
---
Every other accuracy corpus here is generated — this project's own, or
ReconRiver's, which says so of itself. Real settlement data with ground truth
is not published: it is personal and commercially sensitive, and no merchant
will hand theirs over. But the same problem exists in public accounts. A
government's accounts-payable run pays many invoices with one cheque or one
electronic transfer, and its open checkbook records which invoices each
payment covered. That record was made by the government's own system, not by
anyone here.

THE TEST
--------
One payment (a cheque or transfer) is the "settlement": its amount is the sum
of the invoices it paid. The candidate pool is every invoice paid on the same
day — the whole payment run, often several hundred invoices from every
vendor — and the question is the engine's usual one: which of these make up
this payment?

  payee known     each invoice's reference carries its vendor's code, as an
                  AP ledger does, and the payment names the payee. Several
                  payments to one vendor on one day remain genuinely
                  ambiguous, as they would be for a person.
  amounts only    references carry only the invoice number; nothing ties an
                  invoice to the payment but arithmetic. About a fifth of
                  line amounts repeat within a day, so this is where a wrong
                  clear would come from.

Invoice ids are opaque and shuffled, so nothing in an id says which payment
it belongs to. Amounts, dates, invoice numbers, vendor repetition and
payment-run sizes are all real.

THE DATA
--------
  baton_rouge   Open Checkbook BR, City of Baton Rouge / Parish of East Baton
                Rouge (data.brla.gov, 7qhq-wwsg). Public domain.
  fulton        Vendor Payments, Fulton County Government, GA
                (sharefulton.fultoncountyga.gov, kp4p-scak). CC BY 4.0.

A snapshot is committed under data/public_ledgers/ with a manifest of the
queries and the date fetched, so the numbers reproduce without the network.
Vendors appear in it only as hashed codes: a public checkbook can name
private people as payees, and nothing here needs a name.

WHAT THIS IS NOT
----------------
Card or UPI settlements. These are government payables in US dollars; the
batching is real but it is not a payment processor's. It is real data with a
real answer key, which the generated corpora are not.

From engine/:
    python scripts/public_ledger_benchmark.py --fetch     # refresh the snapshot
    python scripts/public_ledger_benchmark.py --json docs/benchmarks/public_ledgers.json
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("SETTLEMENT_CYCLE_STORE", "memory")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from ingestion import normalize_ref_id  # noqa: E402
from orchestrator import reconcile_batch  # noqa: E402
from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence  # noqa: E402
from subset_sum import SubsetSumConfig  # noqa: E402

DATA = HERE.parent / "data" / "public_ledgers"
CFG = SubsetSumConfig(solver_time_limit_s=5.0, ambiguity_probe_limit=2, num_search_workers=1)

SOURCES = {
    "baton_rouge": {
        "url": "https://data.brla.gov/resource/7qhq-wwsg.json",
        "fields": {"payment": "check_number", "date": "check_date", "vendor": "vendor_name",
                   "invoice": "invoice_number", "amount": "line_item_amount"},
        "title": "Open Checkbook BR — City of Baton Rouge / Parish of East Baton Rouge",
        "license": "Public Domain",
    },
    "fulton": {
        "url": "https://sharefulton.fultoncountyga.gov/resource/kp4p-scak.json",
        "fields": {"payment": "check_no", "date": "disb_date", "vendor": "vendor_code",
                   "invoice": "vendor_invoice_no", "amount": "amount"},
        "title": "Vendor Payments — Fulton County Government (GA)",
        "license": "CC BY 4.0 — Fulton County Government (GA)",
    },
}
SINCE = "2026-07-01T00:00:00"


def fetch() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    manifest = {"fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "since": SINCE, "sources": {}}
    for name, src in SOURCES.items():
        f = src["fields"]
        query = {"$select": ",".join(f.values()), "$where": f"{f['date']}>='{SINCE}'",
                 "$order": f"{f['date']},{f['payment']}", "$limit": "50000"}
        url = src["url"] + "?" + urllib.parse.urlencode(query)
        with urllib.request.urlopen(url, timeout=120) as r:  # nosec B310 - fixed https URLs
            rows = json.load(r)
        # One row per invoice a payment covered: an invoice's lines are summed.
        invoices: dict[tuple, Decimal] = defaultdict(Decimal)
        for row in rows:
            if not row.get(f["payment"]) or not row.get(f["amount"]):
                continue
            # The vendor is kept only as a hashed code: a public checkbook can
            # name private people as payees, and grouping needs no name.
            key = (row[f["date"]][:10], row[f["payment"]],
                   _vendor_code(row.get(f["vendor"]) or ""), row.get(f["invoice"]) or "")
            invoices[key] += Decimal(row[f["amount"]])
        out = DATA / f"{name}.csv"
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "payment", "vendor", "invoice", "amount_cents"])
            for (d, pay, ven, inv), amt in sorted(invoices.items()):
                w.writerow([d, pay, ven, inv, int(amt * 100)])
        manifest["sources"][name] = {"title": src["title"], "license": src["license"],
                                     "query": url, "rows_fetched": len(rows),
                                     "invoices": len(invoices)}
        print(f"{name}: {len(rows)} lines -> {len(invoices)} invoices")
    (DATA / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")


def _vendor_code(vendor: str) -> str:
    return "VND" + hashlib.sha1(vendor.encode("utf-8")).hexdigest()[:10].upper()  # nosec B324 - an id, not security


def load(name: str) -> dict[str, list[dict]]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    with (DATA / f"{name}.csv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            row["amount_cents"] = int(row["amount_cents"])
            by_day[row["date"]].append(row)
    return by_day


def run(name: str, n: int, seed: int) -> dict:
    by_day = load(name)
    rng = random.Random(seed)
    payments = defaultdict(list)
    for day, rows in by_day.items():
        for r in rows:
            payments[(day, r["payment"])].append(r)
    eligible = sorted(k for k, v in payments.items()
                      if len(v) >= 2 and sum(r["amount_cents"] for r in v) > 0)
    chosen = rng.sample(eligible, min(n, len(eligible)))

    results = {"payee_known": [], "amounts_only": []}
    for day, payment in chosen:
        rows = by_day[day]
        order = list(range(len(rows)))
        rng.shuffle(order)
        ids = {i: f"INV{pos:05d}" for pos, i in enumerate(order)}
        truth = {ids[i] for i, r in enumerate(rows) if r["payment"] == payment}
        members = [r for r in rows if r["payment"] == payment]
        vendor = members[0]["vendor"]
        when = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
        total = sum(r["amount_cents"] for r in members)
        for condition in results:
            pool = [NormalizedTxn(
                source=SourceType.ERP, source_txn_id=ids[i],
                # Canonical exactly as ingestion stores it.
                ref_id_canonical=normalize_ref_id(f"{r['vendor']}-{r['invoice']}"
                                                  if condition == "payee_known" else r["invoice"]),
                amount_cents=r["amount_cents"], currency="USD", timestamp_utc=when,
                tz_confidence=TzConfidence.HIGH, memo_raw="", memo_normalized="",
                # As ingestion keeps it: the reference with the separator an
                # export writes between payee code and invoice number.
                extra={"ref_raw": (f"{r['vendor']}-{r['invoice']}"
                                   if condition == "payee_known" else r["invoice"])})
                for i, r in enumerate(rows)]
            batch = SettlementBatch(
                batch_id=(vendor if condition == "payee_known"
                          else f"PAYMENT {payment}"),
                net_amount_cents=total, currency="USD",
                settled_at_utc=when + timedelta(hours=12), declared_deductions_cents=0,
                member_source=SourceType.ERP)
            t0 = time.perf_counter()
            report = reconcile_batch(batch, pool, subset_config=CFG, settlement_window_days=1)
            m = report.match_result
            got = set(m.matched_txn_ids)
            results[condition].append({
                "day": day, "invoices": len(members), "pool": len(rows),
                "vendor_payments_that_day": len({r["payment"] for r in rows if r["vendor"] == vendor}),
                "exact": got == truth, "cleared": bool(m.cleared),
                "false_clear": bool(m.cleared) and got != truth,
                "confidence": m.confidence, "seconds": round(time.perf_counter() - t0, 2)})
    return {c: summarise(rows) | {"rows": rows} for c, rows in results.items()}


def summarise(rows: list[dict]) -> dict:
    n = len(rows)
    return {"payments": n,
            "exact_set_identified": sum(r["exact"] for r in rows),
            "exact_pct": round(100 * sum(r["exact"] for r in rows) / n, 1) if n else 0.0,
            "auto_cleared_correct": sum(r["cleared"] and r["exact"] for r in rows),
            "false_clears": sum(r["false_clear"] for r in rows),
            "median_pool": sorted(r["pool"] for r in rows)[n // 2] if n else 0,
            "median_invoices": sorted(r["invoices"] for r in rows)[n // 2] if n else 0,
            "mean_seconds": round(sum(r["seconds"] for r in rows) / n, 2) if n else 0.0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--n", type=int, default=50, help="payments sampled per ledger")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    logging.disable(logging.WARNING)
    if args.fetch:
        fetch()
        return
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    out = {"sampled_per_ledger": args.n, "seed": args.seed,
           "fetched_at_utc": manifest["fetched_at_utc"], "ledgers": {}}
    for name in SOURCES:
        res = run(name, args.n, args.seed)
        out["ledgers"][name] = {"source": manifest["sources"][name]["title"],
                                "license": manifest["sources"][name]["license"], **res}
        for cond, r in res.items():
            print(f"{name:12s} {cond:13s} exact {r['exact_set_identified']}/{r['payments']} "
                  f"({r['exact_pct']}%), auto-cleared right {r['auto_cleared_correct']}, "
                  f"FALSE CLEARS {r['false_clears']}, median pool {r['median_pool']}, "
                  f"{r['mean_seconds']}s each")
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1), encoding="utf-8")
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
