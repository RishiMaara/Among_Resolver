"""
Operational endpoints: the audit trail and its hash-chain check, the
10K-scale demo (off on Vercel), and readiness.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

import audit
import auth
import compliance_agent
import history
import india_calendar
import webhook
from api.config import CORS_ORIGINS, CORS_ORIGIN_REGEX
from orchestrator import reconcile_batch
from schema import NormalizedTxn, SettlementBatch, SourceType

router = APIRouter()


@router.get("/audit/{batch_id}/verify", summary="Check a batch's audit trail has not been altered")
def verify_audit(batch_id: str, receipt: str = ""):
    """
    Walk the hash chain. With `receipt` — the `audit_head` a reconciliation
    returned — also prove nothing after it was cut off.
    """
    return audit.verify_chain(batch_id, receipt=receipt.strip())


@router.get("/audit/{batch_id}", summary="Retrieve full audit trail for a batch")
def get_audit(batch_id: str):
    """
    Returns the complete ordered decision log for the given batch — every
    agent call, reasoning trace, and confidence score. Use this to explain
    any reconciliation decision to an auditor without rerunning the pipeline.
    """
    trail = audit.get_audit_trail(batch_id)
    if not trail:
        raise HTTPException(
            status_code=404,
            detail=f"No audit trail found for batch_id='{batch_id}'. "
                   "Either the batch hasn't been reconciled yet, or the "
                   "audit store was cleared."
        )
    return {"batch_id": batch_id, "event_count": len(trail), "trail": trail}


@router.get("/demo", summary="Live 10K-scale reconciliation demo with timing")
def demo():
    """
    Runs the brief's own '412 out of 10,000' scenario end-to-end and returns
    timing, match result, and exception breakdown. Designed to demonstrate
    the CP-SAT solver at real scale during a live hackathon presentation.

    The 'naive_dp_would_take_seconds' field documents the algorithmic
    improvement — naive DP at Rs 60,000 settlement scale takes ~108s;
    CP-SAT solves the same problem in <2s.
    """
    # A 10,000-record solve on every call, reachable without a key, is a way
    # to spend a public deployment's compute. Off on Vercel unless asked for.
    if os.environ.get("VERCEL") and os.environ.get("ENABLE_DEMO_ENDPOINT", "").strip() != "1":
        raise HTTPException(status_code=404, detail=(
            "/demo is off on this deployment; set ENABLE_DEMO_ENDPOINT=1 to run it."))
    import random
    from datetime import timedelta
    from schema import TzConfidence
    from subset_sum import SubsetSumConfig

    random.seed(99)
    base_time = datetime(2026, 8, 15, tzinfo=timezone.utc)

    # Generate 10,000 gateway transactions
    all_txns = []
    for i in range(10_000):
        amount_cents = random.randint(5000, 500000)  # Rs 50 - Rs 5000
        ts = base_time + timedelta(hours=random.uniform(0, 72))
        all_txns.append(NormalizedTxn(
            source=SourceType.GATEWAY,
            source_txn_id=f"GW{i:06d}",
            ref_id_canonical=f"RZP{110000 + i}",
            amount_cents=amount_cents,
            currency="INR",
            timestamp_utc=ts,
            tz_confidence=TzConfidence.HIGH,
            memo_raw=f"Payment order {10000 + i}",
            memo_normalized=f"payment order {10000 + i}",
        ))

    # Pick a true subset of 412 transactions
    true_subset = random.sample(all_txns, 412)
    gross_sum = sum(t.amount_cents for t in true_subset)
    gw_fee = round(gross_sum * 0.02)
    tax_wh = round(gross_sum * 0.01)
    net_amount = gross_sum - gw_fee - tax_wh

    settled_at = base_time + timedelta(hours=100)
    batch = SettlementBatch(
        batch_id="DEMO-10K",
        net_amount_cents=net_amount,
        currency="INR",
        settled_at_utc=settled_at,
    )

    # CP-SAT solve timing
    t0 = time.perf_counter()
    report = reconcile_batch(
        batch, all_txns,
        subset_config=SubsetSumConfig(
            tolerance_cents=10,
            solver_time_limit_s=20.0,
            ambiguity_probe_limit=2,
        ),
        settlement_window_days=5,
    )
    elapsed = time.perf_counter() - t0

    return {
        "demo": {
            "scenario": "412 true transactions out of 10,000 candidates",
            "true_subset_size": 412,
            "gross_sum_inr": gross_sum / 100,
            "net_settlement_inr": net_amount / 100,
        },
        "timing": {
            "cp_sat_total_pipeline_seconds": round(elapsed, 2),
            "naive_dp_would_take_seconds": "~108s+ at this rupee scale (measured — see README)",
            "speedup_factor": f">{round(108 / elapsed, 0):.0f}x" if elapsed > 0 else "N/A",
        },
        "result": report.summary(),
        "matched_txn_ids_sample": report.match_result.matched_txn_ids[:10],
        "exceptions_sample": [
            {"reason": e.reason.value, "note": e.diagnosis_note}
            for e in report.exceptions[:5]
        ],
    }


@router.get("/health", summary="Liveness and readiness")
def health():
    """
    What an operator needs before trusting a deployment, not just a 200.

    A health check that only says "the process is up" is the one that lets a
    node serve traffic while its audit trail is being written somewhere the
    OS will delete. Each field below is something that can be silently wrong
    and that changes whether the answers should be relied on.
    """
    storage = audit.storage_status()
    history_storage = history.storage_status()
    webhook_storage = webhook.storage_status()
    sanctions = compliance_agent.sanctions_provenance()
    warnings = []
    # Serverless is where private state stops being a durability footnote and
    # becomes a correctness problem: consecutive requests from one browser can
    # land on different instances, so the agent-flow view asking for a trail a
    # moment ago's reconcile wrote can be answered by an instance that never
    # saw it. Named explicitly rather than folded into the durability warning,
    # because the fix is different — a shared store, not a disk path.
    if os.environ.get("VERCEL") and not (
        storage.get("backend") == "redis" and history_storage.get("shared")
    ):
        warnings.append(
            "Running on Vercel without a shared store: each instance keeps its "
            "own audit trail and run history, so a request served by another "
            "instance will not see a run recorded here. Connect Upstash Redis "
            "(REDIS_URL or KV_URL)."
        )
    if not storage["durable"]:
        warnings.append(
            "Audit trail is NOT durable — entries are under the system temp "
            "directory and will not survive a reboot. Set AUDIT_DB_PATH or "
            "configure Redis."
        )
    if sanctions["is_illustrative"]:
        warnings.append(
            "Sanctions screening uses the illustrative built-in list, not a "
            "real one. Run scripts/fetch_sanctions_list.py."
        )
    if auth.status_label() == "disabled":
        warnings.append(
            "API authentication is disabled — every endpoint is open to "
            "anything that can reach this port. Set API_KEY."
        )
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "auth": auth.status_label(),
        "audit_storage": storage,
        "history_storage": history_storage,
        "webhook_storage": webhook_storage,
        # Which holidays working-day ageing counts. A thinner calendar would
        # call items late on festival days; say which one is in use.
        "bank_calendar": india_calendar.source(),
        "sanctions_list": sanctions,
        "cors_origins": CORS_ORIGINS,
        "cors_origin_regex": CORS_ORIGIN_REGEX,
        "ready_for_production": not warnings,
        "warnings": warnings,
    }
