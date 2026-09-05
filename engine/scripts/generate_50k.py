"""
Generate the 50K multi-source stress dataset.

WHY THIS WAS REWRITTEN
----------------------
The previous generator rigged the problem, and said so in its own comments:

    # Noise subset (Massive amounts: > Rs 30,000 so they can NEVER be
    # included in the subset)
    # By making noise strictly > Rs 30,000, we mathematically guarantee
    # CP-SAT finds only 1 unique subset.

It produced a settlement of Rs 80.34 against a pool whose median transaction
was Rs 42,709. Exactly 5 of 50,100 records were small enough to participate,
and those were the 5 true ones — so the "hard" 50K reconciliation had a real
search space of 2^5 = 32. It demonstrated throughput and proved nothing
whatsoever about matching. Its ground truth recorded only
`true_subset_count: 5`, so the stress test could only check count-and-sum:
ANY five transactions summing to Rs 80.34 would have passed.

This version removes the rigging:

  * True members and noise are drawn from the SAME amount distribution, so
    the target is not isolated. Tens of thousands of candidates can
    legitimately participate and enormous numbers of subsets sum to the
    target. Arithmetic alone cannot identify the settlement — which is the
    honest situation, and the one the linkage stage exists to handle.

  * The settlement is ~55 transactions rather than 5, matching the brief's
    "50+ record batch" and what a real gateway settlement looks like.

  * Ground truth records the actual transaction IDs, so accuracy can be
    checked by IDENTITY instead of by count.

  * Members carry the settlement reference in their reference chain, the way
    a real gateway settlement report tags them. This is the signal the
    engine is supposed to use, and it is present because it is realistic —
    not because it makes the demo easy. Decoy settlements are included so
    the engine must pick the RIGHT settlement's members, not merely
    something that references a settlement.

  * Multi-source: ERP journal mirrors of the true payments carry the same
    amounts, so the pool contains substitutable records and the engine has
    to choose the correct system of record.

Run from engine/:
    python scripts/generate_50k.py
"""

import os
import csv
import json
import random
from datetime import datetime, timezone, timedelta

OUT_DIR = os.path.join("data", "50k_stress")

SETTLEMENT_ID = "STL20260818001"      # the batch under reconciliation
DECOY_SETTLEMENTS = [                  # other settlements in the same window
    "STL20260817001", "STL20260819001", "STL20260816001",
]

TRUE_SUBSET_SIZE = 55
GATEWAY_TOTAL = 25_000
BANK_TOTAL = 15_000
ERP_TOTAL = 10_000


def realistic_amount_cents(rng: random.Random) -> int:
    """
    A payment-gateway-shaped amount distribution: mostly small consumer
    payments, a long tail of larger ones.

    Critically, TRUE members and noise both come from here. The previous
    generator drew them from disjoint ranges, which is what made the target
    trivially isolated.
    """
    r = rng.random()
    if r < 0.55:
        return rng.randint(9_900, 200_000)        # Rs 99 - Rs 2,000
    if r < 0.85:
        return rng.randint(200_000, 1_000_000)    # Rs 2,000 - Rs 10,000
    if r < 0.97:
        return rng.randint(1_000_000, 5_000_000)  # Rs 10,000 - Rs 50,000
    return rng.randint(5_000_000, 25_000_000)     # Rs 50,000 - Rs 2,50,000


def generate():
    os.makedirs(OUT_DIR, exist_ok=True)
    rng = random.Random(42)
    base_time = datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc)
    settled_at = base_time + timedelta(hours=72)

    gw_rows = []
    true_ids = []
    gross_target_cents = 0

    # ── the settlement's true members ─────────────────────────────────────
    # Tagged with the settlement id in the reference chain, exactly as a
    # gateway settlement report tags the payments it paid out.
    print(f"Generating {TRUE_SUBSET_SIZE} true settlement members...")
    for i in range(TRUE_SUBSET_SIZE):
        amount = realistic_amount_cents(rng)
        gross_target_cents += amount
        pid = f"pay_{100000 + i}"
        true_ids.append(pid)
        ts = base_time + timedelta(hours=rng.uniform(0, 48))
        gw_rows.append({
            "Payment ID": pid,
            "Order ID": f"{SETTLEMENT_ID}-ORD{200000 + i}",
            "Amount (INR)": f"{amount / 100:.2f}",
            "Timestamp": ts.isoformat(),
            "Memo": f"Sale settled in {SETTLEMENT_ID}",
            "Payer": f"customer_{rng.randint(1, 1000)}",
        })

    # ── gateway noise, same amount distribution ───────────────────────────
    # A slice references OTHER settlements, so the engine has to select the
    # right settlement's members rather than anything settlement-shaped.
    noise_count = GATEWAY_TOTAL - TRUE_SUBSET_SIZE - 103  # 103 compliance rows
    print(f"Generating {noise_count} gateway noise transactions...")
    for i in range(noise_count):
        amount = realistic_amount_cents(rng)
        ts = base_time - timedelta(days=2) + timedelta(hours=rng.uniform(0, 120))
        if rng.random() < 0.25:
            order_ref = f"{rng.choice(DECOY_SETTLEMENTS)}-ORD{300000 + i}"
        else:
            order_ref = f"ORD{300000 + i}"
        gw_rows.append({
            "Payment ID": f"pay_noise_{i}",
            "Order ID": order_ref,
            "Amount (INR)": f"{amount / 100:.2f}",
            "Timestamp": ts.isoformat(),
            "Memo": f"Sale {300000 + i}",
            "Payer": f"customer_{rng.randint(1001, 5000)}",
        })

    # ── compliance triggers (unchanged behaviour, still exercised) ────────
    gw_rows.append({
        "Payment ID": "pay_extreme_001",
        "Order ID": "ORD_EXTREME_001",
        "Amount (INR)": "60000000.00",
        "Timestamp": (base_time + timedelta(hours=10)).isoformat(),
        "Memo": "Massive corporate payment",
        "Payer": "corp_1",
    })
    gw_rows.append({
        "Payment ID": "pay_sanction_001",
        "Order ID": "ORD_SANCTION_001",
        "Amount (INR)": "5000.00",
        "Timestamp": (base_time + timedelta(hours=11)).isoformat(),
        "Memo": "UN_TERROR_2",
        "Payer": "UN_TERROR_2",
    })
    vel_start = base_time + timedelta(hours=12)
    for i in range(101):
        gw_rows.append({
            "Payment ID": f"pay_vel_{i}",
            "Order ID": f"ORD_VEL_{i}",
            "Amount (INR)": "100.00",
            "Timestamp": (vel_start + timedelta(seconds=i * 30)).isoformat(),
            "Memo": "Micro payment",
            "Payer": "spammer_account",
        })

    rng.shuffle(gw_rows)
    with open(os.path.join(OUT_DIR, "gateway_report.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Payment ID", "Order ID", "Amount (INR)",
                                          "Timestamp", "Memo", "Payer"])
        w.writeheader()
        w.writerows(gw_rows)

    # ── bank statement, including the settlement credit itself ────────────
    print(f"Generating {BANK_TOTAL} bank rows...")
    gateway_fee = round(gross_target_cents * 0.02)
    tax_withheld = round(gross_target_cents * 0.01)
    net_target_cents = gross_target_cents - gateway_fee - tax_withheld

    bank_rows = [{
        "Bank Ref": f"UTR_{SETTLEMENT_ID}",
        "Debit": "",
        "Credit": f"{net_target_cents / 100:.2f}",
        "Value Date": settled_at.strftime("%Y-%m-%d"),
        "Description": f"Gateway settlement {SETTLEMENT_ID}",
        "Counterparty": "Payment Gateway",
    }]
    for i in range(BANK_TOTAL - 1):
        amount = realistic_amount_cents(rng)
        ts = base_time - timedelta(days=2) + timedelta(hours=rng.uniform(0, 120))
        bank_rows.append({
            "Bank Ref": f"UTR_NOISE_{i}",
            "Debit": "",
            "Credit": f"{amount / 100:.2f}",
            "Value Date": ts.strftime("%Y-%m-%d"),
            "Description": f"Miscellaneous deposit {i}",
            "Counterparty": f"Corp {rng.randint(1, 100)}",
        })
    with open(os.path.join(OUT_DIR, "bank_statement.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Bank Ref", "Debit", "Credit",
                                          "Value Date", "Description", "Counterparty"])
        w.writeheader()
        w.writerows(bank_rows)

    # ── ERP ledger, including journal mirrors of the true payments ────────
    # Same amounts as the gateway records they mirror, so they are
    # substitutable and the engine must pick the right system of record.
    print(f"Generating {ERP_TOTAL} ERP rows (incl. mirrors of true members)...")
    erp_rows = []
    for i, pid in enumerate(true_ids):
        gw = next(r for r in gw_rows if r["Payment ID"] == pid)
        erp_rows.append({
            "erp_id": f"JRNL_MIRROR_{i}",
            "ref": f"JRNL{900000 + i}",
            "amt": float(gw["Amount (INR)"]),
            "currency": "INR",
            "date": gw["Timestamp"],
            "notes": f"Revenue recognition for {pid}",
        })
    for i in range(ERP_TOTAL - len(true_ids)):
        amount = realistic_amount_cents(rng)
        ts = base_time - timedelta(days=2) + timedelta(hours=rng.uniform(0, 120))
        erp_rows.append({
            "erp_id": f"JRNL_{i}",
            "ref": f"JRNL{400000 + i}",
            "amt": amount / 100,
            "currency": "INR",
            "date": ts.isoformat(),
            "notes": "Revenue recognition",
        })
    rng.shuffle(erp_rows)
    with open(os.path.join(OUT_DIR, "erp_ledger.json"), "w", encoding="utf-8") as f:
        json.dump(erp_rows, f, indent=2)

    # ── config, now with ground-truth IDs ─────────────────────────────────
    config = {
        "batch_id": SETTLEMENT_ID,
        "target_net_amount_inr": round(net_target_cents / 100, 2),
        "target_gross_amount_inr": round(gross_target_cents / 100, 2),
        "settled_at_utc": settled_at.isoformat(),
        "member_source": "gateway",
        "true_subset_count": len(true_ids),
        # Recorded so accuracy is checked by IDENTITY, not by count-and-sum.
        # The previous config omitted this, which is why the old stress test
        # would have accepted any five transactions summing to the target.
        "true_subset_ids": true_ids,
        "total_rows": len(gw_rows) + len(bank_rows) + len(erp_rows),
    }
    with open(os.path.join(OUT_DIR, "batch_config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    # ── honesty check: is the target actually contested? ──────────────────
    all_amounts = (
        [int(round(float(r["Amount (INR)"]) * 100)) for r in gw_rows]
        + [int(round(float(r["Credit"]) * 100)) for r in bank_rows if r["Credit"]]
        + [int(round(r["amt"] * 100)) for r in erp_rows]
    )
    eligible = sum(1 for a in all_amounts if a <= gross_target_cents)

    print("\nDataset generation complete.")
    print(f"  total rows            : {config['total_rows']:,}")
    print(f"  settlement            : {SETTLEMENT_ID}")
    print(f"  true members          : {len(true_ids)} (ids recorded in config)")
    print(f"  gross target          : Rs {config['target_gross_amount_inr']:,}")
    print(f"  net settled           : Rs {config['target_net_amount_inr']:,}")
    print(f"  candidates <= target  : {eligible:,} of {len(all_amounts):,} "
          f"({100 * eligible / len(all_amounts):.1f}%)")
    if eligible < 1000:
        print("  WARNING: target is isolated — the arithmetic is trivially "
              "constrained and this dataset proves nothing about matching.")
    else:
        print("  -> target is genuinely contested; arithmetic alone cannot "
              "identify the settlement, linkage must.")


if __name__ == "__main__":
    generate()
