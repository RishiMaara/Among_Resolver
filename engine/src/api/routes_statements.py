"""
Read a bank statement and show the proof that it was read correctly.

`POST /statements/parse` takes MT940, CAMT.053, OFX or a text PDF and returns
every line with the balance check — opening + credits - debits = closing,
and the running balance line by line. The same parse runs when a statement
is uploaded as the bank file of a reconciliation; this route exists so the
reading can be checked on its own, before anything depends on it.
"""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile

import statement_parsers

router = APIRouter()


@router.post("/statements/parse", summary="Parse a bank statement and check that it balances")
async def parse_statement(file: UploadFile = File(...)):
    from main import read_upload_capped  # pylint: disable=import-outside-toplevel
    content = await read_upload_capped(file)
    try:
        st, check = statement_parsers.parse(content, file.filename or "")
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
    }
