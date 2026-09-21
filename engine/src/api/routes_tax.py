"""
Tax checks a controller runs outside a single settlement.

`GET /tax/provisions` says which TDS and TCS provision applies on a date, so
a reviewer can see why a finding cites Section 194-O for a March payment and
Section 393(1) for an April one. `POST /tax/itc-check` compares the GST a
gateway deducted over a month with the GST on its monthly invoice — the
invoice is what input tax credit is claimed on, so the difference is money
that cannot be claimed until somebody issues a document.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import india_tax

router = APIRouter()


def _provision(schedule, on: date) -> dict | None:
    p = india_tax.in_force(schedule, on)
    if p is None:
        return None
    return {"rate_bps": p.rate_bps, "rate_percent": p.rate_bps / 100,
            "citation": p.citation, "in_force_from": p.effective_from.isoformat()}


@router.get("/tax/provisions", summary="TDS and TCS provisions in force on a date")
def provisions(on: str = Query("", description="YYYY-MM-DD, in India; default today")):
    try:
        day = date.fromisoformat(on) if on.strip() else india_tax.ist_date(None)
    except ValueError:
        raise HTTPException(status_code=422, detail={
            "message": "on must be YYYY-MM-DD",
            "plain": f"Could not read {on!r} as a date.",
        })
    return {
        "on": day.isoformat(),
        "ecommerce_tds": _provision(india_tax.TDS_ECOMMERCE, day),
        "gst_tcs": _provision(india_tax.TCS_GST_ECOMMERCE, day),
        "gst_on_gateway_fee_bps": india_tax.GST_ON_SERVICES_BPS,
        "note": ("Which provision applies is set by the payment date. Whether "
                 "it applies to this merchant at all depends on their "
                 "arrangement with the gateway; this is not tax advice."),
    }


class ItcCheck(BaseModel):
    invoice_number: str
    period: str                     # YYYY-MM
    invoice_taxable_value_cents: int
    invoice_gst_cents: int
    deducted_fee_cents: int         # fees before GST, summed over the month
    deducted_gst_cents: int


@router.post("/tax/itc-check", summary="Reconcile a month's deducted GST with the gateway's invoice")
def itc(body: ItcCheck):
    if min(body.invoice_taxable_value_cents, body.invoice_gst_cents,
           body.deducted_fee_cents, body.deducted_gst_cents) < 0:
        raise HTTPException(status_code=422, detail={
            "message": "amounts must not be negative",
            "plain": "Send each amount as a positive figure in paise.",
        })
    return india_tax.itc_check(
        india_tax.GstInvoice(body.invoice_number.strip(), body.period.strip(),
                             body.invoice_taxable_value_cents, body.invoice_gst_cents),
        body.deducted_fee_cents, body.deducted_gst_cents,
    )
