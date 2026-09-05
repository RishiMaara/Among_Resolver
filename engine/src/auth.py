"""
API authentication.

WHY THIS EXISTS
---------------
Every endpoint was open. Settlement amounts, matched transaction ids, audit
trails and compliance findings — including which counterparties were blocked
and under which rule — were readable by anything that could reach the port.
For a tool whose whole subject is money movement, that is the wrong default
even on a laptop.

THE SHAPE OF IT
---------------
A shared API key, sent as `X-API-Key` or `Authorization: Bearer …`. Not OAuth,
not per-user sessions: this engine has no user model and inventing one to look
thorough would be worse than a key that is honestly a key. What it does buy is
that a deployment can be locked, and that the decision to run it open becomes
explicit rather than accidental.

DISABLED BY DEFAULT, AND LOUD ABOUT IT
--------------------------------------
With no API_KEY set the engine runs open, because a reconciliation demo that
refuses to start until you export a secret is a worse first experience than
one that works and warns. But it warns at every startup, and /health reports
`auth: "disabled"` so the state is visible to whoever is looking rather than
only to whoever configured it.

The comparison is constant-time. A key check that returns faster for a wrong
first character leaks the key one character at a time, and `==` on a str does
exactly that.
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import Header, HTTPException, status

logger = logging.getLogger(__name__)

# Paths that must stay reachable without a key: a load balancer cannot present
# one, and a locked-out operator still needs to see why.
PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


def configured_key() -> str | None:
    key = os.environ.get("API_KEY", "").strip()
    return key or None


def is_enabled() -> bool:
    return configured_key() is not None


def status_label() -> str:
    return "enabled" if is_enabled() else "disabled"


def warn_if_open() -> None:
    """Called once at startup. Silence here would be the actual hazard."""
    if not is_enabled():
        logger.warning(
            "API authentication is DISABLED. Every endpoint — settlement "
            "results, audit trails, compliance findings — is readable by "
            "anything that can reach this port. Acceptable for a local demo; "
            "set API_KEY before exposing this to a network."
        )


def _present(x_api_key: str | None, authorization: str | None) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def require_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> None:
    """
    FastAPI dependency. A no-op when no key is configured.

    Raises 401 rather than 403 on a missing or wrong key: the caller has not
    been refused permission, they have failed to identify themselves, and the
    WWW-Authenticate header tells them how.
    """
    expected = configured_key()
    if expected is None:
        return

    supplied = _present(x_api_key, authorization)
    # hmac.compare_digest, not ==. String equality short-circuits on the first
    # differing byte, so its timing reveals how much of a guess was right.
    if supplied is None or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Missing or invalid API key. Send it as the X-API-Key header "
                "or as 'Authorization: Bearer <key>'."
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
