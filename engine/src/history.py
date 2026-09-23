"""
A persistent record of each reconciliation run, for later comparison.

Bounded on purpose: the audit trail is excluded (it lives in the audit
store), the exception list is capped (one record reached 131 MB), and
filenames carry a sanitised batch id (untrusted input). Stored as files
under HISTORY_DIR, in the temp dir when that is read-only (serverless), or
in Redis when REDIS_URL / KV_URL is set so every instance shares it.
"""

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone

import audit

logger = logging.getLogger(__name__)

HISTORY_DIR = os.environ.get("HISTORY_DIR", "").strip() or os.path.join(
    os.path.dirname(__file__), "..", "data", "history"
)

# Where records go when HISTORY_DIR cannot be written.
_FALLBACK_DIR = os.path.join(tempfile.gettempdir(), "among_resolver_history")

# Redis keys, when a shared store is configured. The index is newest-first;
# the hash holds the records. Capped, because a demo anyone can reach must not
# be able to grow a shared store without bound.
_REDIS_INDEX = "history:index"
_REDIS_RECORDS = "history:records"
MAX_SHARED_RECORDS = 200

# Enough to read a run's exceptions and understand it; short of the point
# where one record can no longer be held in memory alongside 499 others.
MAX_RECORDED_EXCEPTIONS = 500


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value or "unknown")[:120] or "unknown"


def _shared_store():
    """The Redis client — but only when a URL was configured explicitly."""
    if not audit.redis_url():
        return None
    return audit._get_redis()


def _writable_dir() -> str:
    """
    HISTORY_DIR if a write there succeeds, else the temp-directory fallback.

    Decided by trying, not by reading permissions: a read-only mount can
    report writable mode bits, and the only answer that matters is whether a
    write succeeds.
    """
    for candidate in (HISTORY_DIR, _FALLBACK_DIR):
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, ".write_probe")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("")
            os.remove(probe)
        except OSError:
            continue
        if candidate != HISTORY_DIR:
            logger.warning(
                "History: %s is not writable; recording runs under %s, which "
                "is private to this machine. Set REDIS_URL to share history "
                "across instances.", HISTORY_DIR, candidate,
            )
        return candidate
    raise OSError("no writable directory for run history")


def _read_dirs() -> list[str]:
    """Every directory a record may have been written to, configured one first."""
    dirs = [HISTORY_DIR]
    if os.path.abspath(_FALLBACK_DIR) != os.path.abspath(HISTORY_DIR):
        dirs.append(_FALLBACK_DIR)
    return [d for d in dirs if os.path.isdir(d)]


def storage_status() -> dict:
    """Which backend history is using, for /health."""
    if _shared_store() is not None:
        return {"backend": "redis", "shared": True}
    try:
        where = _writable_dir()
    except OSError:
        return {"backend": "none", "shared": False}
    return {
        "backend": "files",
        "shared": False,
        "fallback": os.path.abspath(where) != os.path.abspath(HISTORY_DIR),
    }


def record_run(batch_id: str, inputs: dict, formatted_result: dict) -> str:
    """
    Save a record of this run and return where it went.

    The caller is expected to treat a failure here as non-fatal — the
    reconciliation result matters, this record does not.
    """
    # Microseconds, so two runs of the same batch in the same second — two
    # people trying the same sample at once — do not overwrite each other.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    name = f"run_{_safe_name(batch_id)}_{timestamp}.json"

    trail = formatted_result.get("audit_trail") or []
    slim = {k: v for k, v in formatted_result.items() if k != "audit_trail"}
    slim["audit_trail_entry_count"] = len(trail)
    slim["audit_trail_note"] = (
        f"{len(trail)} entries omitted from this record; retrieve them from "
        f"GET /audit/{batch_id}"
    )

    exceptions = slim.get("exceptions")
    if isinstance(exceptions, list) and len(exceptions) > MAX_RECORDED_EXCEPTIONS:
        slim["exceptions_total_count"] = len(exceptions)
        slim["exceptions_truncated"] = True
        slim["exceptions_note"] = (
            f"{len(exceptions)} exceptions were raised; the first "
            f"{MAX_RECORDED_EXCEPTIONS} are recorded here. This record is a "
            f"summary for comparison, not the authority — the full set is in "
            f"the result returned at the time and in the audit trail at "
            f"GET /audit/{batch_id}."
        )
        slim["exceptions"] = exceptions[:MAX_RECORDED_EXCEPTIONS]

    record = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": inputs,
        "result": slim,
    }

    shared = _shared_store()
    if shared is not None:
        try:
            pipe = shared.pipeline()
            pipe.hset(_REDIS_RECORDS, name, json.dumps(record, default=str))
            pipe.lpush(_REDIS_INDEX, name)
            pipe.execute()
            # Evict past the cap: trim the index, then drop what it named.
            evicted = shared.lrange(_REDIS_INDEX, MAX_SHARED_RECORDS, -1)
            if evicted:
                shared.ltrim(_REDIS_INDEX, 0, MAX_SHARED_RECORDS - 1)
                shared.hdel(_REDIS_RECORDS, *evicted)
            return f"redis:{name}"
        except Exception as exc:
            # Fall through to disk: a record on one machine beats no record.
            logger.warning("History: Redis write failed (%s); recording to disk.",
                           type(exc).__name__)

    filepath = os.path.join(_writable_dir(), name)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, default=str)

    return filepath


def _read(filepath: str) -> dict | None:
    try:
        with open(filepath, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        # A truncated or hand-edited file must not take down the listing.
        # Skipping one record is better than returning none of them.
        logger.debug("_read: best-effort step skipped (%s: %s)", type(exc).__name__, exc)
        return None


def _iter_records():
    """(name, record) pairs from whichever backend holds them."""
    shared = _shared_store()
    if shared is not None:
        try:
            names = shared.lrange(_REDIS_INDEX, 0, -1)
            for name in names:
                raw = shared.hget(_REDIS_RECORDS, name)
                try:
                    yield name, (json.loads(raw) if raw else None)
                except ValueError:
                    yield name, None
            return
        except Exception as exc:
            logger.warning("History: Redis read failed (%s); reading disk.",
                           type(exc).__name__)

    found: dict[str, str] = {}
    for directory in _read_dirs():
        for name in os.listdir(directory):
            if name.startswith("run_") and name.endswith(".json"):
                found.setdefault(name, os.path.join(directory, name))
    for name in sorted(found, reverse=True):
        yield name, _read(found[name])


def list_runs(limit: int = 200, batch_id: str | None = None) -> list[dict]:
    """
    Every recorded run, newest first, as summaries rather than full records.

    record_run has written one of these on every reconciliation since it was
    built, and nothing could read them back — 44 files sat in data/history
    with no endpoint and no screen. A record nobody can retrieve is not a
    record.

    Summaries only: the caller listing a hundred runs wants batch id, verdict,
    when and by whom, not a hundred full results. run_detail() returns one.
    """
    wanted = _safe_name(batch_id) if batch_id else None
    out: list[dict] = []
    for name, rec in _iter_records():
        if wanted and not name.startswith(f"run_{wanted}_"):
            continue
        if not rec:
            continue
        result = rec.get("result") or {}
        summary = result.get("summary") or {}
        out.append({
            "record": name,
            "timestamp_utc": rec.get("timestamp_utc"),
            "batch_id": summary.get("batch_id") or (rec.get("inputs") or {}).get("batch_id"),
            "reviewer": (rec.get("inputs") or {}).get("reviewer"),
            "status": (
                "cleared" if summary.get("cleared")
                else "withheld" if summary.get("ambiguous")
                else "unmatched"
            ),
            "cleared": summary.get("cleared"),
            "confidence": summary.get("confidence"),
            "matched_count": summary.get("matched_count"),
            "total_candidates": summary.get("total_candidates"),
            "tie_out_residual_cents": summary.get("tie_out_residual_cents"),
            "audit_trail_entry_count": result.get("audit_trail_entry_count"),
        })
        if len(out) >= limit:
            break
    return out


def run_detail(record: str) -> dict | None:
    """One full record by name. The name is sanitised before it touches a
    path — these come in over HTTP, and "../../etc/passwd" is a valid string."""
    safe = _safe_name(record)
    if not safe.startswith("run_") or not safe.endswith(".json"):
        return None
    shared = _shared_store()
    if shared is not None:
        try:
            raw = shared.hget(_REDIS_RECORDS, safe)
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.warning("History: Redis read failed (%s); reading disk.",
                           type(exc).__name__)
    for directory in _read_dirs():
        rec = _read(os.path.join(directory, safe))
        if rec is not None:
            return rec
    return None
