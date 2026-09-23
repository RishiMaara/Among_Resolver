"""
The shared stores, in one place: which Redis and which SQLite connection the
stateful modules use (settled ledger, open items, settlement cycle, history,
webhooks, chargebacks, calibration, model budget, model replay).

audit.py opens the connections, because the audit trail was the first thing
that needed them. Every other module asks here rather than reaching into
audit's private functions, so the policy is decided once: Redis only when
REDIS_URL / KV_URL says shared state is wanted, SQLite when the engine
directory is writable, and the caller's own in-process fallback otherwise.
"""
from __future__ import annotations

import audit


def redis_url() -> str:
    """The configured shared-store URL, or ""."""
    return audit.redis_url()


def shared_redis():
    """Redis when a URL is configured, else None."""
    return audit._get_redis() if audit.redis_url() else None  # pylint: disable=protected-access


def any_redis():
    """Any reachable Redis, including a local one found without a URL (audit's rule)."""
    return audit._get_redis()  # pylint: disable=protected-access


def durable_db():
    """The durable SQLite connection shared with the audit trail, or None."""
    return audit._get_db()  # pylint: disable=protected-access
