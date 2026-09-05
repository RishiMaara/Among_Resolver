"""
FastAPI entrypoint for the AI Finance Controller — Reconciliation Engine.

Endpoints:
  POST /reconcile            — single gateway source + one settlement batch
  POST /reconcile/multi      — gateway + bank + ERP sources in one call
  GET  /audit/{batch_id}     — pull full audit trail for a batch
  GET  /demo                 — runs the 10K-scale demo and returns timing + results
  GET  /health               — liveness check

Run: uvicorn main:app --reload --app-dir src
"""

from __future__ import annotations
import time
import logging
from pathlib import Path
import os
from datetime import datetime, timezone
from typing import Optional

import dateutil.parser as dp
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from schema import SourceType, SettlementBatch, NormalizedTxn, TzConfidence
from ingestion import normalize_batch, normalize_batch_with_report, normalize_amount_to_cents
from orchestrator import reconcile_batch
import audit
import file_agent
# Load .env before anything reads os.environ.
#
# The engine reads GEMINI_API_KEY, API_KEY, AUDIT_DB_PATH and CORS_ORIGINS
# from the process environment, and nothing was reading a .env file — so a
# key put in one was silently ignored and the LLM features stayed off with no
# indication why. Exporting the variable in the shell works too, but only if
# it happens before the server starts, which is not obvious and is easy to
# get wrong after a restart.
#
# override=False: a variable already set in the real environment wins. A
# deployment's secrets must not be overridden by a file someone left in the
# working tree.
try:
    from dotenv import load_dotenv
    for _candidate in (
        Path(__file__).resolve().parents[1] / ".env",   # engine/.env
        Path(__file__).resolve().parents[2] / ".env",   # repo root
    ):
        if _candidate.is_file():
            load_dotenv(_candidate, override=False)
            logging.getLogger(__name__).info("Loaded environment from %s", _candidate)
except ImportError:
    # Optional. Without it the engine still reads real environment variables.
    pass

import auth
import compliance_agent
from fee_decomposition import DEFAULT_RATE_CARD, FeeRateCard
import compliance_rulebook as compliance_rulebook_mod
from pipeline import reconcile_settlement
from cash_position import build_cash_position
import erp_sync
import settlement_qa
import history
from plain_summary import plain_summary
import re
import auto_disposition
import settled_ledger

# Without this, every logger.info(...) call in the pipeline (compliance
# blocks, linkage narrowing, which tier cleared) is silently swallowed by
# Python's default WARNING log level -- a long run looks completely idle
# with zero terminal output. That silence was originally mistaken for a
# hang during the 50K stress test.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


app = FastAPI(
    title="AI Finance Controller — Reconciliation Engine",
    description=(
        "Multi-source reconciliation engine using CP-SAT subset-sum matching, "
        "fuzzy semantic fallback, and rule-based exception diagnosis. "
        "Designed for Razorpay Buildathon Track 04."
    ),
    version="1.0.0",
)

# CORS.
#
# This was allow_origins=["*"] with allow_credentials=True, which is two
# problems. It lets any site on the internet a user visits read this engine's
# settlement data, audit trails and compliance findings if it can reach the
# host — and for a finance tool that is the wrong default even in a demo. It
# is also an invalid pair: the CORS spec forbids a wildcard origin alongside
# credentials, so browsers reject it and the credentials flag never did
# anything.
#
# The default now names the local dev servers, which is what the demo
# actually needs. Set CORS_ORIGINS (comma-separated) to deploy elsewhere.
_DEV_ORIGINS = [
    "http://localhost:8080", "http://127.0.0.1:8080",   # this project's vite port
    "http://localhost:5173", "http://127.0.0.1:5173",   # vite default
]
_configured = os.environ.get("CORS_ORIGINS", "").strip()
CORS_ORIGINS = (
    [o.strip() for o in _configured.split(",") if o.strip()]
    if _configured else _DEV_ORIGINS
)
# Upload ceiling.
#
# Every read below used to be a bare `await file.read()`, which buffers the
# whole upload into memory before anything looks at it. Three of them run on
# a single /reconcile/upload request, so a large file does not need to be
# malicious to matter — a controller exporting a year of gateway traffic can
# produce one honestly, and the process dies with an OOM rather than a message
# that says what went wrong.
#
# 64 MB is generous for the intended input: the 50,000-record stress corpus is
# about 5 MB, so this leaves an order of magnitude of headroom over anything a
# real settlement export produces. Override with MAX_UPLOAD_MB where a genuine
# larger feed exists.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "64")) * 1024 * 1024

# Chunked, because the point is to STOP before the memory is spent. Reading it
# all and then checking len() detects the problem after paying for it.
_UPLOAD_CHUNK = 1024 * 1024


async def read_upload_capped(upload_file, *, limit: int = MAX_UPLOAD_BYTES) -> bytes:
    """
    Read an upload, refusing anything over `limit` before it is buffered.

    Raises 413 rather than 400: the request is well-formed, it is the size
    that is unacceptable, and a client should be able to tell those apart to
    know whether retrying a smaller file is worth it.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload_file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            name = getattr(upload_file, "filename", None) or "upload"
            raise HTTPException(
                status_code=413,
                detail=(
                    f"{name} is larger than the {limit // (1024 * 1024)} MB "
                    f"upload limit. Split the export, or raise MAX_UPLOAD_MB "
                    f"if this engine is being run against a genuinely larger "
                    f"feed."
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)


if CORS_ORIGINS == ["*"]:
    logging.getLogger(__name__).warning(
        "CORS is open to every origin. Any site a user visits can read this "
        "engine's settlement data and audit trails. Acceptable for a local "
        "demo; never for a deployment holding real settlements."
    )

@app.middleware("http")
async def _require_api_key(request, call_next):
    """
    Auth as middleware rather than a per-route dependency.

    A dependency has to be remembered on every new endpoint, and the one it
    is forgotten on is the one that leaks. Middleware defaults to closed and
    names its exceptions, so adding a route cannot accidentally add an
    unauthenticated route.
    """
    if request.method == "OPTIONS" or request.url.path in auth.PUBLIC_PATHS:
        return await call_next(request)
    expected = auth.configured_key()
    if expected is not None:
        supplied = request.headers.get("X-API-Key")
        if not supplied:
            hdr = request.headers.get("Authorization", "")
            if hdr.lower().startswith("bearer "):
                supplied = hdr[7:].strip()
        import hmac as _hmac
        if not supplied or not _hmac.compare_digest(supplied, expected):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid API key. Send it as "
                                   "X-API-Key or 'Authorization: Bearer <key>'."},
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)


auth.warn_if_open()

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    # Only meaningful once origins are explicit, which is now the default.
    allow_credentials=CORS_ORIGINS != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request/Response models ────────────────────────────────────────────────────

class TxnIn(BaseModel):
    txn_id: str
    ref_id: str
    amount: float
    currency: str = "INR"
    timestamp: str
    memo: str = ""


class SettlementBatchIn(BaseModel):
    batch_id: str
    net_amount: float
    currency: str = "INR"
    settled_at: str


class ReconcileRequest(BaseModel):
    gateway_txns: list[TxnIn] = Field(default_factory=list)
    settlement_batch: SettlementBatchIn
    settlement_window_days: int = Field(default=5, ge=1, le=30)


class MultiSourceReconcileRequest(BaseModel):
    """
    Full multi-source reconciliation. Pass any combination of gateway,
    bank, and ERP transactions — all 3 source types are normalized into
    the unified schema before the pipeline runs.
    """
    gateway_txns: list[TxnIn] = Field(default_factory=list)
    bank_txns: list[TxnIn] = Field(default_factory=list)
    erp_txns: list[TxnIn] = Field(default_factory=list)
    settlement_batch: SettlementBatchIn
    settlement_window_days: int = Field(default=5, ge=1, le=30)


def _build_settlement_batch(b: SettlementBatchIn) -> SettlementBatch:
    return SettlementBatch(
        batch_id=b.batch_id,
        net_amount_cents=normalize_amount_to_cents(b.net_amount),
        currency=b.currency,
        settled_at_utc=_settlement_instant(b.settled_at),
    )


def _format_report(report, audit_trail=None, batch=None, candidates=None) -> dict:
    # Closing the finance-ops loop: a matched set is not what a controller
    # consumes. Where we have the batch and the candidate pool, turn the
    # reconciliation into a cash position and a balanced posting proposal —
    # the two artifacts the track statement's "run the books and the cash
    # position" actually asks for. Nothing is ever posted; proposals await
    # human approval.
    cash = None
    if batch is not None and candidates is not None:
        cash_obj = build_cash_position(
            batch,
            candidates,
            matched_txn_ids=report.match_result.matched_txn_ids,
            exceptions=report.exceptions,
            cleared=report.match_result.cleared,
        )
        
        if report.match_result.cleared:
            erp_sync.push_to_erp(cash_obj)
            
        cash = cash_obj.to_dict()
        # Agent 8 reports itself. Without this the stage runs but leaves no
        # trace, so the flow visualiser marks it "not needed" — which is a
        # lie about work that actually happened. Any agent absent from the
        # trail is invisible to the reviewer as well as to the UI.
        journal = cash.get("journal")
        audit.log_decision(
            batch_id=batch.batch_id, agent="cash_position",
            detail=(
                f"{len(cash.get('buckets', []))} bucket(s) computed. "
                + (
                    f"Journal proposal {'balanced' if journal.get('balanced') else 'REJECTED as unbalanced'}"
                    f" ({journal.get('status')})."
                    if journal else
                    "No journal proposal: the batch did not clear, so there is "
                    "nothing to post."
                )
            ),
        )

    formatted = {
        "summary": report.summary(),
        "cash_position": cash,
        "matched_txn_ids": report.match_result.matched_txn_ids,
        "matched_transactions": matched_rows(report.match_result, candidates),
        "interchangeable": interchangeable_note(report.match_result, candidates),
        "compliance_review": compliance_review(candidates, report.batch_id),
        "exceptions": [
            {
                "reason": e.reason.value,
                "candidate_txn_ids": e.candidate_txn_ids,
                "diagnosis_note": e.diagnosis_note,
                "requires_human_approval": e.requires_human_approval,
                # Present for compliance blocks: the specific rule, whether it
                # is law or internal policy, and an official source the
                # reviewer can open. Empty for ordinary reconciliation
                # exceptions.
                "findings": [
                    {
                        "rule_id": f.rule_id,
                        "title": f.title,
                        "severity": f.severity,
                        "action": f.action,
                        "basis": f.basis.value,
                        "authority": f.authority,
                        "source_name": f.source_name,
                        "citation": f.citation,
                        "reference_url": f.reference_url,
                        "rule_text": f.rule_text,
                        "threshold_applied": f.threshold_applied,
                        "why": f.why,
                        "observed": f.observed,
                        "remediation": f.remediation,
                    }
                    for f in e.findings
                ],
            }
            for e in report.exceptions
        ],
        "audit_trail": audit_trail or audit.get_audit_trail(report.batch_id),
    }
    # Kept so a question can be answered from what the engine actually
    # recorded, rather than by re-running the match at question time.
    settlement_qa.store_result(report.batch_id, formatted)
    return formatted


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post("/reconcile", summary="Reconcile gateway transactions against a settlement batch")
def reconcile(req: ReconcileRequest):
    """
    Primary reconciliation endpoint. Accepts gateway transactions and a
    settlement batch. Runs the full 6-agent pipeline and returns:
    - match_rate, matched_txn_ids
    - exception list with root-cause categories
    - false_positive_cost_estimate (financial materiality of a wrong match)
    - full audit trail of every decision made
    """
    candidates = normalize_batch(
        [t.model_dump() for t in req.gateway_txns], SourceType.GATEWAY
    )
    batch = _build_settlement_batch(req.settlement_batch)
    report = reconcile_settlement(batch, candidates, settlement_window_days=req.settlement_window_days)
    return _format_report(report, batch=batch, candidates=candidates)


@app.post("/reconcile/multi", summary="Reconcile across gateway, bank, and ERP sources")
def reconcile_multi(req: MultiSourceReconcileRequest):
    """
    Multi-source reconciliation: merges gateway, bank, and ERP transaction
    feeds into a unified candidate pool before matching. Each source type
    is normalized independently (timezone, currency, ref_id canonicalization)
    per the Agent 1 schema before the matching pipeline sees them.
    """
    candidates: list[NormalizedTxn] = []

    if req.gateway_txns:
        candidates += normalize_batch(
            [t.model_dump() for t in req.gateway_txns], SourceType.GATEWAY
        )
    if req.bank_txns:
        candidates += normalize_batch(
            [t.model_dump() for t in req.bank_txns], SourceType.BANK
        )
    if req.erp_txns:
        candidates += normalize_batch(
            [t.model_dump() for t in req.erp_txns], SourceType.ERP
        )

    if not candidates:
        raise HTTPException(status_code=400, detail="No valid transactions provided across any source.")

    batch = _build_settlement_batch(req.settlement_batch)
    report = reconcile_settlement(batch, candidates, settlement_window_days=req.settlement_window_days)
    return _format_report(report, batch=batch, candidates=candidates)


def _check_then_record(batch_id: str, summary: dict, matched_ids: list[str]) -> dict:
    """
    Order matters: check BEFORE recording, or a batch reports itself.

    The queue reconciles many settlements in one pass, so a payment cleared by
    the first can legitimately be seen by the second — and the second needs to
    be told. Recording happens only on a clear, for the same reason as the
    single path: a withheld batch has consumed nothing.
    """
    prior = settled_ledger.check_claims(batch_id, matched_ids)
    if summary.get("cleared") and matched_ids:
        settled_ledger.record_settled(batch_id, matched_ids)
    return prior


def contested_payments(results: list[dict]) -> dict:
    """
    Payments claimed by more than one settlement in the same run.

    This is the only check that REQUIRES the queue. A single settlement can
    never see it: the question is not "are these the right payments for this
    deposit" but "has this payment already been spent on a different one",
    and only a run that holds every settlement at once can answer it.

    It matters most exactly where the engine is otherwise least useful. When
    payments are fungible — a merchant selling one item at one price, 2,000
    apples at Rs 5 — WHICH payments compose a settlement is arbitrary and the
    engine says so. What is not arbitrary is whether the same payment has
    been counted twice, and that is the real exposure for that business.

    Measured before this existed: two weekly settlements over one pool of
    identical payments, 1,200 and 700, and 700 payments appeared in both
    matched sets with nothing in the response mentioning it. The plain-English
    text was already telling reviewers to "check these are not also being
    claimed by another settlement" while the only system able to check it
    stayed silent.

    Reported, never auto-resolved. Deciding which settlement legitimately owns
    a contested payment needs facts this engine does not have.
    """
    from collections import defaultdict
    owners: dict[str, list[str]] = defaultdict(list)
    cleared_owners: dict[str, list[str]] = defaultdict(list)
    for r in results:
        bid = r.get("batch_id") or "?"
        is_cleared = r.get("status") == "cleared"
        for tid in (r.get("matched_txn_ids") or []):
            owners[tid].append(bid)
            if is_cleared:
                cleared_owners[tid].append(bid)

    # Two very different situations, and collapsing them makes the signal
    # useless. A payment in two CLEARED settlements has genuinely been
    # counted twice and the books are wrong. A payment shared with a
    # WITHHELD settlement is not a claim at all — that batch did not clear,
    # its set is a proposal, and overlapping a real settlement is one of the
    # reasons it should not be believed. Reporting the second as though it
    # were the first would put ten alarms on a healthy week.
    contested = {t: b for t, b in cleared_owners.items() if len(b) > 1}
    proposed_overlap = sum(
        1 for t, b in owners.items()
        if len(b) > 1 and len(cleared_owners.get(t, [])) <= 1
    )

    if not contested:
        note = "No payment was claimed by more than one CLEARED settlement."
        if proposed_overlap:
            note += (f" {proposed_overlap} payment(s) appear in an unresolved "
                     f"batch's proposed set as well as elsewhere, which is "
                     f"one of the reasons those batches were not cleared.")
        return {"count": 0, "proposed_overlap": proposed_overlap,
                "batches": [], "sample": [], "summary": note}

    pairs: dict[tuple, int] = defaultdict(int)
    for batches in contested.values():
        for i in range(len(batches)):
            for j in range(i + 1, len(batches)):
                pairs[tuple(sorted((batches[i], batches[j])))] += 1

    return {
        "count": len(contested),
        "proposed_overlap": proposed_overlap,
        "batches": [{"between": list(k), "shared_payments": v}
                    for k, v in sorted(pairs.items(), key=lambda kv: -kv[1])],
        "sample": sorted(contested)[:20],
        "summary": (
            f"{len(contested)} payment(s) were counted in more than one "
            f"CLEARED settlement. A payment can only belong to one, so at "
            f"least one of these settlements is wrong and the books do not "
            f"balance until it is resolved."
        ),
    }


def compliance_review(candidates, batch_id_for_audit: str = "") -> dict:
    """
    What compliance found, for the person who has to review it.

    Findings were being computed and then thrown away. compliance_agent
    attaches a full ComplianceFinding to every transaction a rule hits —
    rule, severity, authority, citation, what was observed, what to do — and
    the API only ever built exceptions from the BLOCKED list. Everything
    FLAGGED, which is most of what a reviewer actually needs to look at,
    reached no screen at all.

    Measured on one realistic merchant day: 41 payments, 6 of them flagged by
    two rules — a customer who paid twice in the same second, and a reseller
    whose four sub-Rs 50,000 orders in a day match the structuring pattern.
    The engine found both. The reviewer was shown neither.

    Grouped by rule rather than by transaction, because a reviewer decides
    per pattern, not per row: "these four payments are one wholesale customer
    restocking" is a single judgement covering four transactions.
    """
    from collections import defaultdict
    by_rule: dict[str, dict] = {}
    members: dict[str, list] = defaultdict(list)

    for t in candidates or []:
        for f in (getattr(t, "compliance_findings", None) or []):
            rid = getattr(f, "rule_id", "") or "UNKNOWN"
            if rid not in by_rule:
                by_rule[rid] = {
                    "rule_id": rid,
                    "title": getattr(f, "title", ""),
                    "severity": getattr(f, "severity", ""),
                    "action": getattr(f, "action", ""),
                    "basis": getattr(getattr(f, "basis", None), "value",
                                     str(getattr(f, "basis", ""))),
                    "authority": getattr(f, "authority", ""),
                    "source_name": getattr(f, "source_name", ""),
                    "citation": getattr(f, "citation", ""),
                    "reference_url": getattr(f, "reference_url", ""),
                    "rule_text": getattr(f, "rule_text", ""),
                    "threshold_applied": getattr(f, "threshold_applied", ""),
                    "why": getattr(f, "why", ""),
                    "observed": getattr(f, "observed", ""),
                    "remediation": getattr(f, "remediation", ""),
                }
            members[rid].append({
                "txn_id": t.source_txn_id,
                "amount_cents": t.amount_cents,
                "currency": t.currency,
                "timestamp_utc": t.timestamp_utc.isoformat(),
                "payer_id": getattr(t, "payer_id", ""),
                "memo": (getattr(t, "memo_raw", "") or "")[:80],
            })

    out = []
    for rid, rule in by_rule.items():
        rows = sorted(members[rid], key=lambda r: r["amount_cents"], reverse=True)
        rule["transactions"] = rows
        rule["transaction_count"] = len(rows)
        rule["total_amount_cents"] = sum(r["amount_cents"] for r in rows)
        out.append(rule)
    # Most serious first; a reviewer works down from the top.
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    out.sort(key=lambda r: (r["action"] != "BLOCKED",
                            order.get(r["severity"], 9),
                            -r["total_amount_cents"]))

    # Triage before returning. A controller with two hundred settlements does
    # not want two hundred decisions, and most findings are the same finding.
    # What the engine can defensibly close, it closes; what it cannot, it
    # hands over with the reason it is a person's call.
    triaged = auto_disposition.triage(out)

    # An auto-closure is recorded under its OWN agent name, never as a human
    # decision. A trail that implied a person reviewed something nobody
    # reviewed would undo the attribution work this sits next to.
    for f in triaged["findings"]:
        d = f.get("auto_disposition") or {}
        if d.get("disposition") != "auto":
            continue
        try:
            audit.log_decision(
                batch_id=batch_id_for_audit,
                agent="auto_disposition",
                detail=(f"Closed compliance finding {f.get('rule_id')} without "
                        f"human review. {d.get('reason', '')} "
                        f"{d.get('residual', '')}".strip()),
            )
        except Exception:
            # Never let a bookkeeping write fail a completed reconciliation.
            pass
    return triaged


def interchangeable_note(match_result, pool) -> dict | None:
    """
    Is the choice the solver made actually a choice?

    A settlement of three Rs 500 payments, drawn from ten Rs 500 payments that
    share a timestamp, a reference and a memo, is reported as ambiguous — and
    it is, arithmetically. But the advice that followed, "someone needs to
    look at the candidates and choose", is wrong here in a way worth catching:
    there is nothing to choose between. The ten are indistinguishable in every
    field a reviewer can see, so any three of them are the same answer, and
    asking a person to pick produces an arbitrary result wearing the costume
    of a decision.

    That is a different situation from two genuinely different sets of
    payments that happen to sum alike, where the choice is real and matters.
    Collapsing the two into one message tells the reviewer to do busywork in
    the first case and under-warns them in the second.

    What actually carries risk when payments are fungible is not WHICH three
    were picked — the settlement total is right either way — but whether the
    same payment is claimed again by another settlement. That is the thing to
    say, and it is what this reports.

    Returns None when the matched set is not interchangeable with anything.
    """
    wanted = set(match_result.matched_txn_ids or [])
    if not wanted or not pool:
        return None

    def signature(t):
        """
        What makes two payments substitutable FOR THIS SETTLEMENT.

        The timestamp used to be part of this, which made the check almost
        useless: real payments never share a timestamp to the second, so it
        only ever fired on genuinely duplicated rows. A merchant selling one
        item at one price — 2,000 apples at Rs 5 — got told "more than one
        set adds up, someone needs to choose" when every payment in the pool
        was interchangeable with every other and there was nothing to choose
        between. That is the exact advice this check exists to prevent.

        Dropping the timestamp is safe because the pool reaching here has
        already been filtered to the settlement window. Everything in it is
        in-window by construction, so the timestamp was not distinguishing
        candidates, only preventing them from being recognised as alike.
        """
        return (t.amount_cents, t.currency,
                t.ref_id_canonical or "", (t.memo_normalized or ""))

    from collections import Counter
    pool_sigs = Counter(signature(t) for t in pool)
    picked = [t for t in pool if t.source_txn_id in wanted]
    picked_sigs = Counter(signature(t) for t in picked)

    groups = []
    for sig, n_picked in picked_sigs.items():
        available = pool_sigs.get(sig, 0)
        if available > n_picked:
            groups.append({
                "amount_cents": sig[0],
                "currency": sig[1],
                "picked": n_picked,
                "identical_available": available,
                "txn_ids": sorted(
                    t.source_txn_id for t in pool if signature(t) == sig
                )[:20],
            })
    if not groups:
        return None

    wholly = sum(g["picked"] for g in groups) == len(picked)

    # A REPRODUCIBLE proposal for the case where the choice is arbitrary.
    #
    # When every picked payment is interchangeable, the solver returned one
    # subset out of an enormous number of equally correct ones, and which one
    # depends on solver internals. Run the same file twice and you can get a
    # different set of ids for the same settlement — which is indefensible in
    # an audit even though every set is arithmetically right.
    #
    # So propose the OLDEST N instead. FIFO is the convention accounting has
    # used for fungible units for a century: you do not identify which unit
    # left, you consume in a stated order and track the balance. It makes the
    # answer stable, explains itself, and is honest that it is a convention
    # rather than an identification.
    #
    # Note this proposes only. Nothing here clears a batch — a person accepts
    # the convention through /settlement/{id}/accept-fifo, and that acceptance
    # is what records consumption.
    fifo = None
    if wholly:
        sigs = {(g["amount_cents"], g["currency"]) for g in groups}
        eligible = [
            t for t in pool
            if (t.amount_cents, t.currency) in sigs
            and signature(t) in picked_sigs
        ]
        eligible.sort(key=lambda t: (t.timestamp_utc, t.source_txn_id))
        chosen = eligible[: len(picked)]
        if len(chosen) == len(picked):
            fifo = {
                "basis": "oldest_first",
                "count": len(chosen),
                "txn_ids": [t.source_txn_id for t in chosen],
                "earliest": str(chosen[0].timestamp_utc),
                "latest": str(chosen[-1].timestamp_utc),
                "pool_size": len(eligible),
            }

    return {
        "groups": groups,
        # True when EVERY picked payment came from an interchangeable group —
        # the whole selection is arbitrary, not just part of it.
        "wholly_interchangeable": wholly,
        "fifo_proposal": fifo,
    }


def matched_rows(match_result, pool) -> list[dict]:
    """
    The payments the engine picked, with enough detail to judge them.

    matched_txn_ids was already returned on both paths and no screen rendered
    it, so a reviewer was told "confirm these are the right payments" and
    shown nothing — sent back to the source file to find them by hand. That
    inverts the point of the tool. Summing a column is the easy half; naming
    WHICH payments sum, so a person can agree or disagree in seconds, is the
    half worth building.

    Ids alone are not enough to decide on. A reviewer needs the amount, the
    date and the reference to recognise a payment, so those travel with it,
    ordered by amount because that is how a person scans a list like this.
    """
    wanted = set(match_result.matched_txn_ids or [])
    # `candidates` is optional on _format_report's signature even though every
    # call site passes it. The reconciliation is already done by the time this
    # runs, and the caller is entitled to the result — a presentation helper
    # must not be able to turn a finished answer into a 500.
    if not wanted or not pool:
        return []
    rows = [
        {
            "txn_id": t.source_txn_id,
            "source": t.source.value,
            "amount_cents": t.amount_cents,
            "currency": t.currency,
            "timestamp_utc": t.timestamp_utc.isoformat(),
            "reference": t.ref_id_canonical or "",
            "memo": (t.memo_raw or "")[:80],
        }
        for t in pool
        if t.source_txn_id in wanted
    ]
    rows.sort(key=lambda r: r["amount_cents"], reverse=True)
    return rows


def _settlement_instant(value) -> datetime:
    """
    Read a settlement timestamp without letting the SERVER's timezone decide.

    This was `dp.parse(x).astimezone(timezone.utc)`. When the string carries an
    offset that is correct, but a settlements file routinely holds a bare date
    — "2026-03-03" — and dp.parse returns a NAIVE datetime. astimezone() then
    interprets naive as the machine's local zone, so the anchor moved by the
    server's offset: on an IST host "2026-03-03" became 2026-03-02T18:30Z.

    Two things follow, and both are worse than the shift itself. The same file
    reconciles differently in Mumbai and in Virginia, which makes a result
    unreproducible. And the anchor is compared against transaction timestamps
    that ingestion normalises by a different rule — it never uses server-local,
    falling back to UTC with TzConfidence.LOW — so the settlement and its own
    members were being placed on two different clocks.

    Measured on a queue of 12 real settlements: the window kept 20 of 126
    candidates and excluded true members dated after 18:30 on the anchor day,
    so nothing could sum and every batch came back unmatched.
    """
    parsed = dp.parse(str(value))
    if parsed.tzinfo is None:
        # Same convention ingestion falls back to, so both sides of the
        # comparison are on one clock.
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _rate_card(gateway_bps, tax_bps, flat) -> FeeRateCard:
    """
    The processor's terms for THIS run.

    DEFAULT_RATE_CARD is 2% + 1%, which is a plausible guess and nobody's
    actual contract. Where deductions are not declared outright, the engine
    reconstructs the gross target from this card — and subset-sum is exact, so
    a card that is wrong by a fraction of a percent does not degrade the match,
    it eliminates it. Measured: a 1.5 basis point error takes auto-clear to 0%.

    Versioning by merchant and effective date is the real answer and is not
    built. Accepting the numbers per run is the difference between "this tool
    works for the one merchant we hardcoded" and "tell it your terms".
    """
    if gateway_bps is None and tax_bps is None and flat is None:
        return DEFAULT_RATE_CARD
    return FeeRateCard(
        gateway_fee_bps=gateway_bps if gateway_bps is not None
        else DEFAULT_RATE_CARD.gateway_fee_bps,
        tax_withholding_bps=tax_bps if tax_bps is not None
        else DEFAULT_RATE_CARD.tax_withholding_bps,
        flat_fee_cents=flat if flat is not None
        else DEFAULT_RATE_CARD.flat_fee_cents,
    )


def _rate_card_is_assumed(gateway_bps, tax_bps, flat) -> bool:
    """
    True when no term was supplied and the guessed card is in use.

    A wrong card cannot cause a FALSE clear — subset-sum is exact, so it
    eliminates the match rather than faking one — but that is precisely why
    it needs saying. The operator sees "withheld", concludes the engine could
    not find their settlement, and never learns the cause was a fee guess
    they were never told about.
    """
    return gateway_bps is None and tax_bps is None and flat is None


@app.post("/reconcile/upload", summary="Upload raw CSV/JSON files for reconciliation")
async def reconcile_upload(
    batch_id: str = Form(...),
    net_amount: float = Form(...),
    currency: str = Form("INR"),
    settled_at: str = Form(...),
    settlement_window_days: int = Form(5),
    member_source: Optional[str] = Form(None),
    declared_deductions: Optional[float] = Form(None),
    gateway_fee_bps: Optional[int] = Form(None),
    tax_withholding_bps: Optional[int] = Form(None),
    flat_fee_cents: Optional[int] = Form(None),
    # Who asked for this run. An audit trail that records what happened and
    # why, but not who set it going, is a log rather than a control — and the
    # sign-in screen already tells the user decisions are attributed to them.
    # Sent by the UI from the signed-in session; optional, because the engine
    # is usable without the UI and must not require it.
    reviewer: Optional[str] = Form(None),
    gateway_file: Optional[UploadFile] = File(None),
    bank_file: Optional[UploadFile] = File(None),
    erp_file: Optional[UploadFile] = File(None),
):
    """
    File Upload endpoint. Accepts raw data files (CSV or JSON) and uses
    Agent 0 (File Understanding Agent) to auto-map the headers and scrub the data.
    Then feeds the cleaned data into the reconciliation engine.
    """
    candidates: list[NormalizedTxn] = []
    ingestion_notes: list[str] = []

    async def process_upload(upload_file: Optional[UploadFile], source_type: SourceType):
        if not upload_file:
            return
        
        content = await read_upload_capped(upload_file)
        if not content:
            return
            
        # Agent 0: parse, map headers, and REFUSE the file if the fields
        # reconciliation depends on are not there.
        try:
            header_warnings: list[str] = []
            parsed_rows = file_agent.parse_file_content(
                content, upload_file.filename or "", header_warnings
            )
            # Agent 0's header decisions were logged and never shown. The one
            # that matters most is a column it had to choose between, or a
            # duplicate name where the rightmost silently won.
            for w in header_warnings:
                ingestion_notes.append(f"{upload_file.filename or 'file'}: {w}")
            audit.log_decision(
                batch_id=batch_id, agent="file_agent",
                detail=(
                    f"{upload_file.filename} ({source_type.value}) · "
                    f"{len(parsed_rows)} row(s) parsed and headers mapped to "
                    f"the schema. No required field absent — file accepted."
                ),
            )
            if parsed_rows:
                # Agent 1: ingest and normalise, keeping the structured drop
                # report rather than inferring a count from list lengths — the
                # report names WHICH records failed and why, which is what an
                # operator needs to fix the feed.
                nreport = normalize_batch_with_report(parsed_rows, source_type)
                candidates.extend(nreport.normalized)
                audit.log_decision(
                    batch_id=batch_id, agent="ingestion",
                    detail=(
                        f"{source_type.value}: {len(nreport.normalized)} of "
                        f"{nreport.total_input} row(s) normalized — amounts to "
                        f"integer paise, timestamps to UTC, references "
                        f"canonicalized. {nreport.drop_count} dropped."
                    ),
                )

                if nreport.drop_count:
                    sample = ", ".join(
                        f"{d.txn_id or f'row {d.record_index}'} ({d.reason})"
                        for d in nreport.dropped[:3]
                    )
                    ingestion_notes.append(
                        f"{source_type.value}: {nreport.drop_count} of "
                        f"{nreport.total_input} rows from '{upload_file.filename}' "
                        f"could not be normalised ({nreport.drop_rate:.1%}). "
                        f"First failures: {sample}"
                    )

                # A LOW-confidence timestamp means the source's timezone is
                # unknown, so the engine defaulted to UTC. On an Indian bank
                # feed that is a 5.5-hour error, enough to move a transaction
                # out of its settlement window and make a correct match look
                # like a missing one. ingestion.py has always documented that
                # these "MUST route to the exception queue, not be silently
                # matched" — this is where that stops being a comment.
                # Same reasoning as the timezone note below: an assumption
                # the operator cannot see is an assumption they cannot check.
                unstated = [t for t in nreport.normalized if not t.currency_stated]
                if unstated:
                    ingestion_notes.append(
                        f"{source_type.value}: {len(unstated)} row(s) carry no "
                        f"currency column and were read as INR. If this feed is "
                        f"not INR, its amounts are being compared against a "
                        f"settlement in a different currency and the currency "
                        f"guard cannot see it — add a currency column to be sure."
                    )

                low = [
                    t for t in nreport.normalized
                    if t.tz_confidence is TzConfidence.LOW
                ]
                if low:
                    ingestion_notes.append(
                        f"{source_type.value}: {len(low)} row(s) have an "
                        f"UNKNOWN source timezone and were assumed UTC. If the "
                        f"feed is not UTC these are off by the zone offset and "
                        f"may fall outside the settlement window — verify "
                        f"before relying on any match involving them."
                    )
                    logging.getLogger(__name__).warning(
                        "Ingestion: %d %s row(s) have LOW timezone confidence; "
                        "matches involving them are not trustworthy without "
                        "confirming the source zone.",
                        len(low), source_type.value,
                    )
        except file_agent.FileRejected as e:
            audit.log_decision(
                batch_id=batch_id, agent="file_agent",
                detail=(
                    f"REJECTED {upload_file.filename} ({source_type.value}): "
                    f"{e}"
                ),
            )
            # A rejection is a diagnosis, not a stack trace. Return the
            # structured detail so the UI can tell the user which column is
            # missing and what to rename — 422 rather than 400 because the
            # request was well-formed and the CONTENT is what cannot be
            # processed.
            raise HTTPException(
                status_code=422,
                detail={
                    "message": str(e),
                    "source": source_type.value,
                    **e.to_dict(),
                },
            )
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to parse {source_type.value} file '{upload_file.filename}': {str(e)}"
            )

    await process_upload(gateway_file, SourceType.GATEWAY)
    await process_upload(bank_file, SourceType.BANK)
    await process_upload(erp_file, SourceType.ERP)

    if not candidates:
        raise HTTPException(status_code=400, detail="No valid transactions extracted from the uploaded files.")

    # member_source and declared_deductions are the two facts a production
    # reconciliation knows and the engine cannot infer. Leaving them off the
    # upload form meant the UI ran systematically weaker than the CLI: on the
    # benchmark, declaring the member feed is the difference between 62% and
    # 75.33% auto-clear, because without it a gateway payment and its ERP
    # mirror carry the same amount and neither can be ruled out.
    parsed_member_source = None
    if member_source:
        try:
            parsed_member_source = SourceType(member_source.strip().lower())
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"member_source must be one of "
                    f"{[s.value for s in SourceType]}, got {member_source!r}."
                ),
            )

    batch = SettlementBatch(
        batch_id=batch_id,
        net_amount_cents=normalize_amount_to_cents(net_amount),
        currency=currency,
        settled_at_utc=_settlement_instant(settled_at),
        member_source=parsed_member_source,
        # Declared deductions make the gross target a fact rather than a
        # rate-card estimate, so the tolerance band is not being spent
        # absorbing fee drift.
        declared_deductions_cents=(
            normalize_amount_to_cents(declared_deductions)
            if declared_deductions is not None else None
        ),
    )
    
    report = reconcile_settlement(
        batch, candidates, settlement_window_days=settlement_window_days,
        rate_card=_rate_card(gateway_fee_bps, tax_withholding_bps, flat_fee_cents),
        # (see _rate_card_is_assumed — the note is added below)
    )
    formatted = _format_report(report, batch=batch, candidates=candidates)
    
    inputs = {
        "batch_id": batch_id,
        "net_amount": net_amount,
        "currency": currency,
        "settled_at": settled_at,
        "settlement_window_days": settlement_window_days,
        "gateway_file": gateway_file.filename if gateway_file else None,
        "bank_file": bank_file.filename if bank_file else None,
        "erp_file": erp_file.filename if erp_file else None,
        "reviewer": (reviewer or "").strip() or None,
        "rate_card_assumed": _rate_card_is_assumed(
            gateway_fee_bps, tax_withholding_bps, flat_fee_cents),
    }
    # Recording history must never fail the reconciliation. The work is
    # already done and the caller is entitled to the result; a full disk or a
    # permissions problem on an audit convenience is not a reason to throw it
    # away.
    if _rate_card_is_assumed(gateway_fee_bps, tax_withholding_bps, flat_fee_cents)             and not declared_deductions:
        ingestion_notes.append(
            "No deductions were declared and no rate card was supplied, so "
            f"the gross target was reconstructed from the DEFAULT card "
            f"({DEFAULT_RATE_CARD.gateway_fee_bps}bps + "
            f"{DEFAULT_RATE_CARD.tax_withholding_bps}bps). That is a plausible "
            "guess, not your contract. Subset-sum is exact, so if these terms "
            "are wrong the batch will be withheld rather than mismatched — "
            "supply your real terms if this batch does not clear."
        )

    # Has a previous run already paid these out? The queue catches a payment
    # counted twice within one run; this catches the case that actually
    # happens — last week's cleared payments still sitting in this week's
    # pool. Checked before recording, so a batch never trips over itself.
    matched_ids = formatted.get("matched_txn_ids") or []
    prior = settled_ledger.check_claims(report.batch_id, matched_ids)
    if prior.get("count"):
        formatted["already_settled_elsewhere"] = prior
        audit.log_decision(
            batch_id=report.batch_id, agent="settled_ledger",
            detail=(f"{prior['count']} matched payment(s) were already cleared "
                    f"into an earlier settlement. {prior['summary']}"),
        )

        # A batch built mostly from payments a previous settlement already
        # paid out must not carry the word "cleared". The first version left
        # the clear standing and printed a warning underneath it, which is
        # the arrangement every alert-fatigue story starts with: the headline
        # says safe, the detail says otherwise, and the headline wins.
        #
        # This withholds rather than excludes. The payments stay in the pool
        # and the arithmetic is untouched — silently dropping them would
        # change an answer on the strength of a record that could itself be
        # wrong, and a batch cleared in error last week would then corrupt
        # this week's invisibly. Refusing to clear is visible; rewriting the
        # sum is not.
        summ = formatted.get("summary") or {}
        share = prior["count"] / max(1, len(matched_ids))
        if summ.get("cleared") and share >= 0.5:
            summ["cleared"] = False
            summ["ambiguous"] = True
            summ["withheld_reason"] = "already_settled_elsewhere"
            formatted["cleared"] = False
            audit.log_decision(
                batch_id=report.batch_id, agent="settled_ledger",
                detail=(f"Withheld from auto-clear: {prior['count']} of "
                        f"{len(matched_ids)} matched payments were already "
                        f"paid out by an earlier settlement. The arithmetic is "
                        f"unchanged; the clear is withheld for a person."),
            )

    # Record only on a CLEAR. A withheld batch has consumed nothing, and
    # writing proposals here would make this a record of guesses.
    if (formatted.get("summary") or {}).get("cleared") and matched_ids:
        settled_ledger.record_settled(
            report.batch_id, matched_ids,
            when=(formatted.get("summary") or {}).get("as_of_utc") or "")

    formatted["plain_summary"] = plain_summary(
        formatted.get("summary") or {}, formatted.get("reasoning") or "",
        formatted.get("interchangeable"))

    if (reviewer or "").strip():
        audit.log_decision(
            batch_id=batch_id,
            agent="attribution",
            detail=f"Run requested by {reviewer.strip()}.",
        )
        formatted["reviewer"] = reviewer.strip()

    # A batch reconciled twice is a question an auditor asks, so say it rather
    # than letting two different verdicts for one batch sit in the trail with
    # nothing connecting them.
    try:
        prior = [r for r in history.list_runs(limit=50, batch_id=batch_id)]
        if prior:
            last = prior[0]
            ingestion_notes.append(
                f"This batch has been reconciled {len(prior)} time"
                f"{'' if len(prior) == 1 else 's'} before — most recently "
                f"{last.get('timestamp_utc')} with the verdict "
                f"'{last.get('status')}'. This run does not replace that "
                f"record; both are in the history."
            )
    except Exception:
        # A history read must never fail a completed reconciliation.
        pass

    if ingestion_notes:
        formatted["ingestion_notes"] = ingestion_notes

    try:
        history.record_run(batch_id, inputs, formatted)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Could not record run history for %s (%s: %s). The reconciliation "
            "result is unaffected.", batch_id, type(exc).__name__, exc,
        )
    
    return formatted


# ── Regulator / auditor endpoints ──────────────────────────────────────────────
#
# Read-only and open to everyone. Intended for a bank's compliance function, an
# internal auditor, or a supervisor who wants to inspect what this engine
# enforces and why a given batch was decided the way it was — but there is no
# gate on them, because the whole value of a published rulebook is that anyone
# relying on the engine's output can check it.


@app.post("/settlements/detect", summary="Read settlements out of a statement")
async def detect_settlements(file: UploadFile = File(...)):
    """
    Pull the settlement rows out of an uploaded file.

    WHY
    ---
    The batch id, credited amount and settlement date were fields a user typed
    — and they were retyping what a bank statement already states. That is the
    manual work this engine exists to remove, reintroduced at the front door.

    A bank statement's credit lines ARE the settlements list. This reads them
    with the same parser /reconcile/queue uses, so a statement can populate
    the form instead of being transcribed into it.

    Returns candidates, not decisions. The user confirms which row is the
    settlement they mean; nothing is reconciled here.
    """
    raw = await read_upload_capped(file)
    report: dict = {}
    try:
        rows = file_agent.parse_settlements(
            raw, file.filename or "settlements", report=report)
    except file_agent.FileRejected as e:
        raise HTTPException(status_code=422, detail={
            "message": str(e), "filename": file.filename,
            "rejected": True, **e.to_dict(),
        })

    # The cap exists so a year-long statement cannot flood the response, but
    # it is REPORTED rather than applied quietly. A count that silently means
    # "the first 200" on a reconciliation tool is worse than no count.
    LIMIT = 200
    out = []
    for r in rows[:LIMIT]:
        try:
            amount = float(str(r["net_amount"]).replace(",", "").strip())
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            # A statement holds debits as well as credits. A settlement is
            # money arriving, so a negative or zero line is not one.
            continue
        out.append({
            "batch_id": str(r["batch_id"]).strip(),
            "net_amount": amount,
            "settled_at": str(r.get("settled_at") or "").strip(),
            "currency": str(r.get("currency") or "INR").strip() or "INR",
            "declared_deductions": r.get("declared_deductions"),
        })
    unreadable = report.get("unreadable_amounts", 0)
    return {
        # What the file actually contains, not what survived this response.
        "detected": len(rows),
        "returned": len(out),
        "truncated": len(rows) > LIMIT,
        # Credit rows whose amount could not be parsed. Never silently zero:
        # a settlement dropped without a word is the one outcome this engine
        # is built to refuse.
        "unreadable_amounts": unreadable,
        # settled_at is ISO-8601. It used to be the file's raw string, which
        # the browser read with Date() — month-first — so a day-first
        # statement reported a month it did not contain.
        "date_order": report.get("date_order", "day"),
        "date_order_proven": report.get("date_order_proven", False),
        "settlements": out,
        "filename": file.filename,
    }


@app.post("/reconcile/queue", summary="Reconcile many settlements in one run")
async def reconcile_queue(
    settlements_file: UploadFile = File(...),
    settlement_window_days: int = Form(5),
    member_source: Optional[str] = Form(None),
    gateway_fee_bps: Optional[int] = Form(None),
    tax_withholding_bps: Optional[int] = Form(None),
    flat_fee_cents: Optional[int] = Form(None),
    gateway_file: Optional[UploadFile] = File(None),
    bank_file: Optional[UploadFile] = File(None),
    erp_file: Optional[UploadFile] = File(None),
):
    """
    A day's settlements against one candidate pool.

    WHY THIS EXISTS
    ---------------
    /reconcile/upload handles ONE settlement, and a controller does not have
    one settlement. They have a morning's worth — tens or hundreds — and the
    question they actually need answered is not "did this batch clear" but
    "which of today's batches need me". A tool that answers the first question
    one batch at a time is a demo; answering the second is the job.

    The candidate feeds are parsed and normalised ONCE and reused across every
    settlement in the queue. That is not just a speed trick: parsing a 50,000
    row export per settlement would make a queue of fifty batches quadratic in
    the thing that is already the most expensive stage.

    Each settlement is reconciled independently and its failure is contained.
    One malformed row in one batch must not cost a controller the other
    forty-nine results, so a batch that raises is reported as an error entry
    beside the batches that succeeded rather than taking down the request.
    """
    settlements_raw = await read_upload_capped(settlements_file)
    try:
        settlement_rows = file_agent.parse_settlements(
            settlements_raw, settlements_file.filename or "settlements"
        )
    except file_agent.FileRejected as e:
        raise HTTPException(status_code=422, detail={
            "message": str(e), "source": "settlements",
            "filename": settlements_file.filename, "rejected": True, **e.to_dict(),
        })

    candidates: list[NormalizedTxn] = []
    ingestion_notes: list[str] = []

    async def load(upload: Optional[UploadFile], source_type: SourceType):
        if not upload:
            return
        content = await read_upload_capped(upload)
        if not content:
            return
        try:
            parsed = file_agent.parse_file_content(content, upload.filename or "")
            if not parsed:
                return
            nreport = normalize_batch_with_report(parsed, source_type)
            candidates.extend(nreport.normalized)
            audit.log_decision(
                batch_id="QUEUE", agent="ingestion",
                detail=(f"{source_type.value}: {len(nreport.normalized)} of "
                        f"{nreport.total_input} row(s) normalized for the queue."),
            )
            if nreport.drop_count:
                ingestion_notes.append(
                    f"{source_type.value}: {nreport.drop_count} of "
                    f"{nreport.total_input} rows could not be normalised."
                )
        except file_agent.FileRejected as e:
            raise HTTPException(status_code=422, detail={
                "message": str(e), "source": source_type.value,
                "filename": upload.filename, "rejected": True, **e.to_dict(),
            })

    await load(gateway_file, SourceType.GATEWAY)
    await load(bank_file, SourceType.BANK)
    await load(erp_file, SourceType.ERP)

    parsed_member_source = None
    if member_source:
        try:
            parsed_member_source = SourceType(member_source.strip().lower())
        except ValueError:
            raise HTTPException(status_code=422, detail={
                "message": f"member_source must be one of "
                           f"{[s.value for s in SourceType]}, got {member_source!r}.",
            })

    results = []
    for row in settlement_rows:
        bid = str(row.get("batch_id") or "").strip()
        try:
            batch = SettlementBatch(
                batch_id=bid,
                net_amount_cents=normalize_amount_to_cents(row["net_amount"]),
                currency=str(row.get("currency") or "INR"),
                settled_at_utc=_settlement_instant(str(row["settled_at"])),
                member_source=parsed_member_source,
                declared_deductions_cents=(
                    normalize_amount_to_cents(row["declared_deductions"])
                    if row.get("declared_deductions") not in (None, "") else None
                ),
            )
            report = reconcile_settlement(
                batch, candidates, settlement_window_days=settlement_window_days,
                rate_card=_rate_card(gateway_fee_bps, tax_withholding_bps, flat_fee_cents),
            )
            summary = report.summary()
            results.append({
                "batch_id": bid,
                "status": ("cleared" if summary["cleared"]
                           else "withheld" if summary.get("ambiguous")
                           else "unmatched"),
                "summary": summary,
                "matched_txn_ids": report.match_result.matched_txn_ids,
                "matched_transactions": matched_rows(report.match_result, candidates),
                "interchangeable": interchangeable_note(report.match_result, candidates),
                "compliance_review": compliance_review(candidates, bid),
                "already_settled_elsewhere": _check_then_record(
                    bid, summary, report.match_result.matched_txn_ids),
                "exception_count": len(report.exceptions),
                # Plain first, technical second. A reviewer meets the
                # statement they can act on; the engine's own wording stays
                # for whoever is reading the audit trail.
                "plain": plain_summary(summary, report.match_result.reasoning,
                                       interchangeable_note(report.match_result, candidates)),
                "reasoning": report.match_result.reasoning,
            })
        except Exception as exc:
            # Contained per batch. Forty-nine good results are worth more than
            # a clean stack trace.
            logging.getLogger(__name__).exception("Queue: %s failed", bid)
            results.append({
                "batch_id": bid or "(unnamed)",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            })

    tally = {k: sum(1 for r in results if r["status"] == k)
             for k in ("cleared", "withheld", "unmatched", "error")}
    return {
        "queued": len(results),
        "candidates_pooled": len(candidates),
        "tally": tally,
        "needs_review": tally["withheld"] + tally["unmatched"] + tally["error"],
        # Only answerable across a whole run: has one payment been spent twice?
        "contested_payments": contested_payments(results),
        "results": results,
        "ingestion_notes": ingestion_notes,
    }


class AcceptFifo(BaseModel):
    """A reviewer accepting the FIFO convention on a fungible settlement."""
    reviewer: str                      # who is accountable for it
    note: str = ""                     # why, in their own words


@app.post("/settlement/{batch_id}/accept-fifo",
          summary="Accept the oldest-first convention on a fungible settlement")
def accept_fifo(batch_id: str, body: AcceptFifo):
    """
    The way out of a settlement nobody can identify.

    A merchant selling one item at one price produces payments that are
    identical in every recorded field. Any N of them make the settlement's
    total, so naming N specific ids is not an identification — it is an
    arbitrary pick wearing the costume of one. The engine correctly refuses to
    make that pick and call it a match, which leaves that whole class of
    merchant unable to close their books.

    Accounting solved this long before software: for fungible units you do not
    say WHICH unit left, you consume in a stated order and track the balance.
    That is what this records. The reviewer accepts the oldest-first
    convention; the payments are marked consumed so no other settlement can
    claim them; and the trail says plainly that this rests on a convention and
    not on evidence.

    Deliberately NOT an automatic clear. The engine's verdict is untouched and
    the batch stays withheld, exactly as with every other decision endpoint —
    a person accepted a convention, which is a different fact from the engine
    having identified something, and the record has to keep them apart.

    Refused unless the settlement is WHOLLY interchangeable. FIFO is only
    defensible when the choice is genuinely arbitrary; using it on a batch
    withheld for any other reason would launder a real problem into a
    convention.
    """
    reviewer = (body.reviewer or "").strip()
    if not reviewer:
        raise HTTPException(status_code=422, detail={
            "message": "reviewer is required",
            "plain": ("Accepting a convention has to be attributed to someone. "
                      "Sign in first."),
        })

    report = settlement_qa.get_result(batch_id)
    if not report:
        raise HTTPException(status_code=404, detail={
            "message": "no recorded run for this batch",
            "plain": ("There is no recorded run for this settlement in this "
                      "session, so there is nothing to accept. Reconcile it "
                      "again and accept from the result."),
        })

    inter = report.get("interchangeable") or {}
    proposal = inter.get("fifo_proposal")
    if not inter.get("wholly_interchangeable") or not proposal:
        raise HTTPException(status_code=422, detail={
            "message": "settlement is not wholly interchangeable",
            "plain": ("This settlement was not withheld because its payments "
                      "are indistinguishable, so the oldest-first convention "
                      "does not apply. Whatever is unresolved here is a real "
                      "question about which payments belong, and needs an "
                      "answer rather than a convention."),
        })

    txn_ids = list(proposal.get("txn_ids") or [])
    claims = settled_ledger.check_claims(batch_id, txn_ids)
    if claims.get("count"):
        raise HTTPException(status_code=409, detail={
            "message": "payments already claimed elsewhere",
            "plain": ("Some of these payments are already recorded against "
                      f"another settlement. {claims.get('summary', '')}"),
        })

    written = settled_ledger.record_settled(batch_id, txn_ids)
    note = f" Note: {body.note.strip()}" if (body.note or "").strip() else ""
    entry = audit.log_decision(
        batch_id=batch_id,
        agent="human_reviewer",
        detail=(
            f"{reviewer} ACCEPTED the oldest-first convention covering "
            f"{len(txn_ids)} payment(s), drawn from {proposal.get('pool_size')} "
            f"indistinguishable ones between {proposal.get('earliest')} and "
            f"{proposal.get('latest')}.{note} These payments are now recorded "
            f"as consumed so no other settlement can claim them. This rests on "
            f"a stated convention, NOT on evidence that these specific payments "
            f"are the members — any set of the same size would have been "
            f"equally correct. The engine's own verdict is unchanged."
        ),
    )
    return {
        "batch_id": batch_id,
        "accepted": True,
        "basis": proposal.get("basis"),
        "reviewer": reviewer,
        "consumed_count": written,
        "txn_ids": txn_ids,
        "clears_the_batch": False,
        "audit_entry": entry,
    }


class ReviewDecision(BaseModel):
    """A person's verdict on a batch the engine would not decide alone."""
    decision: str                      # confirmed | rejected
    reviewer: str                      # who is accountable for it
    note: str = ""                     # why, in their own words
    txn_ids: list[str] = []            # the payments the decision covers


@app.post("/settlement/{batch_id}/decision",
          summary="Record a reviewer's decision on a batch")
def record_decision(batch_id: str, body: ReviewDecision):
    """
    Close the loop the rest of this engine is built around.

    Every other endpoint here reads or computes. The product's whole claim is
    that the engine declines when it cannot be certain and a person decides —
    and until now there was nowhere for that decision to go. It happened in
    someone's head and evaporated. The audit trail recorded what the engine
    did and never what the human did, which makes it a log of half the
    process.

    It also left the sign-in screen making a promise the system did not keep:
    "reconciliation decisions are attributed to whoever is signed in". They
    were not attributed to anyone, because they were not recorded.

    This does NOT change the engine's verdict, and deliberately so. A cleared
    batch stays cleared and a withheld batch stays withheld; the human
    decision is a separate fact recorded ALONGSIDE the machine's, not an
    overwrite of it. An audit trail where a person can retroactively make the
    engine look right is worth nothing.

    Re-deciding is allowed and appends rather than replaces. People change
    their minds with new information, and the sequence of decisions is itself
    part of the record.
    """
    decision = (body.decision or "").strip().lower()
    if decision not in ("confirmed", "rejected"):
        raise HTTPException(status_code=422, detail={
            "message": "decision must be 'confirmed' or 'rejected'",
            "plain": ("A decision has to be either 'confirmed' or 'rejected'. "
                      f"Received {body.decision!r}."),
        })
    reviewer = (body.reviewer or "").strip()
    if not reviewer:
        # An unattributed decision is the thing this endpoint exists to stop.
        raise HTTPException(status_code=422, detail={
            "message": "reviewer is required",
            "plain": ("A decision has to be attributed to someone. Sign in "
                      "before confirming or rejecting a batch."),
        })

    covered = f" covering {len(body.txn_ids)} payment(s)" if body.txn_ids else ""
    note = f" Note: {body.note.strip()}" if (body.note or "").strip() else ""
    entry = audit.log_decision(
        batch_id=batch_id,
        agent="human_reviewer",
        detail=(f"{reviewer} {decision.upper()} this batch{covered}."
                f"{note} This is the reviewer's decision and does not alter "
                f"the engine's own verdict."),
    )
    return {
        "batch_id": batch_id,
        "decision": decision,
        "reviewer": reviewer,
        "note": body.note.strip(),
        "txn_ids": body.txn_ids,
        "recorded_at": entry.get("timestamp") or entry.get("ts"),
        "audit_entry": entry,
    }


class JournalDecision(BaseModel):
    """A person's verdict on the posting proposal."""
    decision: str                      # approved | rejected
    reviewer: str
    entry_id: str = ""
    note: str = ""


@app.post("/settlement/{batch_id}/journal/decision",
          summary="Approve or reject the posting proposal")
def record_journal_decision(batch_id: str, body: JournalDecision):
    """
    The cash panel said the posting proposal was "ready for human approval",
    and there was nowhere to approve it. Same fault as the batch decision:
    the app asserted a human step and provided no way to take it, so the
    sentence was decoration.

    APPROVING STILL POSTS NOTHING. This engine does not write to a ledger and
    this endpoint does not change that — it records that a named person
    reviewed a balanced proposal and approved it for posting. The posting
    itself happens in the accounting system, by whoever has that authority.
    Recording an approval and calling it a posting would be the same class of
    lie as clearing a settlement nobody checked.
    """
    decision = (body.decision or "").strip().lower()
    if decision not in ("approved", "rejected"):
        raise HTTPException(status_code=422, detail={
            "message": "decision must be 'approved' or 'rejected'",
            "plain": ("A posting proposal can be 'approved' or 'rejected'. "
                      f"Received {body.decision!r}."),
        })
    reviewer = (body.reviewer or "").strip()
    if not reviewer:
        raise HTTPException(status_code=422, detail={
            "message": "reviewer is required",
            "plain": ("An approval has to be attributed to someone. Sign in "
                      "before approving a posting proposal."),
        })

    ref = f" (entry {body.entry_id})" if (body.entry_id or "").strip() else ""
    note = f" Note: {body.note.strip()}" if (body.note or "").strip() else ""
    entry = audit.log_decision(
        batch_id=batch_id,
        agent="human_reviewer",
        detail=(f"{reviewer} {decision.upper()} the posting proposal{ref}."
                f"{note} Nothing has been posted to any ledger — this records "
                f"the approval, not the posting."),
    )
    return {
        "batch_id": batch_id, "decision": decision, "reviewer": reviewer,
        "entry_id": body.entry_id, "note": body.note.strip(),
        "posted": False,
        "audit_entry": entry,
    }


class ComplianceDecision(BaseModel):
    """A reviewer's verdict on one compliance finding."""
    rule_id: str
    decision: str                      # cleared | escalated
    reviewer: str
    basis: str = ""                    # statutory | regulatory_guidance | internal_policy
    note: str = ""
    txn_ids: list[str] = []


@app.post("/settlement/{batch_id}/compliance/decision",
          summary="Record a reviewer's decision on a compliance finding")
def record_compliance_decision(batch_id: str, body: ComplianceDecision):
    """
    Each compliance card told a reviewer what to do next and gave them no way
    to record that they had done it.

    The verbs here are deliberately not approve/reject. A reviewer looking at
    a flagged pattern either decides it is benign — the reseller really is
    restocking — or escalates it to someone who can act. "Approving" a
    suspected structuring pattern is not a thing anyone does.

    THE STATUTORY CARVE-OUT. Clearing a review is not the same as discharging
    a legal duty, and on a statutory finding the two are dangerously easy to
    confuse. A CTR obligation is a duty to REPORT; deciding the transaction is
    legitimate does not remove it. So a cleared statutory finding records, in
    the trail itself, that the reporting obligation is unaffected. A reviewer
    who clicks a button and believes they have filed something is worse off
    than one with no button at all.
    """
    decision = (body.decision or "").strip().lower()
    if decision not in ("cleared", "escalated"):
        raise HTTPException(status_code=422, detail={
            "message": "decision must be 'cleared' or 'escalated'",
            "plain": ("A compliance finding is either 'cleared' (reviewed, no "
                      f"concern) or 'escalated'. Received {body.decision!r}."),
        })
    reviewer = (body.reviewer or "").strip()
    if not reviewer:
        raise HTTPException(status_code=422, detail={
            "message": "reviewer is required",
            "plain": ("A compliance decision has to be attributed to someone. "
                      "Sign in before clearing or escalating a finding."),
        })
    rule_id = (body.rule_id or "").strip() or "UNSPECIFIED_RULE"

    covered = f" covering {len(body.txn_ids)} payment(s)" if body.txn_ids else ""
    note = f" Note: {body.note.strip()}" if (body.note or "").strip() else ""
    caveat = ""
    if decision == "cleared" and body.basis.strip().lower() == "statutory":
        caveat = (" This finding rests on a STATUTORY obligation: clearing the "
                  "review records a judgement about the transaction and does "
                  "NOT discharge any reporting duty.")

    entry = audit.log_decision(
        batch_id=batch_id,
        agent="human_reviewer",
        detail=(f"{reviewer} {decision.upper()} compliance finding "
                f"{rule_id}{covered}.{note}{caveat}"),
    )
    return {
        "batch_id": batch_id, "rule_id": rule_id, "decision": decision,
        "reviewer": reviewer, "note": body.note.strip(),
        "txn_ids": body.txn_ids,
        "discharges_reporting_obligation": False,
        "audit_entry": entry,
    }


@app.get("/settlement/{batch_id}/decisions",
         summary="Reviewer decisions recorded against a batch")
def list_decisions(batch_id: str):
    """Every human decision on this batch, oldest first."""
    trail = audit.get_audit_trail(batch_id) or []
    out = [e for e in trail if e.get("agent") == "human_reviewer"]
    return {"batch_id": batch_id, "count": len(out), "decisions": out}


@app.get("/escalations", summary="Compliance findings escalated for follow-up")
def list_escalations(limit: int = 200):
    """
    Where an escalation goes.

    "Escalate" is a verb that names a destination, and there was not one. The
    button wrote a line to the batch's own audit trail and stopped, so a
    reviewer who escalated a finding had no way to see it again, and nobody
    downstream had any way to find it. The word promised a handoff the system
    never performed.

    This is that list: every finding escalated across every batch, newest
    first, with what was escalated, by whom and against which payments. It
    reads the audit trail rather than a second store, so an escalation cannot
    exist in one place and not the other.

    Escalations do not auto-resolve. Clearing one is a decision, and it goes
    through the same compliance decision endpoint that raised it — an item
    that disappeared on its own would be worse than one that never moved.
    """
    entries = audit.find_entries("human_reviewer", "ESCALATED", limit=limit)

    out = []
    for e in entries:
        detail = e.get("detail") or ""
        m = re.search(r"^(\S+)\s+ESCALATED compliance finding\s+(\S+?)\.?(?:\s|$)", detail)
        note = ""
        nm = re.search(r"Note:\s*(.*?)(?:\s+This finding rests|$)", detail)
        if nm:
            note = nm.group(1).strip()
        cm = re.search(r"covering (\d+) payment", detail)
        out.append({
            "batch_id": e.get("batch_id"),
            "escalated_by": m.group(1) if m else "",
            "rule_id": m.group(2).rstrip(".") if m else "",
            "payment_count": int(cm.group(1)) if cm else 0,
            "note": note,
            "raised_at": e.get("timestamp_utc"),
            "detail": detail,
        })

    return {
        "count": len(out),
        "escalations": out,
        "summary": (
            f"{len(out)} compliance finding(s) escalated and awaiting "
            f"follow-up." if out else
            "Nothing has been escalated."
        ),
    }


@app.get("/compliance/rulebook", summary="Published compliance rulebook")
def compliance_rulebook():
    """
    The full set of controls this engine enforces: for each rule, what the
    underlying authority requires, what threshold this system actually applies,
    whether the rule is statutory / supervisory guidance / internal policy, and
    a link to the official source.

    The basis field matters: several thresholds here are this system's own risk
    appetite rather than law, and are labelled as such so nobody mistakes an
    internal ceiling for a statutory requirement.
    """
    return {
        "scope_note": compliance_rulebook_mod.scope_note(),
        "summary": compliance_rulebook_mod.rulebook_summary(),
        # SANCTIONS_HIT is the only rule here that blocks funds on a statutory
        # basis, and it is only as good as the list behind it. Publishing that
        # list's origin and age alongside the rule keeps a demo screening four
        # names from reading like one screening the UN Consolidated List.
        "sanctions_list": compliance_agent.sanctions_provenance(),
        "rules": [
            r.to_dict()
            for r in sorted(
                compliance_rulebook_mod.COMPLIANCE_RULES.values(),
                key=lambda x: (x.action != "BLOCKED", x.basis.value, x.rule_id),
            )
        ],
    }


@app.get("/history", summary="Previously recorded reconciliation runs")
def list_history(limit: int = 100, batch_id: str | None = None):
    """
    Every run this engine has recorded, newest first.

    record_run has written one of these on every reconciliation since it was
    built, and until now nothing could read them back — the files accumulated
    in data/history with no endpoint and no screen. A record nobody can
    retrieve is not a record.
    """
    runs = history.list_runs(limit=max(1, min(limit, 500)), batch_id=batch_id)
    return {"count": len(runs), "runs": runs}


@app.get("/history/{record}", summary="One recorded run in full")
def get_history_record(record: str):
    rec = history.run_detail(record)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"No such record: {record!r}")
    return rec


@app.get("/compliance/attestation/{batch_id}", summary="Per-batch compliance attestation")
def compliance_attestation(batch_id: str):
    """
    Everything a reviewer needs to understand one batch's compliance outcome:
    the decision trail, which rules fired, and the authority behind each.

    Derived from the recorded audit trail — this endpoint does not re-run the
    scan, so it reflects what the engine actually decided at the time rather
    than what it would decide now.
    """
    trail = audit.get_audit_trail(batch_id)

    # The batch's OWN trail is what makes this an attestation. The guard used
    # to accept the global scan trail as sufficient, so a batch that had never
    # been reconciled — including the literal string "undefined", which the UI
    # sent for a while — came back 200 with a full document attesting nothing.
    # A compliance record must not exist for a batch the engine never saw.
    if not trail:
        raise HTTPException(
            status_code=404,
            detail=f"No audit trail found for batch_id='{batch_id}'. "
                   f"Reconcile the batch before requesting its attestation.",
        )

    # GLOBAL_COMPLIANCE_SCAN is a single key that every run appends to, so it
    # is the whole history of every scan the engine has ever done — measured
    # at 26 MB on this machine, embedded in full into every per-batch
    # attestation. The document is meant to be handed to a reviewer; nobody
    # can read 26 MB, and almost none of it concerns this batch.
    #
    # What the reviewer actually needs from it is provenance: which sanctions
    # list was in force when this batch was screened. That is already carried
    # separately by sanctions_provenance(). A bounded, most recent slice is
    # kept for context, and the true total is reported so the trim is visible
    # rather than silent.
    SCAN_CONTEXT_LIMIT = 50
    full_scan = audit.get_audit_trail("GLOBAL_COMPLIANCE_SCAN")
    scan_trail = full_scan[-SCAN_CONTEXT_LIMIT:]

    compliance_events = [e for e in trail if e.get("agent") == "compliance_agent"]

    return {
        "batch_id": batch_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope_note": compliance_rulebook_mod.scope_note(),
        "rulebook_summary": compliance_rulebook_mod.rulebook_summary(),
        "sanctions_list": compliance_agent.sanctions_provenance(),
        "compliance_scan_events": scan_trail,
        "compliance_scan_events_total": len(full_scan),
        "compliance_scan_events_truncated": len(full_scan) > SCAN_CONTEXT_LIMIT,
        "batch_compliance_events": compliance_events,
        "decision_trail": trail,
        "event_count": len(trail),
    }


class SettlementQuestion(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000)


@app.post("/settlement/{batch_id}/ask", summary="Ask a question about a reconciliation")
def ask_settlement(batch_id: str, req: SettlementQuestion):
    """
    Plain-language Q&A over a completed reconciliation.

    Read-only and grounded: every figure in an answer comes from the engine's
    recorded results — the audit trail, match result, cash position and
    compliance findings. The model explains those facts; it does not compute,
    match or decide anything, and it has no write path.
    """
    return settlement_qa.answer_question(batch_id, req.question)


@app.get("/audit/{batch_id}", summary="Retrieve full audit trail for a batch")
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


@app.get("/demo", summary="Live 10K-scale reconciliation demo with timing")
def demo():
    """
    Runs the brief's own '412 out of 10,000' scenario end-to-end and returns
    timing, match result, and exception breakdown. Designed to demonstrate
    the CP-SAT solver at real scale during a live hackathon presentation.

    The 'naive_dp_would_take_seconds' field documents the algorithmic
    improvement — naive DP at Rs 60,000 settlement scale takes ~108s;
    CP-SAT solves the same problem in <2s.
    """
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


@app.get("/health", summary="Liveness and readiness")
def health():
    """
    What an operator needs before trusting a deployment, not just a 200.

    A health check that only says "the process is up" is the one that lets a
    node serve traffic while its audit trail is being written somewhere the
    OS will delete. Each field below is something that can be silently wrong
    and that changes whether the answers should be relied on.
    """
    storage = audit.storage_status()
    sanctions = compliance_agent.sanctions_provenance()
    warnings = []
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
        "sanctions_list": sanctions,
        "cors_origins": CORS_ORIGINS,
        "ready_for_production": not warnings,
        "warnings": warnings,
    }


