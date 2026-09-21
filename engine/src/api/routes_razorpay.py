"""
Razorpay as the primary feed: reconcile straight from the Settlement Recon API.

  GET  /razorpay/status              are keys configured, and in which mode
  POST /razorpay/reconcile           fetch live (keys required) and check
  POST /razorpay/reconcile/upload    the same checks on saved API responses

The upload route takes the JSON the two API calls return — /v1/settlements
and /v1/settlements/recon/combined — plus, optionally, a bank statement and a
ledger in any format the main upload accepts. It exists so the path can be
exercised without keys, and so a merchant can reconcile an export they
already have. Both routes run the same code (razorpay_recon.reconcile).

Keys never leave the server: the status route reports the mode and the first
characters of the Key Id, never the secret.
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
             summary="Reconcile saved Razorpay API responses, with bank and books if given")
async def reconcile_upload(
    settlements_file: UploadFile = File(..., description="JSON from GET /v1/settlements"),
    recon_file: UploadFile = File(..., description="JSON from GET /v1/settlements/recon/combined"),
    bank_file: Optional[UploadFile] = File(None),
    ledger_file: Optional[UploadFile] = File(None),
):
    settlements = _items(await _json(settlements_file, "settlements_file"), "settlements_file")
    recon = _items(await _json(recon_file, "recon_file"), "recon_file")
    bank = await _rows(bank_file, SourceType.BANK)
    ledger = await _rows(ledger_file, SourceType.ERP)
    return razorpay_recon.reconcile(settlements, recon, bank=bank, ledger=ledger)
