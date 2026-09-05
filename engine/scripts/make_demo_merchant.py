"""
One real trading day for a D2C merchant on an Indian gateway.

The fixtures used to exercise this engine have mostly been throwaway — two
settlements called Q-1 and Q-2, ten identical Rs 500 rows, amounts with no
relationship to anything a shop actually charges. They prove a code path and
they demonstrate nothing to a person, because no finance reviewer has ever
seen a day that looks like that.

This generates a day that looks like a real one. Aarna Organics is a D2C
foods brand: retail orders at Indian price points, a wholesale customer
restocking, one corporate bulk order, and a customer who double-clicked
checkout. Settlement is T+2 at the standard gateway rate of 2% plus 18% GST
on the fee, so the net credit is a figure a merchant would recognise from
their own dashboard.

Three compliance conditions are present, and all three are things that
happen in ordinary retail rather than crimes:

  DUPLICATE_TX        a customer paid twice in the same second
  STRUCTURING_PATTERN a reseller placed four sub-Rs 50,000 orders in a day
  CTR_THRESHOLD       a company ordered Rs 12.5 lakh of Diwali hampers

That is the point. Each one is FLAGGED, none is proof of anything, and a
reviewer's job is to look and say so. A demo where every alert is a real
criminal teaches the wrong reflex.
"""
import csv
import os
from datetime import datetime, timedelta, timezone

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "demo_merchant")
DAY = datetime(2026, 8, 17, tzinfo=timezone.utc)
SETTLED = DAY + timedelta(days=2)
BATCH = "STL20260817AARNA"

FEE_BPS = 200          # 2.00% gateway fee
GST_ON_FEE_BPS = 1800  # 18% GST charged on the fee

# Retail price points a D2C foods brand actually charges.
BASKET = [34900, 49900, 64900, 89900, 119900, 149900, 229900, 299900, 349900]


def rupees(paise): return f"{paise / 100:.2f}"


def build():
    rows, t = [], DAY + timedelta(hours=6)

    # ── ordinary retail traffic through the day ───────────────────────────
    for i in range(34):
        amt = BASKET[i % len(BASKET)]
        t += timedelta(minutes=17)
        rows.append({
            "txn_id": f"pay_AARNA{i:04d}", "amount": rupees(amt), "currency": "INR",
            "timestamp": t.isoformat(), "status": "captured",
            "ref_id": BATCH, "payer_id": f"cust_{1000 + i}",
            "memo": "Online order",
        })

    # ── a customer who double-clicked checkout -> DUPLICATE_TX ────────────
    dup_t = (DAY + timedelta(hours=13, minutes=42)).isoformat()
    for n in (1, 2):
        rows.append({
            "txn_id": f"pay_AARNADUP{n}", "amount": rupees(149900), "currency": "INR",
            "timestamp": dup_t, "status": "captured", "ref_id": BATCH,
            "payer_id": "cust_1042", "memo": "Online order",
        })

    # ── a reseller restocking -> STRUCTURING_PATTERN (each < Rs 50,000) ───
    for n, amt in enumerate((1850000, 2200000, 1975000, 1640000), start=1):
        rows.append({
            "txn_id": f"pay_AARNAWH{n}", "amount": rupees(amt), "currency": "INR",
            "timestamp": (DAY + timedelta(hours=9 + n)).isoformat(),
            "status": "captured", "ref_id": BATCH,
            "payer_id": "cust_wholesale_88", "memo": "Wholesale restock",
        })

    # ── a company's Diwali hamper order -> CTR_THRESHOLD (>= Rs 10 lakh) ──
    rows.append({
        "txn_id": "pay_AARNACORP1", "amount": rupees(12500000), "currency": "INR",
        "timestamp": (DAY + timedelta(hours=15, minutes=5)).isoformat(),
        "status": "captured", "ref_id": BATCH,
        "payer_id": "corp_zenithsoft", "memo": "Corporate gifting - Diwali hampers",
    })

    gross = sum(int(round(float(r["amount"]) * 100)) for r in rows)
    fee = gross * FEE_BPS // 10000
    gst = fee * GST_ON_FEE_BPS // 10000
    net = gross - fee - gst

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "gateway.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    with open(os.path.join(OUT, "settlements.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["settlement_id", "net_amount", "settled_at", "currency",
                    "declared_deductions"])
        w.writerow([BATCH, rupees(net), SETTLED.date().isoformat(), "INR",
                    rupees(fee + gst)])

    print(f"Aarna Organics - trading day {DAY.date()}, settled {SETTLED.date()}")
    print(f"  {len(rows)} payments")
    print(f"  gross           Rs {gross/100:>12,.2f}")
    print(f"  gateway fee 2%  Rs {fee/100:>12,.2f}")
    print(f"  GST 18% on fee  Rs {gst/100:>12,.2f}")
    print(f"  net settled     Rs {net/100:>12,.2f}")
    print(f"  -> {OUT}")
    return gross, net, fee + gst


if __name__ == "__main__":
    build()
