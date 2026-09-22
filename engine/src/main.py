"""
FastAPI entrypoint for the AI Finance Controller — Reconciliation Engine.

Endpoints:
  POST /reconcile            — single gateway source + one settlement batch
  POST /reconcile/multi      — gateway + bank + ERP sources in one call
  POST /reconcile/joint      — N:M: several settlement batches solved at once
                               against one shared pool (see orchestrator.reconcile_many)
  POST /reconcile/queue      — a day's settlements, reconciled one after another
                               against one shared pool (see settled_ledger)
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
from datetime import datetime, timedelta, timezone
from typing import Optional

import dateutil.parser as dp
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from schema import SourceType, SettlementBatch, NormalizedTxn, TzConfidence
from ingestion import normalize_batch, normalize_batch_with_report, normalize_amount_to_cents
from orchestrator import reconcile_batch, reconcile_many
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
import webhook
import chargeback_engine
from plain_summary import plain_summary
import re
import auto_disposition
import settled_ledger
import open_items
import investigation_agent
import llm_provider
import model_budget
import settlement_cycle
import india_calendar

# Without this, every logger.info(...) call in the pipeline (compliance
# blocks, linkage narrowing, which tier cleared) is silently swallowed by
# Python's default WARNING log level -- a long run looks completely idle
# with zero terminal output. That silence was originally mistaken for a
# hang during the 50K stress test.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)



# Moved out of this file; re-exported because tests and callers still
# reach for them here, and because the split is an internal detail.
from api.models import (  # noqa: E402,F401
    TxnIn, SettlementBatchIn, ReconcileRequest, MultiSourceReconcileRequest,
    JointReconcileRequest, _build_settlement_batch,
)
from api.presentation import (  # noqa: E402,F401
    _format_report, _check_then_record, contested_payments, compliance_review,
    interchangeable_note, matched_rows, _settlement_instant, _rate_card,
    _rate_card_is_assumed,
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

# Optional: a regex for origins that cannot be listed ahead of time.
#
# A Vercel deployment serves every preview at its own generated host
# (project-git-branch-owner.vercel.app), so an explicit list can only ever name
# production — and a preview of the frontend then fails every request with a
# CORS error that looks exactly like the engine being down. Anchored by
# Starlette (fullmatch), so "https://my-app.*\.vercel\.app" cannot be satisfied
# by an attacker's "https://my-app.evil.example/.vercel.app".
CORS_ORIGIN_REGEX = os.environ.get("CORS_ORIGIN_REGEX", "").strip() or None
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
async def _meter_model_calls(request, call_next):
    """
    Name the visitor, so model calls made while serving them can be metered.

    Vercel sets x-real-ip and the first x-forwarded-for hop from the connection
    it received; elsewhere the socket's address is the best there is.
    """
    ip = (request.headers.get("x-real-ip")
          or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else ""))
    model_budget.begin_request(ip)
    return await call_next(request)


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
    # Paths that prove who they are by HMAC instead of by API key. See
    # auth.HMAC_AUTHENTICATED_PATHS for why this is a different door and not
    # a hole in the same one.
    if request.url.path in auth.HMAC_AUTHENTICATED_PATHS:
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
    allow_origin_regex=CORS_ORIGIN_REGEX,
    # Only meaningful once origins are explicit, which is now the default.
    allow_credentials=CORS_ORIGINS != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.post("/reconcile/joint", summary="Reconcile several settlements at once against one shared pool")
def reconcile_joint(req: JointReconcileRequest):
    """
    N:M reconciliation. Solves every settlement batch in the request
    SIMULTANEOUSLY against one shared candidate pool — see
    orchestrator.reconcile_many's docstring for the full reasoning and for
    what safety logic it carries over from /reconcile's 1:N path (linkage
    narrowing, anchored-refund forcing, per-target ambiguity probing,
    evidence-based withholding, confidence gating) versus what it does not
    (the 1:N tiering and cross-feed substitutability refinements).

    Compliance screening runs once over the full shared pool before any
    batch is solved, exactly as /reconcile/multi does for one batch — a
    transaction the firm may not touch must not become part of ANY cleared
    settlement here either.

    Returns one result per batch, each shaped exactly like /reconcile's
    response (cash position, matched transactions, exceptions, compliance
    review, audit trail) via the same _format_report used everywhere else,
    in the same order settlement_batches was given.
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

    batches = [_build_settlement_batch(b) for b in req.settlement_batches]
    reports = reconcile_many(batches, candidates, settlement_window_days=req.settlement_window_days)
    return {
        "results": [
            _format_report(report, batch=batch, candidates=candidates)
            for report, batch in zip(reports, batches)
        ],
    }


@app.post("/reconcile/upload", summary="Upload raw CSV/JSON files for reconciliation")
async def reconcile_upload(
    batch_id: str = Form(...),
    net_amount: float = Form(...),
    currency: str = Form("INR"),
    settled_at: str = Form(...),
    settlement_window_days: int = Form(5),
    member_source: Optional[str] = Form(None),
    # Whose settlement: keeps what one merchant's payouts teach (the settlement
    # cycle) from teaching another's. Empty for a single-merchant deployment.
    merchant_id: str = Form("", max_length=64),
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
    # Chargeback reversals wait until a settlement takes them in. Opt-in,
    # because which payout absorbs a clawback is a fact about the processor's
    # timing rather than something the engine should assume.
    include_chargebacks: bool = Form(False),
    # A settlement that does not clear can be handed to the investigator
    # (investigation_agent.py): read-only case, one typed proposal, verified
    # in code before anyone sees it. The model proposer only when asked for
    # AND configured; otherwise fixed rules propose.
    investigate: bool = Form(False),
    investigate_with_model: bool = Form(False),
    # What OCR in the browser read off a scanned bank statement. Used only if
    # bank_file is a scan, and only if the reading balances line by line.
    bank_scan_text: str = Form("", max_length=200_000),
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
                content, upload_file.filename or "", header_warnings,
                scan_text=bank_scan_text if source_type == SourceType.BANK else ""
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

    taken_reversals: list[str] = []
    if include_chargebacks:
        # Only reversals this settlement could actually absorb: filed inside
        # its window. Taking one outside it would add a row the window filter
        # drops a moment later — and then mark it taken, removing money owed
        # back from this batch AND every later one. That is what the first
        # version did; the test that caught it is in test_chargebacks.py.
        settled = _settlement_instant(settled_at)
        window_start = settled - timedelta(days=settlement_window_days)
        waiting = chargeback_engine.pending()
        reversals = [t for t in waiting if window_start <= t.timestamp_utc <= settled]
        candidates.extend(reversals)
        taken_reversals = [t.source_txn_id for t in reversals]
        audit.log_decision(
            batch_id=batch_id, agent="chargeback_engine",
            detail=(f"{len(reversals)} of {len(waiting)} pending chargeback "
                    f"reversal(s) fall inside this settlement's window and were "
                    f"added to its pool, totalling "
                    f"{sum(t.amount_cents for t in reversals)}c. The rest stay "
                    f"pending. The settlements the originals cleared in are "
                    f"untouched."),
        )

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
        merchant=merchant_id,
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

    # Only now — the batch reconciled with them in the pool, so they are
    # accounted for. Dropping them from pending any earlier would lose money
    # owed back from every future batch as well as this one.
    if taken_reversals:
        chargeback_engine.mark_taken(taken_reversals, batch_id)
        formatted["chargeback_reversals_included"] = taken_reversals

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

    # A verified clear teaches the processor's settlement cycle. After the
    # already-settled check, so a clear that check withdrew teaches nothing.
    if (formatted.get("summary") or {}).get("cleared") and matched_ids:
        settlement_cycle.learn_from(batch, candidates, matched_ids)

    if investigate and not (formatted.get("summary") or {}).get("cleared"):
        found = investigation_agent.investigate(
            batch, candidates, report,
            use_model=investigate_with_model and llm_provider.is_configured())
        if found:
            formatted["investigation"] = found
            prop, ver = found["proposal"], found["verification"]
            audit.log_decision(
                batch_id=report.batch_id, agent="investigator",
                detail=(f"Proposed {prop['action']} ({prop['proposer']}): {prop['reason']} "
                        + ("Verified in code; still a proposal for a reviewer."
                           if ver["valid"] else "REJECTED by the verifier: "
                           + "; ".join(ver["failed"]))))

    # What is still waiting to be paid out, carried to the next run. After the
    # already-settled check, so a clear that check withdrew closes nothing.
    formatted["open_items"] = open_items.update_from_run(
        batch, candidates, matched_ids,
        cleared=bool((formatted.get("summary") or {}).get("cleared")))

    formatted["plain_summary"] = plain_summary(
        formatted.get("summary") or {}, formatted.get("reasoning") or "",
        formatted.get("interchangeable"))
    # Which answers in this response came from a model, and if one was
    # skipped, why — so a rules answer is never read as a model one.
    formatted["ai"] = model_budget.report()

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
    # Whose settlement: keeps what one merchant's payouts teach (the settlement
    # cycle) from teaching another's. Empty for a single-merchant deployment.
    merchant_id: str = Form("", max_length=64),
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
    queue_outcomes = []
    for row in settlement_rows:
        bid = str(row.get("batch_id") or "").strip()
        try:
            batch = SettlementBatch(
                batch_id=bid,
                net_amount_cents=normalize_amount_to_cents(row["net_amount"]),
                currency=str(row.get("currency") or "INR"),
                settled_at_utc=_settlement_instant(str(row["settled_at"])),
                member_source=parsed_member_source,
                merchant=merchant_id,
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
            queue_outcomes.append((batch, report.match_result.matched_txn_ids,
                                   bool(summary["cleared"])))
            # Learned as the queue goes: a payout with references that clears
            # teaches the cycle to the payouts after it that have none.
            if summary["cleared"]:
                settlement_cycle.learn_from(batch, candidates,
                                            report.match_result.matched_txn_ids)
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
    ledger_delta = open_items.update_from_runs(queue_outcomes, candidates)
    return {
        "open_items": ledger_delta,
        "queued": len(results),
        "candidates_pooled": len(candidates),
        "tally": tally,
        "needs_review": tally["withheld"] + tally["unmatched"] + tally["error"],
        # Only answerable across a whole run: has one payment been spent twice?
        "contested_payments": contested_payments(results),
        "results": results,
        "ingestion_notes": ingestion_notes,
    }



# Route groups that used to sit inline here. Registered rather than
# defined, so this file is app construction and the operational
# endpoints, not nine hundred lines of handlers.
from api.routes_decisions import router as _decisions_router  # noqa: E402
from api.routes_reports import router as _reports_router      # noqa: E402
from api.routes_webhook import router as _webhook_router      # noqa: E402
from api.routes_chargebacks import router as _chargeback_router  # noqa: E402
from api.routes_tax import router as _tax_router              # noqa: E402
from api.routes_open_items import router as _open_items_router  # noqa: E402
from api.routes_razorpay import router as _razorpay_router    # noqa: E402
from api.routes_statements import router as _statements_router  # noqa: E402
from api.routes_exports import router as _exports_router      # noqa: E402
from api.routes_ai import router as _ai_router                # noqa: E402

app.include_router(_decisions_router)
app.include_router(_reports_router)
app.include_router(_webhook_router)
app.include_router(_chargeback_router)
app.include_router(_tax_router)
app.include_router(_open_items_router)
app.include_router(_razorpay_router)
app.include_router(_statements_router)
app.include_router(_exports_router)
app.include_router(_ai_router)


@app.get("/audit/{batch_id}/verify", summary="Check a batch's audit trail has not been altered")
def verify_audit(batch_id: str, receipt: str = ""):
    """
    Walk the hash chain. With `receipt` — the `audit_head` a reconciliation
    returned — also prove nothing after it was cut off.
    """
    return audit.verify_chain(batch_id, receipt=receipt.strip())


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


