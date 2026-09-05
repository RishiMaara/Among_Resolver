"""
Fetch a corpus of REAL payment amounts, to stop the benchmark's hardest
assumption from being one we invented.

WHY
---
Every distribution in realistic_benchmark.py was a model. Uniform amounts are
obviously wrong. The "clustered" retail model is a considered guess, but a
guess: it asserts that payment amounts pile up on price points, and nothing in
the repository checked that against a real ledger.

Amount distribution is not a cosmetic detail here. Subset-sum degeneracy is
driven by how many distinct values exist and how often they repeat, so the
amount model IS the difficulty knob. Getting it wrong makes every accuracy
number either flattering or alarmist, and there was no way to tell which.

WHAT THIS FETCHES
-----------------
USAspending.gov — the US federal government's public spending API. Real
contract and grant disbursements: real amounts, real dates, real awarding
agencies, real recipient organisations. It is open data with no registration
and no personal information: recipients are companies and institutions, not
individuals.

WHAT IT IS NOT
--------------
Federal awards are INSTITUTIONAL payments. A payment gateway's traffic is
retail, and retail genuinely does cluster on price points in a way bespoke
contract values do not. So this corpus is not a stand-in for gateway traffic,
and the benchmark does not claim it is — it is a third, measured point next to
two modelled ones, and the three disagreeing is itself the finding.

WHAT THE FIRST FETCH SHOWED
---------------------------
Measured over 2,000 records from 2024:

                      distinct   in a collision   leading digit 1/2/3
  REAL                  96.1%          6.0%        34.5 / 18.4 / 11.4
  clustered (model)     30.9%         74.4%        31.5 / 17.8 / 12.5
  uniform (original)   100.0%          0.1%        28.3 / 28.1 / 26.6

  Benford's law expects                            30.1 / 17.6 / 12.5

Two corrections fall out. The original uniform generator violates Benford
outright — a flat leading-digit distribution does not occur in real financial
data, so it was not merely an easy distribution, it was the wrong shape. And
the clustered model over-corrects: it assumes 74% of amounts collide where
this corpus shows 6%.

Run:
    python scripts/fetch_real_payments.py
    python scripts/fetch_real_payments.py --pages 40 --year 2023
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

API = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
OUT = Path(__file__).resolve().parents[1] / "data" / "real" / "usaspending_2024.json"


def fetch(pages: int, year: int, timeout: int = 60) -> list[dict]:
    records: list[dict] = []
    for page in range(1, pages + 1):
        body = json.dumps({
            "filters": {
                "award_type_codes": ["A", "B", "C", "D"],
                "time_period": [{"start_date": f"{year}-01-01",
                                 "end_date": f"{year}-12-31"}],
            },
            "fields": ["Award ID", "Recipient Name", "Award Amount",
                       "Start Date", "Awarding Agency", "Description"],
            # Sorted by date, not amount: sorting by size would hand back the
            # tail and call it a sample.
            "page": page, "limit": 100, "sort": "Start Date", "order": "desc",
        }).encode()
        req = urllib.request.Request(
            API, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                batch = json.load(resp).get("results", [])
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"  page {page} failed ({type(e).__name__}: {e}); "
                  f"keeping {len(records)} records fetched so far",
                  file=sys.stderr)
            break
        if not batch:
            break
        records.extend(batch)
        print(f"  page {page:>3}: +{len(batch):>3}  (total {len(records)})",
              file=sys.stderr)
        time.sleep(0.2)          # be a good citizen of a public API
    return records


def describe(amounts: list[int]) -> None:
    """The three numbers that decide how hard this corpus is to reconcile."""
    n = len(amounts)
    counts = Counter(amounts)
    shared = sum(c for c in counts.values() if c > 1)
    leading = Counter(int(str(abs(a)).lstrip("0")[0]) for a in amounts if a)

    print(f"  records          : {n:,}")
    print(f"  median           : {statistics.median(amounts) / 100:,.2f}")
    print(f"  distinct values  : {100 * len(counts) / n:.1f}%")
    print(f"  in a collision   : {100 * shared / n:.1f}%")
    print(f"  max repeat       : {max(counts.values())}")
    print("  leading digit    : "
          + ", ".join(f"{d}:{100 * leading[d] / n:.1f}%" for d in (1, 2, 3))
          + "   (Benford: 1:30.1%, 2:17.6%, 3:12.5%)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pages", type=int, default=20, help="100 records per page")
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    print(f"Fetching real payment records from USAspending.gov "
          f"({args.pages} pages, {args.year})…", file=sys.stderr)
    records = fetch(args.pages, args.year)
    if not records:
        print("No records fetched. The API may be unreachable; the existing "
              "corpus (if any) is left untouched.", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records), encoding="utf-8")

    amounts = [round(r["Award Amount"] * 100) for r in records
               if isinstance(r.get("Award Amount"), (int, float))
               and r["Award Amount"] > 0]
    print(f"\nWrote {len(records):,} records to {out}")
    describe(amounts)
    print("\nUse with:  python scripts/realistic_benchmark.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
