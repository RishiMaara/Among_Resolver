"""
Declared exchange rates: a foreign-currency payment converted into the
settlement's currency, exactly, with the rate kept as evidence.

A processor that settles foreign payments states the rate on the settlement
advice. With that rate declared (`USD=83.1250`: one USD is 83.1250 of the
settlement currency) a USD payment enters an INR settlement's pool as INR,
converted in Decimal and rounded half-up to the minor unit, carrying its
original amount, currency and the rate. Without a declared rate nothing is
converted and the payment stays out of the pool: amounts are integers with no
unit attached, and summing across currencies once cleared INR 300 from two
INR legs and a USD leg (candidate_filters.py).

Each converted payment can differ from the processor's own rounding by one
minor unit, which the solver's tolerance absorbs for a handful of foreign
members. A converted payment's fee and tax fields are in the foreign
currency, so they are set aside rather than audited as if they were rupees.
"""
from __future__ import annotations

import dataclasses
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from schema import NormalizedTxn

# ISO 4217 minor units where they are not 2.
_MINOR_UNITS = {"JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "ISK": 0, "UGX": 0,
                "BHD": 3, "KWD": 3, "OMR": 3, "JOD": 3, "TND": 3, "IQD": 3, "LYD": 3}
_CODE = re.compile(r"^[A-Z]{3}$")
_FEE_WORDS = ("fee", "tax", "gst", "tds", "tcs", "gross")


def minor_units(currency: str) -> int:
    return _MINOR_UNITS.get((currency or "").strip().upper(), 2)


def parse_rates(text: str) -> dict[str, Decimal]:
    """
    "USD=83.1250, EUR=90.40" -> {"USD": Decimal("83.1250"), "EUR": Decimal("90.40")}.
    Raises ValueError naming the part it could not read.
    """
    rates: dict[str, Decimal] = {}
    for part in (text or "").split(","):
        if not part.strip():
            continue
        code, sep, value = part.partition("=")
        code = code.strip().upper()
        try:
            rate = Decimal(value.strip())
        except InvalidOperation:
            rate = Decimal(0)
        if not sep or not _CODE.match(code) or not rate.is_finite() or rate <= 0:
            raise ValueError(f"fx_rates: could not read {part.strip()!r}; write CODE=rate, "
                             f"e.g. USD=83.1250, one per currency, comma-separated")
        rates[code] = rate
    return rates


def convert(t: NormalizedTxn, to_currency: str, rate: Decimal) -> NormalizedTxn:
    """The same payment, in `to_currency` at `rate`, with the evidence kept."""
    source_ccy = (t.currency or "").strip().upper()
    major = Decimal(t.amount_cents) / (Decimal(10) ** minor_units(source_ccy))
    target = (major * rate * (Decimal(10) ** minor_units(to_currency))).quantize(
        Decimal(1), rounding=ROUND_HALF_UP)
    extra = dict(t.extra or {})
    set_aside = {k: extra.pop(k) for k in list(extra)
                 if any(w in k.lower() for w in _FEE_WORDS)}
    extra.update(fx_from_currency=source_ccy, fx_rate=str(rate),
                 fx_original_amount_minor=t.amount_cents)
    if set_aside:
        extra["fx_foreign_fee_fields"] = set_aside
    return dataclasses.replace(t, amount_cents=int(target), currency=to_currency, extra=extra)
