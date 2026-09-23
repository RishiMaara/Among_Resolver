"""
The engine under concurrent load: throughput, latency percentiles, errors.

One engine process, driven in-process over ASGI (no network), so the figures
are the engine's own and not a hosting provider's. Three request mixes, each
at rising concurrency:

  upload     POST /reconcile/upload, the sample payout (three files, 91
             candidates), member feed declared
  razorpay   POST /razorpay/reconcile/upload, five payouts checked five ways
  health     GET /health

No model is called (the key is blanked) and every store points at a temporary
directory, so the run measures the engine and leaves nothing behind.

    python scripts/load_test.py [--requests 60] [--json out.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SAMPLE = ROOT.parent / "public" / "sample-data"


def _isolate() -> None:
    tmp = tempfile.mkdtemp(prefix="load_test_")
    os.environ.update({
        "GEMINI_API_KEY": "", "GOOGLE_API_KEY": "", "REDIS_URL": "", "KV_URL": "",
        "AUDIT_DB_PATH": os.path.join(tmp, "audit.sqlite3"),
        "HISTORY_DIR": os.path.join(tmp, "history"),
        "SETTLEMENT_CYCLE_STORE": "memory",
    })
    sys.path.insert(0, str(ROOT / "src"))


def _upload_request():
    files = {
        "gateway_file": ("gateway_report.csv", (SAMPLE / "gateway_report.csv").read_bytes()),
        "bank_file": ("bank_statement.csv", (SAMPLE / "bank_statement.csv").read_bytes()),
        "erp_file": ("erp_ledger.json", (SAMPLE / "erp_ledger.json").read_bytes()),
    }
    data = {"batch_id": "SETTLE-001", "net_amount": "66466.36", "currency": "INR",
            "settled_at": "2026-09-02T00:00:00Z", "settlement_window_days": "5",
            "declared_deductions": "2055.66", "member_source": "gateway"}
    return "POST", "/reconcile/upload", {"data": data, "files": files}


def _razorpay_request():
    base = SAMPLE / "razorpay"
    files = {name: (fn, (base / fn).read_bytes()) for name, fn in (
        ("settlements_file", "settlements.json"), ("recon_file", "recon_combined.json"),
        ("bank_file", "bank_statement.csv"), ("ledger_file", "ledger.json"))}
    return "POST", "/razorpay/reconcile/upload", {"files": files}


MIXES = {
    "upload": _upload_request,
    "razorpay": _razorpay_request,
    "health": lambda: ("GET", "/health", {}),
}


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1))))
    return round(ordered[k] * 1000, 1)


async def _run(app, make, requests: int, concurrency: int) -> dict:
    import httpx  # pylint: disable=import-outside-toplevel

    method, path, kwargs = make()
    latencies: list[float] = []
    errors = 0
    sem = asyncio.Semaphore(concurrency)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://engine") as client:
        async def one():
            nonlocal errors
            async with sem:
                t = time.perf_counter()
                r = await client.request(method, path, **kwargs)
                latencies.append(time.perf_counter() - t)
                if r.status_code != 200:
                    errors += 1

        start = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(requests)))
        wall = time.perf_counter() - start
    return {"concurrency": concurrency, "requests": requests, "errors": errors,
            "throughput_per_s": round(requests / wall, 2),
            "p50_ms": _pct(latencies, 50), "p95_ms": _pct(latencies, 95),
            "p99_ms": _pct(latencies, 99), "mean_ms": round(statistics.mean(latencies) * 1000, 1)}


def run(requests: int = 60, levels: tuple[int, ...] = (1, 4, 8)) -> dict:
    _isolate()
    import logging  # pylint: disable=import-outside-toplevel
    logging.disable(logging.WARNING)
    import main  # pylint: disable=import-outside-toplevel

    out = {}
    for name, make in MIXES.items():
        n = requests * (5 if name == "health" else 1)
        out[name] = [asyncio.run(_run(main.app, make, n, c)) for c in levels]
    return {
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cpu_count": os.cpu_count(),
        "python": sys.version.split()[0],
        "setup": "one engine process, in-process ASGI (no network), no model, SQLite store",
        "results": out,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", type=int, default=60)
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    report = run(args.requests)
    for name, rows in report["results"].items():
        for r in rows:
            print(f"{name:9s} c={r['concurrency']:<2d} {r['throughput_per_s']:7.2f}/s  "
                  f"p50 {r['p50_ms']:7.1f}ms  p95 {r['p95_ms']:7.1f}ms  errors {r['errors']}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
