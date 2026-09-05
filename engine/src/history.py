"""
Persistent record of each reconciliation run, for later comparison.

Three things are deliberately bounded.

The audit trail is EXCLUDED from the record. A 50K run produces ~18,500
entries, so embedding it turns every history file into a multi-megabyte dump
of data that already lives in the audit store — and data/history is inside the
repo, so those files would end up committed. The entry count is kept instead,
and the trail itself stays reachable via GET /audit/{batch_id}.

The exception list is CAPPED, for the same reason and after it defeated it.
Bounding only the audit trail left the largest field unbounded: a run over a
pool of identical payments reports nearly all of them as exceptions, and one
such record on disk here was 131 MB, 95 MB of it exceptions. That is not only
disk — list_runs() parses whole files to build summaries, so a handful of them
turned a listing into seconds of work and gigabytes of transient memory. The
tail carries no information the head does not: past the cap the exceptions of
an unresolved fungible pool are the same sentence with a different id.

Filenames carry a sanitised batch id. Batch ids arrive from uploaded form
fields, so they are untrusted input: "../../etc/passwd" is a valid string and
must not be interpolated into a path unescaped.
"""

import json
import os
import re
from datetime import datetime, timezone

HISTORY_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "history")

# Enough to read a run's exceptions and understand it; short of the point
# where one record can no longer be held in memory alongside 499 others.
MAX_RECORDED_EXCEPTIONS = 500


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value or "unknown")[:120] or "unknown"


def record_run(batch_id: str, inputs: dict, formatted_result: dict) -> str:
    """
    Save a record of this run to data/history and return the file path.

    The caller is expected to treat a failure here as non-fatal — the
    reconciliation result matters, this record does not.
    """
    os.makedirs(HISTORY_DIR, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(
        HISTORY_DIR, f"run_{_safe_name(batch_id)}_{timestamp}.json"
    )

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

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, default=str)

    return filepath

def _read(filepath: str) -> dict | None:
    try:
        with open(filepath, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # A truncated or hand-edited file must not take down the listing.
        # Skipping one record is better than returning none of them.
        return None


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
    if not os.path.isdir(HISTORY_DIR):
        return []

    wanted = _safe_name(batch_id) if batch_id else None
    out: list[dict] = []
    for name in sorted(os.listdir(HISTORY_DIR), reverse=True):
        if not name.startswith("run_") or not name.endswith(".json"):
            continue
        if wanted and not name.startswith(f"run_{wanted}_"):
            continue
        rec = _read(os.path.join(HISTORY_DIR, name))
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
    """One full record by filename. The name is sanitised before it touches a
    path — these come in over HTTP, and "../../etc/passwd" is a valid string."""
    safe = _safe_name(record)
    if not safe.startswith("run_") or not safe.endswith(".json"):
        return None
    return _read(os.path.join(HISTORY_DIR, safe))
