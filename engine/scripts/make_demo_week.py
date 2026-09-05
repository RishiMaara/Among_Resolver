"""
A week of settlements — what the queue is actually for.

The single-settlement screen answers "which payments are in this deposit".
Nobody has one deposit. A merchant has one per day and a controller opens
Monday morning to five of them, or fifty, and the only question that matters
is WHICH ONES NEED ME.

So this builds five trading days for Aarna Organics against ONE pool of
payments, and deliberately makes them differ, because a queue where every
row is identical demonstrates nothing:

  Mon  clean          every payment present and referenced -> clears
  Tue  clean          same -> clears
  Wed  missing leg    one payment never arrived -> cannot tie out
  Thu  no references  the feed lost its ref column that day -> arithmetic
                      alone, so it is withheld rather than guessed
  Fri  duplicate      a customer double-clicked -> clears, but flagged

The point of the run is the top line: three of these are done and two are
not, and the two announce which they are.
"""
import csv
import os
from datetime import datetime, timedelta, timezone

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "demo_week")
WEEK_START = datetime(2026, 8, 17, tzinfo=timezone.utc)   # a Monday
FEE_BPS, GST_ON_FEE_BPS = 200, 1800
BASKET = [34900, 49900, 64900, 89900, 119900, 149900, 229900, 299900]

DAYS = [
    ("MON", "clean",         12),
    ("TUE", "clean",         14),
    ("WED", "missing_leg",   11),
    ("THU", "no_references", 13),
    ("FRI", "duplicate",     12),
]


def r(paise): return f"{paise / 100:.2f}"


def build():
    payments, settlements = [], []

    for day_index, (label, kind, count) in enumerate(DAYS):
        day = WEEK_START + timedelta(days=day_index)
        batch = f"STL2026{day.strftime('%m%d')}AARNA"
        rows, t = [], day + timedelta(hours=7)

        for i in range(count):
            t += timedelta(minutes=23)
            rows.append({
                "txn_id": f"pay_{label}{i:03d}",
                "amount": r(BASKET[i % len(BASKET)]),
                "currency": "INR",
                "timestamp": t.isoformat(),
                "status": "captured",
                # Thursday's feed lost its reference column. The engine has to
                # fall back on arithmetic alone, which is exactly the case it
                # is designed to refuse rather than guess at.
                "ref_id": "" if kind == "no_references" else batch,
                "payer_id": f"cust_{day_index}{i:03d}",
                "memo": "Online order",
            })

        if kind == "duplicate":
            dup = dict(rows[3])
            dup["txn_id"] = f"pay_{label}DUP"
            rows.append(dup)

        # The settlement is built from what the merchant was ACTUALLY paid, so
        # the arithmetic is honest. Wednesday's figure includes a payment that
        # never made it into the feed — the real-world "missing leg".
        banked = list(rows)
        if kind == "missing_leg":
            ghost = {"amount": r(189900)}     # in the bank total, not in the file
            banked = rows + [ghost]

        gross = sum(int(round(float(x["amount"]) * 100)) for x in banked)
        fee = gross * FEE_BPS // 10000
        gst = fee * GST_ON_FEE_BPS // 10000
        net = gross - fee - gst

        payments.extend(rows)
        settlements.append({
            "settlement_id": batch,
            "net_amount": r(net),
            "settled_at": (day + timedelta(days=2)).date().isoformat(),
            "currency": "INR",
            "declared_deductions": r(fee + gst),
            "_kind": kind,
            "_gross": gross,
            "_rows": len(rows),
        })

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "gateway.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(payments[0].keys()))
        w.writeheader()
        w.writerows(payments)
    with open(os.path.join(OUT, "settlements.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["settlement_id", "net_amount", "settled_at", "currency",
                    "declared_deductions"])
        for s in settlements:
            w.writerow([s["settlement_id"], s["net_amount"], s["settled_at"],
                        s["currency"], s["declared_deductions"]])

    print(f"A week for Aarna Organics — {len(payments)} payments, "
          f"{len(settlements)} settlements, one pool.\n")
    print(f"  {'settlement':<22}{'day':<6}{'rows':>5}{'gross':>13}   what is in it")
    for s, (label, kind, _) in zip(settlements, DAYS):
        print(f"  {s['settlement_id']:<22}{label:<6}{s['_rows']:>5}"
              f"{s['_gross']/100:>13,.2f}   {kind}")
    print(f"\n  -> {os.path.abspath(OUT)}")


if __name__ == "__main__":
    build()
