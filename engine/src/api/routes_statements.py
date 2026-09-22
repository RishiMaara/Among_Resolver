"""
Read a bank statement and show the proof that it was read correctly.

`POST /statements/parse` takes MT940, CAMT.053, OFX or a text PDF and returns
every line with the balance check — opening + credits - debits = closing,
and the running balance line by line. The same parse runs when a statement
is uploaded as the bank file of a reconciliation; this route exists so the
reading can be checked on its own, before anything depends on it.
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

import llm_provider
import model_budget
import narration_reader
import statement_parsers

router = APIRouter()


@router.post("/statements/parse", summary="Parse a bank statement and check that it balances")
async def parse_statement(file: UploadFile = File(...),
                          scan_text: str = Form("", max_length=200_000)):
    """
    scan_text: what OCR in the visitor's browser read, when the file is a
    scan. The engine reads it with the same parser as a text PDF and uses it
    only if it balances; otherwise the model reads the scan, where configured.
    """
    from main import read_upload_capped  # pylint: disable=import-outside-toplevel
    content = await read_upload_capped(file)
    try:
        st, check = statement_parsers.parse(content, file.filename or "", scan_text=scan_text)
    except statement_parsers.StatementUnreadable as exc:
        raise HTTPException(status_code=422, detail={
            "message": "statement unreadable", "plain": str(exc)})
    return {
        "format": st.format,
        "account": st.account,
        "currency": st.currency,
        "lines": [{"booked": ln.booked.isoformat(),
                   "value_date": ln.value_date.isoformat() if ln.value_date else None,
                   "amount_cents": ln.amount_cents, "description": ln.description,
                   "reference": ln.reference, "bank_reference": ln.bank_reference,
                   "balance_cents": ln.balance_cents} for ln in st.lines],
        "check": check,
        "notes": st.notes,
        # Who read it. A scan's reading is shown as a reading, never as the file.
        "read_by": {"scan_ocr": "OCR in the browser (Tesseract.js)",
                    "scan": f"{llm_provider.DEFAULT_MODEL}, from the scan"}.get(
                        st.format, "the engine's parser, from the file's text"),
        "ai": model_budget.report(),
    }


class NarrationRequest(BaseModel):
    narrations: list[str]
    use_model: bool = False


@router.post("/narrations/read", summary="Read UTR, settlement ref, counterparty and rail")
def read_narrations(req: NarrationRequest):
    """
    Regex always; the model only when asked for and configured, and only
    with values that appear in the narration they came from.
    """
    if len(req.narrations) > 500:
        raise HTTPException(status_code=422, detail={
            "message": "too many narrations",
            "plain": "Send at most 500 narrations per request."})
    stats: dict = {}
    use = req.use_model and llm_provider.is_configured()
    rows = narration_reader.read(req.narrations, use_llm=use, stats=stats)
    return {
        "reader": "model first, regex fallback" if use else "regex",
        "model_requested_but_unavailable": req.use_model and not use,
        "ungrounded_values_dropped": stats.get("ungrounded", 0),
        "results": [{"narration": t, **r} for t, r in zip(req.narrations, rows)],
        "ai": model_budget.report(),
    }
