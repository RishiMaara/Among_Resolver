"""
Point Agent 0 at real government payment feeds and see what it does.

Every schema the header mapper has been measured against so far was either
written by us or shipped with a benchmark we chose. This runs it against six
live public datasets from six unrelated systems, downloaded rather than
authored, and reports what it mapped, what it dropped, and where it was
wrong. The interesting output is not the pass rate — it is the specific ways
a real feed breaks an assumption.
"""
import io
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import file_agent

DATA = sys.argv[1] if len(sys.argv) > 1 else "."
FILES = ["chicago", "vermont", "cincinnati", "mesa", "cdc_arp", "nyc_payments"]


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def run(name):
    path = os.path.join(DATA, name + ".csv")
    if not os.path.exists(path):
        return None
    raw = io.open(path, encoding="utf-8", errors="replace").read()
    # Cap the giant ones; the mapping decision is made from the header anyway.
    lines = raw.splitlines()
    if len(lines) > 4001:
        raw = "\n".join(lines[:4001])
    headers = lines[0]

    cap = Capture()
    root = logging.getLogger()
    prev = root.level
    root.setLevel(logging.INFO)
    root.addHandler(cap)
    try:
        rows = file_agent.parse_csv(raw, name + ".csv")
        err = None
    except Exception as e:
        rows, err = [], f"{type(e).__name__}: {e}"
    finally:
        root.removeHandler(cap)
        root.setLevel(prev)

    return {
        "name": name,
        "headers": headers,
        "input_rows": len(lines) - 1,
        "parsed": len(rows),
        "error": err,
        "sample": rows[0] if rows else None,
        "warnings": [w for w in cap.lines if "header mapping" in w or "dropped" in w],
    }


print("=" * 78)
print("AGENT 0 vs SIX REAL PUBLIC PAYMENT FEEDS")
print("=" * 78)

for name in FILES:
    r = run(name)
    if r is None:
        print(f"\n[{name}] file missing, skipped")
        continue
    print(f"\n{'-' * 78}\n[{r['name']}]  {r['input_rows']} input rows")
    print(f"  headers: {r['headers'][:150]}")
    if r["error"]:
        print(f"  RESULT: HARD FAILURE -> {r['error']}")
        continue
    kept = r["parsed"]
    pct = (kept / r["input_rows"] * 100) if r["input_rows"] else 0
    print(f"  RESULT: {kept}/{r['input_rows']} rows parsed ({pct:.1f}%)")
    if r["sample"]:
        s = r["sample"]
        print("  mapped ->")
        for f in ("txn_id", "ref_id", "amount", "currency", "timestamp", "memo"):
            v = s.get(f)
            print(f"      {f:<10} = {str(v)[:56]!r}")
    for w in r["warnings"][:5]:
        print(f"  ! {w[:200]}")
