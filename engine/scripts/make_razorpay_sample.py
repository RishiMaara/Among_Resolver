#!/usr/bin/env python3
"""
Build the Razorpay sample: API responses, a bank statement and a ledger.

Field names, types, units and conventions are Razorpay's published contract
for GET /v1/settlements and GET /v1/settlements/recon/combined — amounts in
paise, `fee` INCLUDING its GST with `tax` stating that GST, `credit - debit`
the net line, `settled_at` and `created_at` in Unix seconds. The values are
invented, and every file says so.

Five payouts on a T+2 cycle. Three are clean; two are built to show an
outcome:

  setl ...002   verified with findings: one card payment charged 2.5% where
                the rate card says 2%, and one order missing from the books
  setl ...004   not verified: the bank credit carrying its UTR is Rs 10 short

plus two lines in the period that name no settlement yet (on hold).

Deterministic: the same files every run. From engine/:
    python scripts/make_razorpay_sample.py
"""

from __future__ import annotations

import csv
import json
import os
import random
from datetime import datetime, timedelta, timezone

OUT = os.path.join(os.path.dirname(__file__), "..", "..", "public", "sample-data", "razorpay")
IST = timezone(timedelta(hours=5, minutes=30))
NOTE = ("Invented values in Razorpay's published response shapes. Not from a live "
        "account; see engine/src/razorpay_recon.py for what has and has not been verified.")

RATES = {"card": 200, "upi": 0, "netbanking": None, "wallet": 200}   # bps; netbanking Rs 5 flat


def ts(d: datetime) -> int:
    return int(d.timestamp())


def fee_for(amount: int, method: str, override_bps: int | None = None) -> tuple[int, int]:
    """(fee including GST, GST) in paise, Razorpay's convention."""
    if override_bps is not None:
        base = round(amount * override_bps / 10_000)
    elif method == "netbanking":
        base = 500
    else:
        base = round(amount * RATES[method] / 10_000)
    gst = round(base * 1800 / 10_000)
    return base + gst, gst


def main() -> None:
    rng = random.Random(20260915)
    os.makedirs(OUT, exist_ok=True)
    recon, settlements, bank, ledger = [], [], [], []
    methods = ["card", "upi", "upi", "netbanking", "wallet", "card", "upi"]
    order_no = 1000

    for k, day in enumerate([7, 8, 9, 10, 11]):
        captured = datetime(2026, 9, day, 10, 0, tzinfo=IST)
        settled = captured + timedelta(days=2, hours=1)       # T+2
        sid = f"setl_SAMPLE00000{k + 1}"
        utr = f"UTIB0000SMPL{k + 1:04d}"
        lines = []
        for j in range(10 + k * 2):
            order_no += 1
            method = methods[(j + k) % len(methods)]
            amount = rng.randrange(49_900, 1_249_900, 100)
            override = 250 if (k == 1 and j == 3 and method == "card") else None
            if k == 1 and j == 3:
                method, override = "card", 250           # the planted overcharge
            fee, gst = fee_for(amount, method, override)
            created = captured + timedelta(minutes=17 * j)
            lines.append({
                "entity_id": f"pay_S{k + 1}{j:03d}{rng.randrange(10**6):06d}", "type": "payment",
                "debit": 0, "credit": amount - fee, "amount": amount, "currency": "INR",
                "fee": fee, "tax": gst, "on_hold": False, "settled": True,
                "created_at": ts(created), "settled_at": ts(settled),
                "settlement_id": sid, "settlement_utr": utr,
                "description": None, "notes": [], "payment_id": None,
                "order_id": f"order_S{order_no}", "order_receipt": f"rcpt_{order_no}",
                "method": method, "card_network": "Visa" if method == "card" else None,
                "card_issuer": None, "card_type": "credit" if method == "card" else None,
                "dispute_id": None,
            })
        refund_amount = rng.randrange(20_000, 90_000, 100)
        lines.append({
            "entity_id": f"rfnd_S{k + 1}{rng.randrange(10**8):08d}", "type": "refund",
            "debit": refund_amount, "credit": 0, "amount": refund_amount, "currency": "INR",
            "fee": 0, "tax": 0, "on_hold": False, "settled": True,
            "created_at": ts(captured + timedelta(hours=5)), "settled_at": ts(settled),
            "settlement_id": sid, "settlement_utr": utr, "description": None, "notes": [],
            "payment_id": lines[0]["entity_id"], "order_id": lines[0]["order_id"],
            "order_receipt": None, "method": lines[0]["method"], "card_network": None,
            "card_issuer": None, "card_type": None, "dispute_id": None,
        })
        net = sum(i["credit"] - i["debit"] for i in lines)
        settlements.append({"id": sid, "entity": "settlement", "amount": net, "status": "processed",
                            "fees": 0, "tax": 0, "utr": utr, "created_at": ts(settled),
                            "currency": "INR"})
        recon += lines

        arrived = net - (1_000 if k == 3 else 0)           # the Rs 10 short credit
        bank.append({"entry_id": f"BNK{9100 + k}", "ref_id": utr, "credit": f"{arrived / 100:.2f}",
                     "currency": "INR", "value_date": (settled + timedelta(hours=2)).isoformat(),
                     "description": f"NEFT CR {utr} RAZORPAY SOFTWARE PVT LTD"})
        for i in lines:
            if i["type"] != "payment" or (k == 1 and i is lines[5]):
                continue                                     # the unbooked order
            ledger.append({"journal_id": f"JV{order_no}{i['entity_id'][-4:]}",
                           "reference": i["order_id"], "credit": f"{i['amount'] / 100:.2f}",
                           "currency": "INR",
                           "booked_at": datetime.fromtimestamp(i["created_at"], tz=IST).isoformat(),
                           "narration": f"Sales {i['order_receipt']}"})

    for j in range(2):                                          # on hold, no payout yet
        order_no += 1
        amount = rng.randrange(49_900, 400_000, 100)
        fee, gst = fee_for(amount, "card")
        recon.append({
            "entity_id": f"pay_HOLD{j:02d}{rng.randrange(10**6):06d}", "type": "payment",
            "debit": 0, "credit": amount - fee, "amount": amount, "currency": "INR",
            "fee": fee, "tax": gst, "on_hold": True, "settled": False,
            "created_at": ts(datetime(2026, 9, 12, 15, tzinfo=IST)), "settled_at": None,
            "settlement_id": None, "settlement_utr": None, "description": None, "notes": [],
            "payment_id": None, "order_id": f"order_S{order_no}", "order_receipt": f"rcpt_{order_no}",
            "method": "card", "card_network": "Mastercard", "card_issuer": None,
            "card_type": "debit", "dispute_id": None,
        })
    bank.append({"entry_id": "BNK9200", "ref_id": "CHQ004512", "credit": "15000.00",
                 "currency": "INR", "value_date": "2026-09-12T11:00:00+05:30",
                 "description": "CHQ DEP 004512 SHARMA TRADERS"})

    with open(os.path.join(OUT, "settlements.json"), "w", encoding="utf-8") as f:
        json.dump({"_note": NOTE, "entity": "collection", "count": len(settlements),
                   "items": settlements}, f, indent=1)
    with open(os.path.join(OUT, "recon_combined.json"), "w", encoding="utf-8") as f:
        json.dump({"_note": NOTE, "entity": "collection", "count": len(recon), "items": recon},
                  f, indent=1)
    with open(os.path.join(OUT, "bank_statement.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(bank[0]))
        w.writeheader()
        w.writerows(bank)
    with open(os.path.join(OUT, "ledger.json"), "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=1)
    print(f"{len(settlements)} settlements, {len(recon)} recon lines, "
          f"{len(bank)} bank rows, {len(ledger)} ledger entries -> {os.path.abspath(OUT)}")


def dashboard_report() -> None:
    """
    The same recon lines as a merchant would download them from the Dashboard:
    title-case headers, rupees with decimals, IST dates as printed. Written from
    recon_combined.json, so the two must reconcile identically.
    """
    import csv  # pylint: disable=import-outside-toplevel
    from zoneinfo import ZoneInfo  # pylint: disable=import-outside-toplevel
    ist = ZoneInfo("Asia/Kolkata")
    with open(os.path.join(OUT, "recon_combined.json"), encoding="utf-8") as f:
        items = json.load(f)["items"]
    cols = ["entity_id", "type", "debit", "credit", "amount", "currency", "fee", "tax",
            "on_hold", "settled", "created_at", "settled_at", "settlement_id",
            "settlement_utr", "order_id", "order_receipt", "method"]

    def cell(k, v):
        if k in ("debit", "credit", "amount", "fee", "tax"):
            return f"{(v or 0) / 100:.2f}"
        if k in ("created_at", "settled_at"):
            return datetime.fromtimestamp(v, tz=ist).strftime("%d/%m/%Y %H:%M:%S") if v else ""
        return "" if v is None else str(v)

    path = os.path.join(OUT, "settlement_report.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([c.replace("_", " ").title().replace("Id", "ID").replace("Utr", "UTR")
                    for c in cols])
        for i in items:
            w.writerow([cell(c, i.get(c)) for c in cols])
    print(f"{len(items)} lines -> {os.path.abspath(path)}")


if __name__ == "__main__":
    import sys
    if "--report-only" in sys.argv:
        dashboard_report()
    else:
        main()
        dashboard_report()
