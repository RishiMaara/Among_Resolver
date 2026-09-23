"""
Uploaded files: a size cap enforced while reading, and one ingest path.

The single upload and the queue used to carry their own copies of the
parse-and-normalise step, and they had drifted: the queue gave a 500 on an
unreadable file and dropped the timezone and currency notes. Both now call
ingest_upload.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

import audit
import file_agent
from ingestion import normalize_batch_with_report
from schema import NormalizedTxn, SourceType, TzConfidence

logger = logging.getLogger(__name__)

# Upload ceiling (MAX_UPLOAD_MB, default 64 MB; the 50K corpus is about
# 5 MB), enforced while reading in chunks so an oversized file fails with a
# message rather than an out-of-memory crash.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "64")) * 1024 * 1024

# Chunked, because the point is to STOP before the memory is spent. Reading it
# all and then checking len() detects the problem after paying for it.
_UPLOAD_CHUNK = 1024 * 1024


async def read_upload_capped(upload_file, *, limit: int = MAX_UPLOAD_BYTES) -> bytes:
    """
    Read an upload, refusing anything over `limit` before it is buffered.

    Raises 413 rather than 400: the request is well-formed, it is the size
    that is unacceptable, and a client should be able to tell those apart to
    know whether retrying a smaller file is worth it.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload_file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            name = getattr(upload_file, "filename", None) or "upload"
            raise HTTPException(
                status_code=413,
                detail=(
                    f"{name} is larger than the {limit // (1024 * 1024)} MB "
                    f"upload limit. Split the export, or raise MAX_UPLOAD_MB "
                    f"if this engine is being run against a genuinely larger "
                    f"feed."
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)


def ingest(content: bytes, filename: str, source_type: SourceType, *, batch_id: str,
           notes: list[str], scan_text: str = "") -> list[NormalizedTxn]:
    """
    Parse one file, map its headers, normalise its rows. Appends to `notes`
    what the reviewer must know: header choices, dropped rows, rows with no
    currency (read as INR), rows with an unknown timezone (assumed UTC).
    Raises file_agent.FileRejected when a required field is missing.
    """
    header_warnings: list[str] = []
    rows = file_agent.parse_file_content(content, filename, header_warnings, scan_text=scan_text)
    notes.extend(f"{filename or 'file'}: {w}" for w in header_warnings)
    audit.log_decision(
        batch_id=batch_id, agent="file_agent",
        detail=(f"{filename} ({source_type.value}) · {len(rows)} row(s) parsed and "
                f"headers mapped to the schema. No required field absent — file accepted."),
    )
    if not rows:
        return []
    report = normalize_batch_with_report(rows, source_type)
    audit.log_decision(
        batch_id=batch_id, agent="ingestion",
        detail=(f"{source_type.value}: {len(report.normalized)} of {report.total_input} "
                f"row(s) normalized — amounts to integer paise, timestamps to UTC, "
                f"references canonicalized. {report.drop_count} dropped."),
    )
    if report.drop_count:
        sample = ", ".join(f"{d.txn_id or f'row {d.record_index}'} ({d.reason})"
                           for d in report.dropped[:3])
        notes.append(
            f"{source_type.value}: {report.drop_count} of {report.total_input} rows "
            f"from '{filename}' could not be normalised ({report.drop_rate:.1%}). "
            f"First failures: {sample}")
    unstated = sum(1 for t in report.normalized if not t.currency_stated)
    if unstated:
        notes.append(
            f"{source_type.value}: {unstated} row(s) carry no currency column and "
            f"were read as INR. If this feed is not INR, its amounts are being "
            f"compared against a settlement in a different currency and the "
            f"currency guard cannot see it — add a currency column to be sure.")
    # An unknown zone defaulted to UTC is a 5.5-hour error on an Indian feed,
    # enough to move a payment out of its settlement window.
    low = sum(1 for t in report.normalized if t.tz_confidence is TzConfidence.LOW)
    if low:
        notes.append(
            f"{source_type.value}: {low} row(s) have an UNKNOWN source timezone and "
            f"were assumed UTC. If the feed is not UTC these are off by the zone "
            f"offset and may fall outside the settlement window — verify before "
            f"relying on any match involving them.")
        logger.warning("Ingestion: %d %s row(s) have LOW timezone confidence; matches "
                       "involving them are not trustworthy without confirming the "
                       "source zone.", low, source_type.value)
    return report.normalized


async def ingest_upload(upload: Optional[UploadFile], source_type: SourceType, *,
                        batch_id: str, notes: list[str],
                        scan_text: str = "") -> list[NormalizedTxn]:
    """
    Read and ingest one uploaded file (none, or empty, gives nothing).
    A refusal is a 422 carrying which field is missing; content that cannot
    be parsed at all is a 400, never a 500.
    """
    if not upload:
        return []
    content = await read_upload_capped(upload)
    if not content:
        return []
    name = upload.filename or ""
    try:
        return await run_in_threadpool(ingest, content, name, source_type,
                                       batch_id=batch_id, notes=notes, scan_text=scan_text)
    except file_agent.FileRejected as e:
        audit.log_decision(batch_id=batch_id, agent="file_agent",
                           detail=f"REJECTED {name} ({source_type.value}): {e}")
        raise HTTPException(status_code=422, detail={
            "message": str(e), "source": source_type.value, "filename": name,
            "rejected": True, **e.to_dict(),
        }) from e
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to parse {source_type.value} file '{name}': {e}",
        ) from e
