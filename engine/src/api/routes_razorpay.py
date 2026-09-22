"""
Razorpay as the primary feed: reconcile from the Settlement Recon API.

GET  /razorpay/status              are keys configured, and in which mode
POST /razorpay/reconcile           fetch live (keys required) and check
POST /razorpay/reconcile/upload    the same checks on saved API responses

Both run razorpay_recon.reconcile. Keys never leave the server.
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

import file_agent
import razorpay_recon
import razorpay_source as rz
from ingestion import normalize_batch_with_report
from schema import SourceType

router = APIRouter()


@router.get("/razorpay/status", summary="Whether a Razorpay account is connected")
def status():
    creds = rz.credentials_from_env()
    if not creds:
        return {"connected": False,
                "plain": ("No Razorpay keys on this server. Set RAZORPAY_KEY_ID and "
                          "RAZORPAY_KEY_SECRET (test-mode keys are free), or upload "
                          "saved API responses to /razorpay/reconcile/upload.")}
    return {"connected": True, "mode": "test" if creds.is_test_mode else "live",
            "key_id_prefix": creds.key_id[:12],
            "plain": "Read-only: this engine only ever GETs from Razorpay."}


class LiveRequest(BaseModel):
    year: int
    month: int
    day: Optional[int] = None
    count: int = 10
    blind: bool = True


@router.post("/razorpay/reconcile", summary="Fetch settlements live and reconcile them")
def reconcile_live(req: LiveRequest):
    creds = rz.credentials_from_env()
    if not creds:
        raise HTTPException(status_code=503, detail={
            "message": "Razorpay keys are not configured",
            "plain": ("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET on the server, or "
                      "use /razorpay/reconcile/upload with saved API responses."),
        })
    try:
        settlements = rz.fetch_settlements(creds, count=req.count)
        recon = rz.fetch_recon(creds, req.year, req.month, req.day)
    except rz.RazorpayError as exc:
        raise HTTPException(status_code=502, detail={"message": "Razorpay request failed",
                                                     "plain": str(exc)})
    out = razorpay_recon.reconcile(settlements, recon, blind=req.blind)
    out["mode"] = "test" if creds.is_test_mode else "live"
    return out


def _items(payload, name: str) -> list[dict]:
    """Accept the API response as returned ({"items": [...]}) or a bare list."""
    if isinstance(payload, dict) and "items" in payload:
        payload = payload["items"]
    if isinstance(payload, dict) and payload.get("entity") == "settlement":
        payload = [payload]
    if not isinstance(payload, list) or not all(isinstance(x, dict) for x in payload):
        raise HTTPException(status_code=422, detail={
            "message": f"{name} is not a Razorpay collection",
            "plain": (f"{name} should be the JSON Razorpay returned — an object with "
                      f"an 'items' list, or the list itself."),
        })
    return payload


async def _json(upload: UploadFile, name: str):
    from main import read_upload_capped  # pylint: disable=import-outside-toplevel
    raw = await read_upload_capped(upload)
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=422, detail={
            "message": f"{name} is not JSON",
            "plain": f"Could not read {upload.filename or name} as JSON.",
        })


async def _rows(upload: Optional[UploadFile], source: SourceType):
    if upload is None:
        return None
    from main import read_upload_capped  # pylint: disable=import-outside-toplevel
    content = await read_upload_capped(upload)
    if not content:
        return None
    parsed = file_agent.parse_file_content(content, upload.filename or "", [])
    return normalize_batch_with_report(parsed, source).normalized if parsed else []


@router.post("/razorpay/reconcile/upload",
             summary="Reconcile Razorpay's recon data — API responses or a dashboard report")
async def reconcile_upload(
    recon_file: UploadFile = File(..., description=(
        "JSON from GET /v1/settlements/recon/combined, or the Settlement Recon report "
        "downloaded from the Razorpay Dashboard as CSV")),
    settlements_file: Optional[UploadFile] = File(None, description=(
        "JSON from GET /v1/settlements. Optional: without it each payout's amount is "
        "taken from its lines, and the bank credit is the independent check")),
    bank_file: Optional[UploadFile] = File(None),
    ledger_file: Optional[UploadFile] = File(None),
):
    """
    Self-serve: a merchant can reconcile the report they download themselves,
    with no API keys and nothing shared — on a self-hosted engine
    (docker compose up) the data never leaves their machine.
    """
    from main import read_upload_capped  # pylint: disable=import-outside-toplevel
    raw = await read_upload_capped(recon_file)
    note = ""
    if raw.lstrip()[:1] in (b"{", b"["):
        try:
            recon = _items(json.loads(raw.decode("utf-8-sig")), "recon_file")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(status_code=422, detail={
                "message": "recon_file is not JSON", "plain": "Could not read the recon file as JSON."})
    else:
        try:
            recon, note = rz.report_items(raw)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={
                "message": "recon_file unreadable",
                "plain": f"Could not read {recon_file.filename or 'the report'}: {exc}."})
    if settlements_file is not None:
        settlements = _items(await _json(settlements_file, "settlements_file"), "settlements_file")
    else:
        settlements = rz.settlements_from_report(recon)
    bank = await _rows(bank_file, SourceType.BANK)
    ledger = await _rows(ledger_file, SourceType.ERP)
    out = razorpay_recon.reconcile(settlements, recon, bank=bank, ledger=ledger)
    out["read"] = {
        "recon": "dashboard report (CSV)" if note else "API response (JSON)",
        "units": note or "Amounts in paise and times in Unix seconds, as the API documents.",
        "settlements": ("taken from the settlements list" if settlements_file is not None else
                        "derived from the report's own lines — tie-out is by construction; "
                        "the bank credit is the independent check"),
    }
    return out
