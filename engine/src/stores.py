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

from typing import Any, Callable

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


# ── Process-local state ──────────────────────────────────────────────────
# What a stateful module keeps in this process when no shared store answers.
# Registered here so one place can say what this instance holds that others
# cannot see (/health), and tests can clear all of it at once.
_LOCAL: dict[str, Any] = {}


def local(name: str, factory: Callable[[], Any]) -> Any:
    """The named in-process fallback, created on first use and then shared."""
    if name not in _LOCAL:
        _LOCAL[name] = factory()
    return _LOCAL[name]


def process_local_state() -> dict[str, int]:
    """Entries each fallback holds now, for the ones holding any."""
    return {n: len(v) for n, v in sorted(_LOCAL.items()) if len(v)}


def clear_process_local() -> None:
    for v in _LOCAL.values():
        v.clear()
