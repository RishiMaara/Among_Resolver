"""
API authentication: one shared key, sent as `X-API-Key` or
`Authorization: Bearer ...`, compared in constant time.

Off unless API_KEY is set, so the demo runs without a secret; it warns at
startup and /health reports `auth: "disabled"`. The engine has no user
model, so a key authenticates a caller, not a person.
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

# Webhook deliveries prove themselves by HMAC-SHA256 over the raw body
# (webhook.py) and are refused there without it. Razorpay cannot send this
# deployment's API key, so the key check would 401 every genuine delivery.
# Only the receiving path; /webhooks/pending stays behind the key.
HMAC_AUTHENTICATED_PATHS = {"/webhooks/razorpay"}


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
