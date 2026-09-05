#!/usr/bin/env python3
"""
Pull real settlements from Razorpay and reconcile them, no CSV in the middle.

GETTING KEYS (test mode — no real money, no KYC needed)
--------------------------------------------------------
1. Sign up at https://dashboard.razorpay.com/signup
2. Switch the dashboard toggle from Live to **Test Mode**
3. Account & Settings -> API Keys -> Generate Test Key
4. Copy both halves. The secret is shown ONCE; if you lose it, regenerate.

    export RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxx
    export RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx

A brand-new test account has no settlements, so this will correctly report
finding none, and --fixture exists for exactly that case.

GETTING LIVE SETTLEMENT DATA, IF YOU WANT IT
--------------------------------------------
Verified read-only against a real test key: a fresh account returns 0
settlements, 0 payments, 0 orders, 0 refunds, and `GET /v1/balance` shows a
balance of 0. Ordinary test settlements are produced on Razorpay's own
schedule — days, not minutes — so waiting is not a demo strategy.

The shortcut is on-demand settlement. `GET /v1/settlements/ondemand` answers
on a fresh test account (an empty collection rather than a 404), so the
endpoint is available; it pays out from balance, which is why an account with
none produces nothing. The chain to real data is therefore:

    1. POST /v1/orders                       create an order
    2. complete it via Checkout              test card 4111 1111 1111 1111
    3. balance rises                          GET /v1/balance to confirm
    4. POST /v1/settlements/ondemand         forces a settlement now
    5. this script, with --month set to the current month

Steps 1, 2 and 4 write to the account, which is why this script does not do
them: it only ever GETs. Whether on-demand settlement is gated behind KYC
activation on a non-activated account was not established.

WHAT IT DOES
------------
    /v1/settlements                  -> the payouts to reconcile
    /v1/settlements/recon/combined   -> every line, each naming its settlement

Then, for each settlement, it runs the same reconcile_settlement() an uploaded
CSV goes through and reports whether the arithmetic ties out.

WHY THIS FEED IS THE EASY CASE, SAID PLAINLY
---------------------------------------------
The recon report carries `settlement_id` on every line, so linkage is handed a
perfect anchor and the reconciliation is close to trivial. That is not a
weakness of the demonstration, but it should not be oversold either: the
interesting claim here is not "the engine can find the members" — with this
feed almost anything could. It is that the engine PROVES the tie-out to the
paisa, names every line, and refuses to clear when the arithmetic does not
close. --verify-only exercises exactly that check and nothing else.

Run from engine/:
    python scripts/pull_razorpay.py --month 2026-09
    python scripts/pull_razorpay.py --month 2026-09 --verify-only
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import razorpay_source as rz                      # noqa: E402
from fee_decomposition import FeeRateCard         # noqa: E402
from pipeline import reconcile_settlement         # noqa: E402

# The recon report's `credit` is already net of fee and tax, so there is
# nothing to add back. Applying the engine's default 2%+1% card here would
# inflate the target off the answer entirely — the same trap run_reconriver.py
# documents for its own feed.
NET_TO_NET = FeeRateCard(gateway_fee_bps=0, flat_fee_cents=0, tax_withholding_bps=0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="YYYY-MM, e.g. 2026-09")
    ap.add_argument("--day", type=int, default=None)
    ap.add_argument("--count", type=int, default=10, help="settlements to reconcile")
    ap.add_argument("--window", type=int, default=5, help="settlement window, days")
    ap.add_argument("--verify-only", action="store_true",
                    help="check the tie-out arithmetic and stop, without reconciling")
    ap.add_argument("--fixture", action="store_true",
                    help="use data/razorpay_fixture.json instead of the network. "
                         "Response SHAPES are Razorpay's published contract; the "
                         "values are invented. Says so on every run.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    if args.fixture:
        import json
        path = os.path.join(os.path.dirname(__file__), "..", "data",
                            "razorpay_fixture.json")
        with open(path, encoding="utf-8") as f:
            fx = json.load(f)
        print("FIXTURE MODE - no network call was made.")
        print("  Field names, types and units are Razorpay's published contract;")
        print("  the values are invented. This shows the mapping and the")
        print("  reconciliation working, NOT that a live account agrees.")
        print(f"  {len(fx['settlements'])} settlement(s), "
              f"{len(fx['recon'])} recon line(s)\n")
        return _reconcile_all(fx["settlements"], fx["recon"], args)

    creds = rz.credentials_from_env()
    if not creds:
        print("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set.")
        print("See the header of this file for how to get test-mode keys.")
        return 1

    try:
        year, month = (int(x) for x in args.month.split("-"))
    except ValueError:
        print(f"--month must be YYYY-MM, got {args.month!r}")
        return 1

    mode = "TEST" if creds.is_test_mode else "LIVE"
    print(f"Razorpay {mode} mode, key {creds.key_id[:12]}…")
    if mode == "LIVE":
        # Read-only, but say so out loud rather than let someone discover it.
        print("  (reading live account data; this script only ever GETs)")

    try:
        print(f"Fetching settlements …")
        settlements = rz.fetch_settlements(creds, count=args.count)
        print(f"Fetching recon lines for {args.month} …")
        recon = rz.fetch_recon(creds, year, month, args.day)
    except rz.RazorpayError as e:
        print(f"\n{e}")
        return 1

    print(f"  {len(settlements)} settlement(s), {len(recon)} recon line(s)\n")
    if not settlements:
        print("No settlements in this account yet. A new test account has none "
              "until it has taken some test payments and Razorpay has settled "
              "them on its own schedule - days, not minutes. To watch the "
              "reconciliation work meanwhile: --fixture")
        return 0

    return _reconcile_all(settlements, recon, args)


def _reconcile_all(settlements: list[dict], recon: list[dict], args) -> int:
    """
    The same loop for live data and for the fixture. Shared deliberately: a
    fixture that exercised different code would demonstrate nothing about the
    code that runs against a real account.
    """
    pool = [t for t in (rz.recon_item_to_txn(i) for i in recon) if t]

    ok = failed = 0
    for s in settlements:
        tie = rz.verify_tie_out(s, recon)
        sid = tie["settlement_id"]

        if not tie["members"]:
            print(f"{sid}: no recon lines in this period — widen --month/--day")
            continue

        mark = "ties out" if tie["ties_out"] else f"OFF BY {tie['residual_paise']}p"
        print(f"{sid}: {tie['members']} member(s), "
              f"sum {tie['sum_of_net_paise']}p vs settlement "
              f"{tie['settlement_amount_paise']}p — {mark}")

        if not tie["ties_out"]:
            # Worth stopping on. It means the mapping in razorpay_source is
            # wrong for this account's data, and every reconciliation below
            # would be solving for the wrong target.
            print("   ^ the credit-debit mapping does not reproduce this "
                  "settlement's amount. Fix that before trusting a match.")
            failed += 1
            continue
        ok += 1

        if args.verify_only:
            continue

        report = reconcile_settlement(
            rz.settlement_to_batch(s), pool,
            settlement_window_days=args.window, rate_card=NET_TO_NET,
        )
        m = report.match_result
        truth = {i["entity_id"] for i in rz.members_of(sid, recon)}
        got = set(m.matched_txn_ids)
        print(f"   cleared={m.cleared} confidence={m.confidence:.2f} "
              f"matched={len(got)}/{len(pool)} exact={got == truth} "
              f"residual={report.target_cents - m.matched_sum_cents}p")

    print(f"\n{ok} settlement(s) tie out, {failed} do not.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
