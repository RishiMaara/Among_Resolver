"""
What a reviewer should know about foreign-currency payments in a run, as notes.

Advisory only and off the money path (application layer, like erp_sync): what
converts a payment is the rate the settlement advice declares (fx.py), never
this module. It says two things the result alone does not:

- which foreign payments were left out because no rate was declared for their
  currency, so a payout that includes them is withheld rather than summed
  across currencies, and the reviewer knows what to add;
- how far a declared rate sits from the European Central Bank's reference for
  that day, derived through EUR. Processors add a spread, so a gap of a few
  percent is normal; past FX_REFERENCE_MAX_GAP (default 3%) it is usually a
  typo or the wrong currency, which also stops those payments tying.

No network, a timeout, or FX_REFERENCE=0 means no reference note, never a
failed run.
"""
from __future__ import annotations

import csv
import io
import logging
import os
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache

import httpx

logger = logging.getLogger(__name__)

ECB_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.{codes}.EUR.SP00.A"


@lru_cache(maxsize=256)
def _ecb_per_eur(codes: str, day: date) -> dict[str, Decimal]:
    """Each currency's latest ECB rate per EUR on or before `day`."""
    resp = httpx.get(ECB_URL.format(codes=codes), timeout=3.0, params={
        "startPeriod": (day - timedelta(days=10)).isoformat(),
        "endPeriod": day.isoformat(), "format": "csvdata"})
    resp.raise_for_status()
    latest: dict[str, tuple[str, Decimal]] = {}
    for row in csv.DictReader(io.StringIO(resp.text)):
        cur, when = row.get("CURRENCY") or "", row.get("TIME_PERIOD") or ""
        try:
            value = Decimal(row.get("OBS_VALUE") or "")
        except InvalidOperation:
            continue
        if cur and when <= day.isoformat() and (cur not in latest or when > latest[cur][0]):
            latest[cur] = (when, value)
    return {cur: v for cur, (_, v) in latest.items()}


def reference_rate(currency: str, into: str, day: date) -> Decimal | None:
    """One `currency` in `into`, from the ECB's reference rates, or None."""
    currency, into = currency.upper(), into.upper()
    codes = "+".join(sorted({c for c in (currency, into) if c != "EUR"}))
    if not codes:
        return None
    try:
        per_eur = _ecb_per_eur(codes, day)
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("FX reference unavailable (%s); no note.", type(exc).__name__)
        return None
    per_eur["EUR"] = Decimal(1)
    if currency not in per_eur or into not in per_eur or not per_eur[currency]:
        return None
    return per_eur[into] / per_eur[currency]


def notes_for(batch, candidates: list) -> list[str]:
    """The notes a reviewer needs about foreign payments in this run."""
    home = (batch.currency or "").upper()
    declared = {k.upper(): v for k, v in (batch.fx_rates or {}).items()}
    foreign = Counter((t.currency or "").upper() for t in candidates
                      if getattr(t, "currency_stated", True) and (t.currency or "").upper() != home)
    notes = [
        f"{n} {cur} payment(s) were left out: no {cur} rate was declared, and amounts in "
        f"different currencies are never summed. If this payout includes them, enter the "
        f"settlement advice's rate as FX rates, e.g. {cur}=83.1250."
        for cur, n in sorted(foreign.items()) if cur and cur not in declared]
    if os.environ.get("FX_REFERENCE", "1").strip() == "0":
        return notes
    max_gap = Decimal(os.environ.get("FX_REFERENCE_MAX_GAP", "0.03"))
    day = batch.settled_at_utc.date()
    for cur, rate in sorted(declared.items()):
        ref = reference_rate(cur, home, day)
        if ref is None or not ref:
            continue
        gap = (Decimal(rate) - ref) / ref
        if abs(gap) > max_gap:
            notes.append(
                f"The declared {cur} rate {rate} is {abs(gap):.0%} "
                f"{'above' if gap > 0 else 'below'} the ECB reference of {ref:.4f} for "
                f"{day.isoformat()} (derived through EUR). Processor rates carry a spread "
                f"of a few percent; a gap this large is usually a typo or the wrong "
                f"currency, and it would stop the {cur} payments from tying.")
    return notes
