"""
Read settlements and their members straight from Razorpay, instead of from a
CSV somebody exported by hand.

WHY THIS IS THE RIGHT FEED
--------------------------
Every corpus this engine has been measured on is a compromise. ReconRiver is
synthetic. A hand-exported CSV is real but arrives with whatever columns the
merchant's export happened to use, and with whatever the person exporting it
remembered to include.

The settlement recon report has none of those problems, because it carries
`settlement_id` on every line. That is not a reference to be fuzzy-matched —
it is the settlement naming its own members, which is the strongest anchor
linkage can be given, and the condition under which this engine scores
94.59% rather than 21.62%. The reconciliation is close to trivial in that condition,
and saying so is more useful than pretending otherwise: the value here is not
that the engine can solve it, it is that the engine can PROVE the arithmetic
ties out to the paisa and show which line did what.

WHAT IT MAPS
------------
    recon item                      NormalizedTxn
    ----------------------------    ---------------------------------
    entity_id                       source_txn_id
    settlement_id                   ref_id_canonical   <- the anchor
    credit - debit                  amount_cents       <- signed, net
    currency                        currency
    settled_at (unix seconds)       timestamp_utc
    type (payment/refund/...)       extra["status"], extra["type"]
    fee, tax                        extra, and summed into declared deductions

Amounts arrive in subunits — paise — as integers. This engine's entire
arithmetic is integer paise. There is no float in this path and no rounding
step where one could enter, which is the single most valuable property of
reading the API rather than parsing "₹1,234.56" out of a spreadsheet cell.

A REFUND IS A NEGATIVE, NOT AN ABSENCE
--------------------------------------
`type` is one of payment, refund, transfer, adjustment. A refund reduces the
settlement, so it enters the pool as a negative amount via `credit - debit`,
which is what orchestrator.py's forced-anchored-negatives handling already
expects. Dropping refunds instead would make the arithmetic stop tying out.

NOT VERIFIED AGAINST A LIVE ACCOUNT
------------------------------------
This was written against Razorpay's published API reference, and its tests run
against recorded response shapes rather than the real service, because no test
credentials were available when it was written. The field names, the endpoint
paths and the subunit convention are documented; the assumption that
`settlement.amount == sum(credit) - sum(debit)` over that settlement's recon
items is inferred from those definitions, not observed. `verify_tie_out()`
exists to check exactly that against a real account, and prints the discrepancy
rather than asserting, because the first run against live data is a
measurement, not a test.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import requests
from dataclasses import dataclass
from datetime import datetime, timezone

from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence

logger = logging.getLogger(__name__)

API_ROOT = "https://api.razorpay.com/v1"

# Recon lines that did not settle are not members of a settlement. Same
# reasoning as NON_SETTLING_STATUSES in orchestrator.py: money that did not
# move cannot compose a payout, and leaving it in the pool invents subsets
# that cannot have happened.
SETTLING_TYPES = {"payment", "refund", "transfer", "adjustment"}


class RazorpayError(RuntimeError):
    """A failure talking to Razorpay, with the cause kept readable."""


@dataclass
class RazorpayCredentials:
    key_id: str
    key_secret: str

    @property
    def is_test_mode(self) -> bool:
        return self.key_id.startswith("rzp_test_")

    def auth_header(self) -> str:
        raw = f"{self.key_id}:{self.key_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()


def credentials_from_env() -> RazorpayCredentials | None:
    """
    RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET, or nothing.

    Returns None rather than raising so the engine runs identically without
    them — the same contract the LLM agents have. Nothing about the
    reconciliation core depends on this module being configured.
    """
    key_id = os.environ.get("RAZORPAY_KEY_ID", "").strip()
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
    if not key_id or not key_secret:
        return None
    if not key_id.startswith("rzp_"):
        logger.warning(
            "RAZORPAY_KEY_ID does not start with 'rzp_' (%s...). That is not the "
            "shape Razorpay issues; check it is the Key Id and not the secret.",
            key_id[:6],
        )
    return RazorpayCredentials(key_id=key_id, key_secret=key_secret)


def _get(path: str, params: dict, creds: RazorpayCredentials, timeout: float = 30.0) -> dict:
    url = f"{API_ROOT}{path}"

    if not url.startswith(API_ROOT + "/") and url != API_ROOT:
        raise RazorpayError(f"Refusing to open a URL outside {API_ROOT}: {url}")

    try:
        response = requests.get(
            url,
            params=params,
            auth=(creds.key_id, creds.key_secret),
            headers={"Accept": "application/json"},
            timeout=timeout
        )
        
        if response.status_code == 401:
            raise RazorpayError(
                "Razorpay rejected the credentials (401). Check RAZORPAY_KEY_ID "
                "and RAZORPAY_KEY_SECRET, and that they are from the same mode "
                "— a test Key Id with a live secret fails exactly like this."
            )
            
        response.raise_for_status()
        return response.json()
        
    except requests.exceptions.HTTPError as e:
        raise RazorpayError(f"Razorpay returned {e.response.status_code} for {path}: {e.response.text}") from e
    except requests.exceptions.RequestException as e:
        raise RazorpayError(f"Could not reach Razorpay ({str(e)}).") from e


def fetch_settlements(creds: RazorpayCredentials, *, count: int = 10,
                      skip: int = 0, frm: int | None = None,
                      to: int | None = None) -> list[dict]:
    """Settlement payouts, newest first. `count` is capped at 100 by the API."""
    params: dict = {"count": max(1, min(count, 100)), "skip": skip}
    if frm is not None:
        params["from"] = frm
    if to is not None:
        params["to"] = to
    return _get("/settlements", params, creds).get("items", [])


def fetch_recon(creds: RazorpayCredentials, year: int, month: int,
                day: int | None = None, *, count: int = 1000,
                skip: int = 0) -> list[dict]:
    """
    Every settled line for a period, each carrying the settlement it belongs to.

    Paginates to the end rather than returning the first page: a partial pool
    would make the arithmetic fail to tie out and look like a reconciliation
    failure rather than a truncated fetch, which is a bad way to lose an hour.
    """
    items: list[dict] = []
    page_size = max(1, min(count, 1000))
    while True:
        params: dict = {"year": year, "month": f"{month:02d}",
                        "count": page_size, "skip": skip}
        if day is not None:
            params["day"] = day
        page = _get("/settlements/recon/combined", params, creds).get("items", [])
        items += page
        if len(page) < page_size:
            return items
        skip += len(page)


def recon_item_to_txn(item: dict) -> NormalizedTxn | None:
    """
    One recon line as a candidate the solver can use, or None if it is not one.

    `credit - debit` rather than `amount`: amount is the gross movement, while
    credit is what actually reached the account after fee and tax. A settlement
    pays out the net, so the net is what has to sum to it. A refund carries the
    value in `debit` and therefore arrives negative, which is what the
    orchestrator's refund handling expects.
    """
    entity_id = str(item.get("entity_id") or "").strip()
    if not entity_id:
        return None

    settlement_id = str(item.get("settlement_id") or "").strip()
    credit = int(item.get("credit") or 0)
    debit = int(item.get("debit") or 0)
    net = credit - debit
    if net == 0:
        # Nothing moved on this line; it cannot be a member and including it
        # only gives the solver a free variable that changes no sum.
        return None

    settled_at = item.get("settled_at")
    if settled_at:
        # Unix seconds. Converted here rather than handed to the ingestion
        # timestamp parser, which does not accept epochs — a documented gap
        # that would otherwise drop every row on this path.
        ts = datetime.fromtimestamp(int(settled_at), tz=timezone.utc)
        tz_conf = TzConfidence.HIGH
    else:
        ts = datetime.now(timezone.utc)
        tz_conf = TzConfidence.LOW

    return NormalizedTxn(
        source=SourceType.GATEWAY,
        source_txn_id=entity_id,
        # The settlement names its own members. This is the anchor.
        ref_id_canonical=settlement_id.upper().replace("_", ""),
        amount_cents=net,
        currency=str(item.get("currency") or "INR").upper(),
        timestamp_utc=ts,
        tz_confidence=tz_conf,
        currency_stated=bool(item.get("currency")),
        memo_raw=" ".join(str(item.get(k) or "") for k in
                          ("type", "method", "order_id", "payment_id")).strip(),
        extra={
            "status": "settled" if item.get("settled") else "unsettled",
            "type": str(item.get("type") or ""),
            "settlement_id": settlement_id,
            "fee": int(item.get("fee") or 0),
            "tax": int(item.get("tax") or 0),
            "gross_amount": int(item.get("amount") or 0),
        },
    )


def settlement_to_batch(settlement: dict, *, member_source: SourceType = SourceType.GATEWAY
                        ) -> SettlementBatch:
    """
    A Razorpay settlement as the batch to reconcile.

    member_source is declared as GATEWAY because it is known here rather than
    guessed: these members came from the recon report for this settlement.
    Declaring it is what moves benchmark auto-clear from 62% to 75.33%, and it
    is the difference between the engine ruling out a mirror record and being
    unable to.
    """
    created = settlement.get("created_at")
    settled_at = (datetime.fromtimestamp(int(created), tz=timezone.utc)
                  if created else datetime.now(timezone.utc))
    return SettlementBatch(
        batch_id=str(settlement.get("id") or "").strip(),
        net_amount_cents=int(settlement.get("amount") or 0),
        currency=str(settlement.get("currency") or "INR").upper(),
        settled_at_utc=settled_at,
        source=SourceType.BANK,
        member_source=member_source,
        # fees and tax on the settlement entity are the payout's own charges.
        # They are stated by the API, so the target is a fact rather than a
        # rate-card estimate, which is the difference between fee_basis
        # "declared" and "estimated" in the report.
        declared_deductions_cents=(int(settlement.get("fees") or 0)
                                   + int(settlement.get("tax") or 0)) or None,
    )


def members_of(settlement_id: str, recon_items: list[dict]) -> list[dict]:
    """Recon lines the API says belong to this settlement."""
    return [i for i in recon_items
            if str(i.get("settlement_id") or "").strip() == settlement_id]


def verify_tie_out(settlement: dict, recon_items: list[dict]) -> dict:
    """
    Does sum(credit - debit) over this settlement's lines equal its amount?

    This is the assumption the whole mapping rests on and it has NOT been
    checked against a live account. Run it before trusting a demo to it: if
    the residual is non-zero the mapping needs a correction, and finding that
    out from this function is much better than finding it out from a
    reconciliation that mysteriously will not clear.
    """
    sid = str(settlement.get("id") or "")
    members = members_of(sid, recon_items)
    net = sum(int(i.get("credit") or 0) - int(i.get("debit") or 0) for i in members)
    declared = int(settlement.get("amount") or 0)
    return {
        "settlement_id": sid,
        "members": len(members),
        "sum_of_net_paise": net,
        "settlement_amount_paise": declared,
        "residual_paise": net - declared,
        "ties_out": net == declared,
    }
