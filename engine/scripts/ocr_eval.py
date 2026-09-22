"""
How often does OCR in the browser read a scanned statement right — and does
a wrong reading ever get through?

THE SET
-------
Scanned statements drawn by make_scanned_statements.py (seed 11): three
layouts Indian banks print, rotated, blurred and noised the way a scanner or
phone leaves a page, each with its known truth.

THE READER
----------
Tesseract.js, run in Node with the settings the app uses in the browser
(scripts/tesseract_read.mjs): English, one uniform block, the page at 3,400
pixels wide. The browser renders a PDF with pdf.js; here the page image is
scaled with Lanczos, which is close but not identical.

WHAT IS COUNTED
---------------
  right            the reading was accepted, and every line's date, amount
                   and balance matches the truth
  refused          the engine refused it — the balance did not follow, a line
                   could not be read — so nothing from it was used
  accepted_wrong   accepted, but some figure differs from the truth. This is
                   the number that must be zero: it is a misread that got in.

Without --model no model is called. With it, every scan the browser's
reading could not prove goes to the model, as it does in the app when a key
is configured, and the same three outcomes are counted for the pipeline as a
whole. Answers are cached by the scan's bytes, so re-scoring spends nothing.

From engine/:
    python scripts/ocr_eval.py
    python scripts/ocr_eval.py --json docs/benchmarks/ocr_eval.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

os.environ["GEMINI_API_KEY"] = ""      # the browser path only: no model
os.environ["GOOGLE_API_KEY"] = ""
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

import make_scanned_statements as mss  # noqa: E402
import statement_parsers as sp  # noqa: E402
from PIL import Image  # noqa: E402

WIDTH = 3400


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--json", default="")
    ap.add_argument("--model", action="store_true", help="send refused scans to the model")
    ap.add_argument("--cache", default="", help="model answers, keyed by scan hash")
    ap.add_argument("--pace", type=float, default=6.5, help="seconds between model calls")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    cases = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(args.n):
            truth = mss.random_truth(rng)
            layout = mss.LAYOUTS[i % len(mss.LAYOUTS)]
            img = mss.degrade(mss.draw(truth, layout), rng)
            data, name = mss.to_bytes(img, "pdf" if i % 2 == 0 else "jpg")
            page = Image.open(__import__("io").BytesIO(data)) if name.endswith(".jpg") else img
            page = page.convert("RGB")
            page = page.resize((WIDTH, round(page.height * WIDTH / page.width)), Image.LANCZOS)
            path = os.path.join(tmp, f"scan_{i:02d}.png")
            page.save(path)
            cases.append({"i": i, "layout": layout, "kind": name.rsplit(".", 1)[1],
                          "bytes": data, "name": name, "truth": truth, "png": path})
        out = subprocess.run(["node", "scripts/tesseract_read.mjs", *[c["png"] for c in cases]],
                             cwd=ROOT, capture_output=True, text=True, timeout=1800)
        if out.returncode != 0:
            raise SystemExit(out.stderr[-800:])
        read = {json.loads(line)["file"]: json.loads(line) for line in out.stdout.splitlines()
                if line.startswith("{")}

    rows = []
    for c in cases:
        r = read[c["png"]]
        t = c["truth"]
        want = [(d, amt, bal) for d, _desc, _ref, amt, bal in t.lines]
        try:
            st, _check = sp.parse(c["bytes"], c["name"], scan_text=r["text"])
            got = [(ln.booked, ln.amount_cents, ln.balance_cents) for ln in st.lines]
            same = (got == want and st.opening_cents == t.opening and st.closing_cents == t.closing)
            outcome, reason = ("right" if same else "accepted_wrong"), ""
        except sp.StatementUnreadable as exc:
            outcome = "refused"
            text = str(exc)
            reason = ("balance did not follow" if "does not follow" in text or "does not move" in text
                      else "total did not balance" if "does not balance" in text
                      else "no rows recognised" if "no transaction lines" in text
                      else "other")
        rows.append({"scan": c["i"], "layout": c["layout"], "kind": c["kind"],
                     "lines": len(t.lines), "confidence": r["confidence"],
                     "outcome": outcome, "reason": reason, "ocr_text": r["text"]})

    if args.model:
        _with_model(cases, rows, args)

    tally = Counter(r["outcome"] for r in rows)
    by_layout = {lay: dict(Counter(r["outcome"] for r in rows if r["layout"] == lay))
                 for lay in mss.LAYOUTS}
    result = {
        "scans": len(rows), "seed": args.seed, "reader": "tesseract.js 7, PSM 6, 3400 px",
        "right": tally["right"], "refused": tally["refused"],
        "accepted_wrong": tally["accepted_wrong"],
        "right_pct": round(100 * tally["right"] / len(rows), 1),
        "refusal_reasons": dict(Counter(r["reason"] for r in rows if r["reason"])),
        "by_layout": by_layout,
        "note": ("Accepted only when every line's running balance follows; refused scans "
                 "go to the model next where one is configured."),
    }
    if args.model:
        pipe = Counter(r.get("with_model", r["outcome"]) for r in rows)
        result["with_model"] = {
            "model": __import__("llm_provider").DEFAULT_MODEL,
            "right": pipe["right"], "refused": pipe["refused"],
            "accepted_wrong": pipe["accepted_wrong"],
            "right_pct": round(100 * pipe["right"] / len(rows), 1),
            "model_calls": sum(1 for r in rows if "with_model" in r),
        }
    print(json.dumps(result, indent=1))
    if args.json:
        result["cases"] = rows
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
        print(f"wrote {args.json}")


def _with_model(cases, rows, args) -> None:
    """The refused scans, read by the model after the browser's reading failed."""
    import hashlib
    import time
    from dotenv import load_dotenv
    import llm_provider
    del os.environ["GEMINI_API_KEY"], os.environ["GOOGLE_API_KEY"]
    load_dotenv(HERE.parent / ".env", override=True)
    if not llm_provider.is_configured():
        raise SystemExit("--model needs GEMINI_API_KEY in engine/.env")
    cache = json.loads(Path(args.cache).read_text()) if args.cache and Path(args.cache).exists() else {}
    real = llm_provider.generate
    calls = 0

    def cached(prompt, **kw):
        nonlocal calls
        key = hashlib.sha256(kw["attachments"][0][0]).hexdigest()[:20]
        if cache.get(key):
            return cache[key]
        if calls:
            time.sleep(args.pace)
        calls += 1
        cache[key] = real(prompt, **kw)
        return cache[key]

    llm_provider.generate = cached
    try:
        for c, r in zip(cases, rows):
            if r["outcome"] != "refused":
                continue
            t = c["truth"]
            want = [(d, amt, bal) for d, _desc, _ref, amt, bal in t.lines]
            try:
                st, _ = sp.parse(c["bytes"], c["name"], scan_text=r["ocr_text"])
                got = [(ln.booked, ln.amount_cents, ln.balance_cents) for ln in st.lines]
                r["with_model"] = ("right" if got == want and st.opening_cents == t.opening
                                   and st.closing_cents == t.closing else "accepted_wrong")
            except sp.StatementUnreadable as exc:
                r["with_model"], r["model_refusal"] = "refused", str(exc)[:200]
    finally:
        llm_provider.generate = real
        if args.cache:
            Path(args.cache).write_text(json.dumps(cache))


if __name__ == "__main__":
    main()
