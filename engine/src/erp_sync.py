"""
ERP write-back: POSTs a balanced journal to ERP_JOURNAL_URL.

No real ERP has received one; the payload shape is a design claim. Every
outcome is distinct and only `posted` returns True:

    posted                  the endpoint accepted it (2xx)
    rejected_by_erp         it answered no; status and body kept
    unreachable             the POST failed; NOT posted
    no_target_configured    ERP_JOURNAL_URL is unset
    skipped_unbalanced      refused before sending

It once reported an unreachable ERP as posted (FAILURE_LOG).
"""

from __future__ import annotations

import json
import logging
import os

import requests

from cash_position import CashPosition

logger = logging.getLogger(__name__)

# Unset by default, deliberately. A default pointing at localhost made "the
# ERP is unreachable" indistinguishable from "no ERP was ever configured",
# and the old code treated both as success.
ERP_URL_ENV = "ERP_JOURNAL_URL"
ERP_TOKEN_ENV = "ERP_API_TOKEN"

# The stand-in used by the demo, named so it is obvious in a log line that
# this is not somebody's general ledger.
MOCK_ERP_URL = "http://localhost:9999/mock-erp/journal"

TIMEOUT_S = 5.0


def configured_url() -> str | None:
    url = os.environ.get(ERP_URL_ENV, "").strip()
    return url or None


def is_mock_target(url: str | None) -> bool:
    return url == MOCK_ERP_URL or (url or "").startswith(("http://localhost", "http://127.0.0.1"))


def build_payload(position: CashPosition) -> dict:
    """
    The journal as a standard ERP JSON document.

    Amounts are sent as decimal strings, not floats. The engine works in
    integer paise throughout and `round(cents / 100, 2)` reintroduces binary
    floating point at the one boundary where a cent must not move — 0.145
    does not round the way a reader expects, and a general ledger is the
    last place to discover that. `paise` carries the exact integer alongside
    it so a receiving system can reconcile without touching the decimal at
    all.
    """
    journal = position.journal
    if journal is None:
        raise ValueError("this cash position carries no journal to send")
    lines = []
    for line in journal.lines:
        if line.debit_cents <= 0 and line.credit_cents <= 0:
            continue
        lines.append({
            "account": line.account,
            "debit": f"{line.debit_cents // 100}.{line.debit_cents % 100:02d}",
            "credit": f"{line.credit_cents // 100}.{line.credit_cents % 100:02d}",
            "debit_paise": line.debit_cents,
            "credit_paise": line.credit_cents,
            "memo": line.memo,
        })
    return {
        "externalId": journal.entry_id,
        "date": journal.date_utc.isoformat(),
        "memo": journal.basis,
        "lines": lines,
    }


def push_to_erp(position: CashPosition, erp_url: str | None = None) -> bool:
    """
    Post the journal, and record honestly which of the five outcomes happened.

    Returns True ONLY when a target accepted it. Every other path sets a
    status naming the reason and returns False, because a caller that cannot
    distinguish "posted" from "nothing was reachable" will report the second
    as the first — which is the bug this function used to have.
    """
    batch = position.batch_id
    journal = position.journal

    if not journal:
        logger.info("ERP sync [%s]: no journal entry to push.", batch)
        return False

    if not journal.is_balanced or journal.status == "rejected":
        journal.status = "skipped_unbalanced"
        logger.warning(
            "ERP sync [%s]: journal is unbalanced or already rejected — refusing "
            "to post. An unbalanced journal is a corruption, not a posting.",
            batch,
        )
        return False

    url = erp_url or configured_url()
    payload = build_payload(position)

    if url is None:
        journal.status = "no_target_configured"
        logger.info(
            "ERP sync [%s]: %s is unset, so nothing was posted. The journal is "
            "built, balanced and available on the report; set %s to deliver it. "
            "Payload: %s",
            batch, ERP_URL_ENV, ERP_URL_ENV, json.dumps(payload),
        )
        return False

    headers = {"Content-Type": "application/json"}
    token = os.environ.get(ERP_TOKEN_ENV, "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT_S)
    except requests.RequestException as exc:
        # NOT success. This is the line the old version got wrong.
        journal.status = "unreachable"
        logger.error(
            "ERP sync [%s]: POST to %s failed (%s: %s). The journal was NOT "
            "posted. Payload retained: %s",
            batch, url, type(exc).__name__, exc, json.dumps(payload),
        )
        return False

    if response.status_code in (200, 201, 202):
        journal.status = "posted_to_mock" if is_mock_target(url) else "posted"
        logger.info(
            "ERP sync [%s]: journal accepted by %s (%d).%s",
            batch, url, response.status_code,
            " This is the local stand-in, not a general ledger."
            if is_mock_target(url) else "",
        )
        return True

    journal.status = "rejected_by_erp"
    body = (response.text or "")[:300]
    logger.error(
        "ERP sync [%s]: %s rejected the journal with %d: %s",
        batch, url, response.status_code, body,
    )
    return False
