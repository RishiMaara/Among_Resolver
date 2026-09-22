"""
A budget on model calls, so a public demo can carry a real key.

Every call made while serving a web request is checked first:
  per visitor, per hour   MODEL_CALLS_PER_CLIENT_PER_HOUR   default 20
  per server, per day     MODEL_CALLS_PER_DAY               default 200
  size of what is sent    MODEL_MAX_PROMPT_CHARS            default 60,000
Counts live in Redis when configured, else in process. Over a limit the call
is skipped, the deterministic path answers, and the response says why.
Scripts outside a web request are not metered. Use a key without billing on
a public demo.
"""

from __future__ import annotations

import contextvars
import os
import threading
import time
from datetime import datetime, timezone

# Set per request by main.py's middleware. A dict rather than a value so a
# call made in a worker thread (FastAPI runs sync routes in a threadpool) can
# record what happened and the route can read it back.
REQUEST: contextvars.ContextVar[dict | None] = contextvars.ContextVar("model_request", default=None)

_local: dict[str, tuple[int, float]] = {}
_lock = threading.Lock()


def per_client_per_hour() -> int:
    return int(os.environ.get("MODEL_CALLS_PER_CLIENT_PER_HOUR", "20"))


def per_day() -> int:
    return int(os.environ.get("MODEL_CALLS_PER_DAY", "200"))


def max_prompt_chars() -> int:
    return int(os.environ.get("MODEL_MAX_PROMPT_CHARS", "60000"))


def begin_request(client: str) -> dict:
    """Called by the middleware: who is asking, and a place to record calls."""
    state = {"client": client or "unknown", "calls": 0, "skipped": ""}
    REQUEST.set(state)
    return state


def _store():
    import audit  # pylint: disable=import-outside-toplevel
    return audit._get_redis() if audit.redis_url() else None  # pylint: disable=protected-access


def _count(key: str, ttl_s: int, add: int) -> int:
    client = _store()
    if client is not None:
        try:
            n = int(client.incrby(key, add)) if add else int(client.get(key) or 0)
            if add and n == add:
                client.expire(key, ttl_s)
            return n
        except Exception:  # a store that fails mid-request falls back, it does not block
            pass
    with _lock:
        now = time.time()
        n, expires = _local.get(key, (0, now + ttl_s))
        if now > expires:
            n, expires = 0, now + ttl_s
        n += add
        _local[key] = (n, expires)
        return n


def _day_key() -> str:
    return "model:day:" + datetime.now(timezone.utc).strftime("%Y%m%d")


def _client_key(client: str) -> str:
    return f"model:client:{client}:" + datetime.now(timezone.utc).strftime("%Y%m%d%H")


def take(prompt_chars: int = 0) -> str | None:
    """
    Reserve one model call for the current request.

    Returns None when the call may go ahead, or the reason it may not. Outside
    a web request there is nothing to meter and every call is allowed.
    """
    state = REQUEST.get()
    if state is None:
        return None
    reason = None
    if prompt_chars > max_prompt_chars():
        reason = (f"the request would send {prompt_chars:,} characters to the model, "
                  f"over this server's limit of {max_prompt_chars():,}")
    elif _count(_day_key(), 90_000, 0) >= per_day():
        reason = f"this server's model budget for today ({per_day()} calls) is spent"
    elif _count(_client_key(state["client"]), 3_700, 0) >= per_client_per_hour():
        reason = (f"this visitor's model budget for the hour "
                  f"({per_client_per_hour()} calls) is spent")
    if reason:
        state["skipped"] = reason
        return reason
    _count(_day_key(), 90_000, 1)
    _count(_client_key(state["client"]), 3_700, 1)
    state["calls"] += 1
    return None


def report() -> dict:
    """What the model did for the current request, for the response body."""
    import llm_provider  # pylint: disable=import-outside-toplevel
    state = REQUEST.get() or {}
    return {
        "model": llm_provider.DEFAULT_MODEL if llm_provider.is_configured() else None,
        "model_calls": state.get("calls", 0),
        "skipped": state.get("skipped") or None,
    }


def status() -> dict:
    """Budget left, without spending any."""
    used = _count(_day_key(), 90_000, 0)
    return {"per_day": per_day(), "used_today": used, "left_today": max(per_day() - used, 0),
            "per_visitor_per_hour": per_client_per_hour(),
            "max_prompt_chars": max_prompt_chars()}
