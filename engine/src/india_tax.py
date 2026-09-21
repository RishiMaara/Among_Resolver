"""
Which Indian tax provision applies on a given day, and at what rate.

WHY DATES AND NOT A NUMBER
--------------------------
The fee audit first shipped with a single TDS rate, and it was wrong for two
years: 1% after the Finance (No. 2) Act 2024 cut it to 0.1% from 1 October
2024 (FAILURE_LOG entry 14). A single number is wrong the day the law moves,
and it is wrong for every settlement that straddles the change. So rates here
are schedules — each entry says what applies from which day — and a payment
is taxed under the provision in force on its own date.

That matters twice in 2026. From 1 April 2026 the Income-tax Act 2025
replaces the 1961 Act, and e-commerce TDS moves from Section 194-O to
Section 393(1), Table Sl. 8(v). The rate did not change; the citation did,
and a TDS certificate or a reviewer's note that cites a repealed section for
an April payment is the kind of error an auditor circles. A batch collected
on 31 March and settled on 2 April has payments under both.

THE DATE THAT DECIDES
---------------------
TDS under 194-O / 393(1) falls due at the EARLIER of crediting the seller or
paying them. A gateway credits the merchant's account when the customer pays
and pays out at settlement, so the payment date is the earlier one and it is
the one used. Dates are taken in India Standard Time: a payment at 20:00 UTC
on 31 March is 01:30 on 1 April in India, and is under the new Act.

TCS under Section 52 of the CGST Act is collected by an e-commerce operator
on the net value of taxable supplies made through it — supplies less returns
— and the rate is set by the supply date: 1% from 1 October 2018, halved to
0.5% (0.25% CGST + 0.25% SGST, or 0.5% IGST) from 10 July 2024.

WHAT THIS IS NOT
----------------
Tax advice. Whether a gateway is the "e-commerce operator" for either
provision depends on the merchant's arrangement, which this engine cannot
see. The schedules make the arithmetic right for the day; whether the
arithmetic should run at all stays the merchant's setting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class Provision:
    effective_from: date
    rate_bps: int
    citation: str


# E-commerce TDS. Section 194-O came into force on 1 October 2020, at 0.75%
# until 31 March 2021 as COVID relief, then 1%, then 0.1% from 1 October 2024.
TDS_ECOMMERCE: tuple[Provision, ...] = (
    Provision(date(2020, 10, 1), 75, "Section 194-O, Income-tax Act 1961"),
    Provision(date(2021, 4, 1), 100, "Section 194-O, Income-tax Act 1961"),
    Provision(date(2024, 10, 1), 10, "Section 194-O, Income-tax Act 1961"),
    Provision(date(2026, 4, 1), 10, "Section 393(1), Table Sl. 8(v), Income-tax Act 2025"),
)

# GST TCS collected by an e-commerce operator.
TCS_GST_ECOMMERCE: tuple[Provision, ...] = (
    Provision(date(2018, 10, 1), 100, "Section 52, CGST Act 2017"),
    Provision(date(2024, 7, 10), 50, "Section 52, CGST Act 2017"),
)

# GST on the gateway's own fee. Not a schedule that has moved, but kept here
# so every statutory rate the audit uses is in one place with its source.
GST_ON_SERVICES_BPS = 1800


def in_force(schedule: tuple[Provision, ...], on: date) -> Provision | None:
    """The provision that applies on `on`, or None if none had begun."""
    current = None
    for p in schedule:
        if p.effective_from <= on:
            current = p
    return current


def ist_date(ts: datetime | None, fallback: date | None = None) -> date:
    """The calendar day in India for a timestamp; naive values are read as UTC."""
    if ts is None:
        return fallback or datetime.now(IST).date()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(IST).date()


# ── Input tax credit on the gateway's monthly invoice ─────────────────────

@dataclass
class GstInvoice:
    """What the gateway's monthly tax invoice states, in paise."""
    invoice_number: str
    period: str                  # "YYYY-MM"
    taxable_value_cents: int     # fees before GST
    gst_cents: int               # IGST, or CGST + SGST


def itc_check(invoice: GstInvoice, deducted_fee_cents: int,
              deducted_gst_cents: int) -> dict:
    """
    Compare the GST the gateway deducted across a month's settlements with
    the GST on its invoice for that month.

    Input tax credit is claimed on the invoice, not on the deductions, so the
    invoice is what can be claimed. The comparison says what to do about the
    difference:

    - deducted more than invoiced: GST was paid that no invoice supports. That
      part cannot be claimed until the gateway issues an invoice or a debit
      note for it — ask for one.
    - invoiced more than deducted: the invoice carries GST on fees that were
      not taken from settlements. Claiming it would claim credit on a charge
      the books do not show — ask for a credit note, or find the charge.

    It also checks the invoice against itself: GST at 18% of the taxable value
    it states, within a rupee for rounding.
    """
    gst_diff = deducted_gst_cents - invoice.gst_cents
    fee_diff = deducted_fee_cents - invoice.taxable_value_cents
    expected_invoice_gst = round(invoice.taxable_value_cents * GST_ON_SERVICES_BPS / 10_000)
    invoice_consistent = abs(expected_invoice_gst - invoice.gst_cents) <= 100

    if gst_diff == 0:
        status, plain = "matched", (
            f"The GST on invoice {invoice.invoice_number} equals the GST deducted "
            f"from {invoice.period}'s settlements. All of it is claimable as input "
            f"tax credit, subject to it appearing in GSTR-2B.")
    elif gst_diff > 0:
        status, plain = "deducted_more_than_invoiced", (
            f"Settlements in {invoice.period} deducted ₹{gst_diff / 100:,.2f} more "
            f"GST than invoice {invoice.invoice_number} carries. That amount cannot "
            f"be claimed until the gateway invoices it; ask for a revised invoice "
            f"or a debit note.")
    else:
        status, plain = "invoiced_more_than_deducted", (
            f"Invoice {invoice.invoice_number} carries ₹{-gst_diff / 100:,.2f} more "
            f"GST than {invoice.period}'s settlements deducted. Do not claim that "
            f"part until the charge behind it is found in the books; ask for a "
            f"credit note if there is none.")

    return {
        "invoice_number": invoice.invoice_number,
        "period": invoice.period,
        "status": status,
        "plain": plain,
        "invoice_gst_cents": invoice.gst_cents,
        "deducted_gst_cents": deducted_gst_cents,
        "gst_difference_cents": gst_diff,
        "fee_difference_cents": fee_diff,
        "claimable_itc_cents": min(invoice.gst_cents, deducted_gst_cents),
        "not_yet_claimable_cents": max(gst_diff, 0),
        "invoice_internally_consistent": invoice_consistent,
        "expected_invoice_gst_cents": expected_invoice_gst,
    }
