"""
The statutory tax checks on a matched set: e-commerce TDS (Section 194-O,
then 393(1) Sl. 8(v) from 1 April 2026) and GST TCS under Section 52 CGST,
each under the law in force on the payment's own date (india_tax.py).
"""

from __future__ import annotations

from datetime import date
import india_tax

from fee_model import FeeAuditCategory, FeeAuditFinding, FeeAuditSeverity, MethodRateCard, _gross_cents, _stated_paise


def _rate_on(override: int | None, schedule, on: date) -> tuple[int, str]:
    """(rate in bps, citation) for a day, from an override or the schedule."""
    provision = india_tax.in_force(schedule, on)
    citation = provision.citation if provision else ""
    if override is not None:
        return override, citation
    return (provision.rate_bps if provision else 0), citation


def _cite(parts: dict[str, int]) -> str:
    """One citation, or each with the amount it covers when a batch spans two."""
    named = {c: v for c, v in parts.items() if c}
    if len(named) <= 1:
        return next(iter(named), "")
    return "; ".join(f"{c} on {v / 100:,.2f}" for c, v in named.items())


def audit_tds_194o(
    matched_txns: list,
    annual_gross_cents: int = 0,
    rate_card: MethodRateCard | None = None,
    as_of: date | None = None,
) -> list[FeeAuditFinding]:
    """
    Check e-commerce TDS: 194-O, and 393(1) Sl. 8(v) from April 2026.

    Each payment is taxed under the provision in force on its own IST date, so a
    batch straddling 1 April cites both. Individual/HUF sellers are exempt on
    the first Rs 5 lakh a year, used up by the earliest payments; set the
    threshold to 0 for other sellers and the rate to 0 to turn the check off.
    Arithmetic against configured numbers, not tax advice.
    """
    if rate_card is None:
        rate_card = MethodRateCard()
    if rate_card.tds_rate_bps == 0:
        return []  # TDS checking disabled

    threshold = max(rate_card.tds_annual_threshold_cents, 0)
    running = annual_gross_cents
    expected_tds = 0
    taxable_by_citation: dict[str, int] = {}

    dated = sorted(
        matched_txns,
        key=lambda t: india_tax.ist_date(getattr(t, "timestamp_utc", None), as_of),
    )
    for txn in dated:
        gross = abs(_gross_cents(txn))
        exempt_left = max(threshold - running, 0)
        taxable = max(gross - exempt_left, 0)
        running += gross
        if taxable == 0:
            continue
        on = india_tax.ist_date(getattr(txn, "timestamp_utc", None), as_of)
        rate, citation = _rate_on(rate_card.tds_rate_bps, india_tax.TDS_ECOMMERCE, on)
        expected_tds += round(taxable * rate / 10_000)
        taxable_by_citation[citation] = taxable_by_citation.get(citation, 0) + taxable

    if not taxable_by_citation:
        return []

    actual_tds = sum(
        _stated_paise(getattr(t, "extra", {}) or {}, "tds_amount_cents", "tds", "tds_amount") or 0
        for t in matched_txns
    )

    diff = expected_tds - actual_tds
    if abs(diff) <= 100:  # Rs 1 tolerance
        return []

    citation = _cite(taxable_by_citation)
    return [FeeAuditFinding(
        category=FeeAuditCategory.TDS_WITHHOLDING_ERROR,
        severity=FeeAuditSeverity.HIGH,
        txn_id="batch",
        expected_cents=expected_tds,
        actual_cents=actual_tds,
        difference_cents=diff,
        description=(
            f"E-commerce TDS ({citation}): year-to-date gross {running/100:,.2f} "
            f"against a threshold of {threshold/100:,.2f}. Expected TDS "
            f"{expected_tds/100:,.2f}, withheld {actual_tds/100:,.2f}, "
            f"difference {diff/100:,.2f}."
        ),
        rule_basis="statutory",
        citation=citation,
    )]


def audit_tcs_section52(
    matched_txns: list,
    rate_card: MethodRateCard | None = None,
    as_of: date | None = None,
) -> list[FeeAuditFinding]:
    """
    GST TCS under Section 52 CGST, on net taxable supplies (refunds reduce the
    base), at the rate on each supply's date. Runs only when a row reports TCS.
    """
    if rate_card is None:
        rate_card = MethodRateCard()
    if rate_card.tcs_rate_bps == 0:
        return []

    stated = [
        _stated_paise(getattr(t, "extra", {}) or {}, "tcs_amount_cents", "tcs", "tcs_amount")
        for t in matched_txns
    ]
    if all(v is None for v in stated):
        return []
    actual = sum(v or 0 for v in stated)

    weighted = 0
    base_by_citation: dict[str, int] = {}
    for txn in matched_txns:
        on = india_tax.ist_date(getattr(txn, "timestamp_utc", None), as_of)
        rate, citation = _rate_on(rate_card.tcs_rate_bps, india_tax.TCS_GST_ECOMMERCE, on)
        value = _gross_cents(txn)          # refunds arrive negative and net off
        weighted += value * rate
        base_by_citation[citation] = base_by_citation.get(citation, 0) + value
    expected = max(round(weighted / 10_000), 0)

    diff = expected - actual
    if abs(diff) <= 100:
        return []
    citation = _cite(base_by_citation)
    net_value = sum(base_by_citation.values())
    return [FeeAuditFinding(
        category=FeeAuditCategory.TCS_COLLECTION_ERROR,
        severity=FeeAuditSeverity.WARNING,
        txn_id="batch",
        expected_cents=expected,
        actual_cents=actual,
        difference_cents=diff,
        description=(
            f"GST TCS ({citation}): net taxable value {net_value/100:,.2f} after "
            f"returns. Expected TCS {expected/100:,.2f}, reported {actual/100:,.2f}, "
            f"difference {diff/100:,.2f}."
        ),
        rule_basis="statutory",
        citation=citation,
    )]
