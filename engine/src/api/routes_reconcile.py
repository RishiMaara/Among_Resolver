"""
Reconciliation endpoints: JSON (/reconcile, /multi, /joint), files
(/reconcile/upload, /reconcile/queue) and reading settlements out of a
statement (/settlements/detect). Handlers parse the request and call
pipeline / orchestrator / settlement_run; what a run does lives there.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

import file_agent
import fx
import settlement_run
from api.models import (
    JointReconcileRequest, MultiSourceReconcileRequest, ReconcileRequest,
    _build_settlement_batch,
)
from api.presentation import (
    _format_report, _rate_card, _rate_card_is_assumed, _settlement_instant,
)
from api.uploads import ingest_upload, read_upload_capped
from ingestion import normalize_amount_to_cents, normalize_batch
from orchestrator import reconcile_many
from pipeline import reconcile_settlement
from schema import NormalizedTxn, SettlementBatch, SourceType

router = APIRouter()


@router.post("/reconcile", summary="Reconcile gateway transactions against a settlement batch")
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


@router.post("/reconcile/multi", summary="Reconcile across gateway, bank, and ERP sources")
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


@router.post("/reconcile/joint", summary="Reconcile several settlements at once against one shared pool")
def reconcile_joint(req: JointReconcileRequest):
    """
    N:M: every settlement in the request solved at once against one shared pool
    (see orchestrator.reconcile_many for what carries over from the 1:N path).
    Compliance screening runs once over the whole pool first. One result per
    batch, in request order, shaped like /reconcile's.
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


@router.post("/reconcile/upload", summary="Upload raw CSV/JSON files for reconciliation")
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
    # Who asked for this run, for the audit trail. Optional: the engine is
    # usable without the UI.
    reviewer: Optional[str] = Form(None),
    # Opt-in: which payout absorbs a clawback is the processor's timing.
    include_chargebacks: bool = Form(False),
    # Hand a settlement that does not clear to the investigator; the model
    # proposes only when asked for AND configured.
    investigate: bool = Form(False),
    investigate_with_model: bool = Form(False),
    # What OCR in the browser read off a scanned bank statement. Used only if
    # bank_file is a scan, and only if the reading balances line by line.
    bank_scan_text: str = Form("", max_length=200_000),
    # Exchange rates into `currency` as the settlement advice states them,
    # "USD=83.1250,EUR=90.40". Only a declared rate converts (fx.py).
    fx_rates: str = Form("", max_length=500),
    gateway_file: Optional[UploadFile] = File(None),
    bank_file: Optional[UploadFile] = File(None),
    erp_file: Optional[UploadFile] = File(None),
):
    """
    Reconcile one settlement from uploaded files (CSV, JSON, bank formats):
    headers mapped, rows normalised, then the full pipeline, the paid-out
    check, and (on request) the investigator.
    """
    notes: list[str] = []
    candidates: list[NormalizedTxn] = []
    for upload, source in ((gateway_file, SourceType.GATEWAY), (bank_file, SourceType.BANK),
                           (erp_file, SourceType.ERP)):
        candidates += await ingest_upload(
            upload, source, batch_id=batch_id, notes=notes,
            scan_text=bank_scan_text if source is SourceType.BANK else "")
    if not candidates:
        raise HTTPException(status_code=400, detail="No valid transactions extracted from the uploaded files.")

    taken: list[str] = []
    if include_chargebacks:
        reversals = settlement_run.take_chargebacks(
            batch_id, _settlement_instant(settled_at), settlement_window_days)
        candidates.extend(reversals)
        taken = [t.source_txn_id for t in reversals]

    try:
        member = settlement_run.parse_member_source(member_source)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    try:
        rates = fx.parse_rates(fx_rates)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None

    batch = SettlementBatch(
        batch_id=batch_id,
        net_amount_cents=normalize_amount_to_cents(net_amount),
        currency=currency,
        settled_at_utc=_settlement_instant(settled_at),
        member_source=member,
        merchant=merchant_id,
        fx_rates=rates,
        # Declared deductions make the gross target a fact, not an estimate.
        declared_deductions_cents=(normalize_amount_to_cents(declared_deductions)
                                   if declared_deductions is not None else None),
    )
    assumed = _rate_card_is_assumed(gateway_fee_bps, tax_withholding_bps, flat_fee_cents)
    inputs = {
        "batch_id": batch_id, "net_amount": net_amount, "currency": currency,
        "settled_at": settled_at, "settlement_window_days": settlement_window_days,
        "gateway_file": gateway_file.filename if gateway_file else None,
        "bank_file": bank_file.filename if bank_file else None,
        "erp_file": erp_file.filename if erp_file else None,
        "reviewer": (reviewer or "").strip() or None,
        "rate_card_assumed": assumed,
    }
    # Off the event loop: the solve is CPU-bound, and the rest reads and
    # writes the shared stores.
    report = await run_in_threadpool(
        reconcile_settlement, batch, candidates,
        settlement_window_days=settlement_window_days,
        rate_card=_rate_card(gateway_fee_bps, tax_withholding_bps, flat_fee_cents),
    )
    return await run_in_threadpool(
        settlement_run.finish_upload_run, batch, candidates, report,
        notes=notes, inputs=inputs, taken_reversals=taken,
        investigate=investigate, investigate_with_model=investigate_with_model,
        reviewer=reviewer, rate_card_assumed=assumed,
        deductions_declared=bool(declared_deductions),
    )


@router.post("/settlements/detect", summary="Read settlements out of a statement")
async def detect_settlements(file: UploadFile = File(...)):
    """
    Read the settlement rows (credit lines) out of an uploaded statement, with
    the same parser /reconcile/queue uses, so the form is filled from the file
    rather than retyped. Returns candidates; nothing is reconciled here.
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


@router.post("/reconcile/queue", summary="Reconcile many settlements in one run")
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
    A day's settlements against one candidate pool. Feeds are parsed and
    normalised once; each settlement is reconciled independently and a
    failure is reported beside the results that succeeded.
    """
    settlements_raw = await read_upload_capped(settlements_file)
    try:
        settlement_rows = file_agent.parse_settlements(
            settlements_raw, settlements_file.filename or "settlements")
    except file_agent.FileRejected as e:
        raise HTTPException(status_code=422, detail={
            "message": str(e), "source": "settlements",
            "filename": settlements_file.filename, "rejected": True, **e.to_dict(),
        }) from e

    notes: list[str] = []
    candidates: list[NormalizedTxn] = []
    for upload, source in ((gateway_file, SourceType.GATEWAY), (bank_file, SourceType.BANK),
                           (erp_file, SourceType.ERP)):
        candidates += await ingest_upload(upload, source, batch_id="QUEUE", notes=notes)

    try:
        member = settlement_run.parse_member_source(member_source)
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"message": str(e)}) from None

    out = await run_in_threadpool(
        settlement_run.run_queue, settlement_rows, candidates,
        member_source=member, merchant_id=merchant_id, window_days=settlement_window_days,
        rate_card=_rate_card(gateway_fee_bps, tax_withholding_bps, flat_fee_cents),
    )
    out["ingestion_notes"] = notes
    return out
