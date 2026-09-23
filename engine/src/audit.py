"""
Audit trail: every automated decision, which agent made it, and why.

STORAGE, first available wins (storage_status() reports which):
  1. Redis, when configured - shared across processes and instances.
  2. SQLite (WAL) at data/audit.sqlite3 or AUDIT_DB_PATH - durable, one host.
  3. JSON Lines, one file per batch, in the temp dir - read-only filesystems.
  4. In-memory - last resort, process-local, warns on first use.

TAMPER-EVIDENT
Each batch's trail is a hash chain: an entry stores prev_hash and the SHA-256
of its own content with that link. verify_chain() names the first entry that
breaks; every reconciliation returns the head hash (audit_head) as a receipt,
so a truncated tail is detectable too. Appends are serialised (BEGIN
IMMEDIATE, a Lua compare-and-set, or the process lock). Evident, not proof:
whoever can write the store can rewrite a chain, but not to match a receipt
someone else holds.
"""

from __future__ import annotations

from typing import Any, TextIO

import hashlib
import json
import logging
import os
import sqlite3
import time
import tempfile
import atexit
import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

GENESIS = "0" * 64
_CHAIN_FIELDS = ("timestamp_utc", "batch_id", "agent", "detail", "prev_hash")


def entry_hash(entry: dict) -> str:
    """SHA-256 over the entry's content and its link, canonically serialised."""
    body = {k: entry.get(k) for k in _CHAIN_FIELDS}
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _seal(entry: dict, prev_hash: str) -> dict:
    entry["prev_hash"] = prev_hash or GENESIS
    entry["hash"] = entry_hash(entry)
    return entry


# Compare-and-set append: extend the chain only if its head is still the
# one this writer hashed against. Redis has no SHA-256 in Lua, so the hash
# is computed in Python and the script only guards the link.
_REDIS_APPEND = """
local head = redis.call('GET', KEYS[2])
if not head then head = ARGV[4] end
if head ~= ARGV[1] then return 0 end
redis.call('RPUSH', KEYS[1], ARGV[2])
redis.call('SET', KEYS[2], ARGV[3])
return 1
"""

# ---------------------------------------------------------------------------
# Redis backend
# ---------------------------------------------------------------------------

_REDIS_CLIENT = None   # None = not tried; False = tried and unavailable; else = client


# Environment variables that can carry a full connection URL. REDIS_URL is
# the conventional name; KV_URL is what Vercel's Upstash integration injects,
# so a project that connected the integration works without renaming
# anything.
REDIS_URL_ENV_VARS = ("REDIS_URL", "KV_URL")

# How long a remote Redis gets before this process stops asking. Only applies
# when a URL is configured: that is an explicit statement that shared state is
# wanted, so one failed ping on a cold start must not silently downgrade a
# serverless instance to private state for its whole lifetime — which is the
# exact drift a shared store exists to prevent.
_REDIS_RETRY_AFTER_S = 30.0
_REDIS_RETRY_AT = 0.0


def redis_url() -> str:
    for var in REDIS_URL_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return ""


def _get_redis():
    global _REDIS_CLIENT, _REDIS_RETRY_AT
    url = redis_url()
    if _REDIS_CLIENT is False and url and time.monotonic() >= _REDIS_RETRY_AT:
        _REDIS_CLIENT = None  # a configured store earns another attempt
    if _REDIS_CLIENT is None:
        try:
            import redis
            if url:
                # Hosted Redis (Upstash over TLS, say): the connect includes a
                # TLS handshake to another region, which does not fit in the
                # quarter-second budget below. Three seconds is generous for
                # that and still short enough to fail visibly.
                client = redis.Redis.from_url(
                    url,
                    decode_responses=True,
                    socket_connect_timeout=3.0,
                    socket_timeout=3.0,
                    retry_on_timeout=True,
                    health_check_interval=30,
                )
                where = "the configured URL"
            else:
                # Short timeouts, because this runs on the first audit write
                # of the process and BLOCKS it. With the library defaults,
                # probing a Redis that is not running cost 4.089 SECONDS of
                # socket.connect on Windows (two attempts at ~2s each) — paid
                # by the first reconciliation of every process start, and
                # easily mistaken for a slow engine. A local Redis either
                # answers in milliseconds or is not there.
                client = redis.Redis(
                    host=os.environ.get("REDIS_HOST", "localhost"),
                    port=int(os.environ.get("REDIS_PORT", "6379")),
                    decode_responses=True,
                    socket_connect_timeout=0.25,
                    socket_timeout=0.25,
                    retry_on_timeout=False,
                )
                where = "localhost"
            client.ping()
            _REDIS_CLIENT = client
            # Never log the URL itself: it carries the password.
            logger.info("Audit: Redis backend available at %s.", where)
        except Exception as exc:
            _REDIS_CLIENT = False  # sentinel: tried, unavailable
            _REDIS_RETRY_AT = time.monotonic() + _REDIS_RETRY_AFTER_S
            log = logger.warning if url else logger.info
            log(
                "Audit: Redis unavailable (%s). Falling back to a local trail%s.",
                type(exc).__name__,
                "; will retry in 30s because a URL is configured" if url else "",
            )
    return _REDIS_CLIENT if _REDIS_CLIENT is not False else None


# ---------------------------------------------------------------------------
# File-based backend
# ---------------------------------------------------------------------------

# ONE FILE PER BATCH, not one file for everything.
#
# The first version of this backend appended every entry from every run to a
# single fixed-name file. Measured after only a few runs: 12 MB, 60,289 lines,
# and 4.59 SECONDS to read — because get_audit_trail scanned the whole file to
# filter by batch_id, and _format_report calls it on every API response. The
# cost grew with every reconciliation ever performed on the host, so the demo
# got slower the more it was used.
#
# Splitting by batch makes a read O(that batch's entries) instead of O(all
# history), and lets clear_trail delete a file rather than rewrite one.
_AUDIT_DIR: Path | None = None
_AUDIT_FILE_LOCK = threading.RLock()  # guards the handle cache and writes
_FILE_WARNED = False

# Open handles, keyed by batch id.
#
# Re-opening the file per entry cost 0.40 ms — trivial alone, but a 50K run
# logs ~18,500 entries, so it was ~7.5 s of pure open/close syscalls per run.
# Handles are cached and closed on eviction; the cache is bounded because a
# long-lived server would otherwise hold one descriptor per batch forever.
_HANDLES: "OrderedDict[str, TextIO]" = OrderedDict()
MAX_OPEN_HANDLES = 8


def _safe_name(batch_id: str) -> str:
    """Batch ids reach us from uploaded files, so they are untrusted input and
    must never be interpolated into a path unescaped — "../../etc/passwd" is a
    valid string. Everything outside [A-Za-z0-9._-] is replaced."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", batch_id or "unknown")
    return cleaned[:120] or "unknown"


def _get_audit_dir() -> Path:
    global _AUDIT_DIR, _FILE_WARNED
    if _AUDIT_DIR is None:
        user = os.environ.get("USER") or os.environ.get("USERNAME") or "app"
        _AUDIT_DIR = Path(tempfile.gettempdir()) / f"among_resolver_audit_{user}"
        _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    if not _FILE_WARNED:
        _FILE_WARNED = True
        logger.info(
            "Audit: using file-based trail under %s (one file per batch). "
            "Shared across workers on this host, but does not survive a host "
            "reboot or temp wipe. Configure Redis for a durable audit store.",
            _AUDIT_DIR,
        )
    return _AUDIT_DIR


def _batch_path(batch_id: str) -> Path:
    return _get_audit_dir() / f"{_safe_name(batch_id)}.jsonl"


def _get_handle(batch_id: str):
    """Return a cached append handle for this batch, opening one if needed."""
    fh = _HANDLES.get(batch_id)
    if fh is not None and not fh.closed:
        _HANDLES.move_to_end(batch_id)
        return fh

    while len(_HANDLES) >= MAX_OPEN_HANDLES:
        _, old = _HANDLES.popitem(last=False)
        try:
            old.close()
        except Exception as exc:
            logger.debug("_get_handle: best-effort step skipped (%s: %s)", type(exc).__name__, exc)

    fh = _batch_path(batch_id).open("a", encoding="utf-8")
    _HANDLES[batch_id] = fh
    return fh


def _close_handle(batch_id: str) -> None:
    fh = _HANDLES.pop(batch_id, None)
    if fh is not None:
        try:
            fh.close()
        except Exception as exc:
            logger.debug("_close_handle: best-effort step skipped (%s: %s)", type(exc).__name__, exc)


def close_all() -> None:
    """Close every cached handle. Registered atexit so a shutdown does not
    leave the last entries sitting in an OS buffer."""
    with _AUDIT_FILE_LOCK:
        for _, fh in list(_HANDLES.items()):
            try:
                fh.close()
            except Exception as exc:
                logger.debug("close_all: best-effort step skipped (%s: %s)", type(exc).__name__, exc)
        _HANDLES.clear()


atexit.register(close_all)


_FILE_HEADS: dict[str, str] = {}


def _file_write(entry: dict) -> None:
    """Append one JSON line to this batch's file, thread-safe in-process."""
    with _AUDIT_FILE_LOCK:
        bid = entry.get("batch_id", "unknown")
        if bid not in _FILE_HEADS:
            prior = _file_read(bid)
            _FILE_HEADS[bid] = (prior[-1].get("hash") if prior else None) or GENESIS
        _seal(entry, _FILE_HEADS[bid])
        line = json.dumps(entry, default=str) + "\n"
        fh = _get_handle(bid)
        fh.write(line)
        # Flushed per entry so another worker reading the file sees it
        # immediately. This is the cross-process visibility guarantee the whole
        # file backend exists for; buffering would break it.
        fh.flush()
        _FILE_HEADS[bid] = entry["hash"]


def _file_read(batch_id: str) -> list[dict]:
    """Read one batch's file. O(entries for THIS batch)."""
    path = _batch_path(batch_id)
    if not path.exists():
        return []

    # Flush our own pending writes first, or a read-after-write in the same
    # process can miss the most recent entries.
    with _AUDIT_FILE_LOCK:
        fh = _HANDLES.get(batch_id)
        if fh is not None and not fh.closed:
            fh.flush()

    results: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    # Partial write from a crashed process — skip it.
                    logger.debug("Audit file: skipped corrupt line in %s", path)
    except OSError as exc:
        logger.warning("Audit: could not read file %s: %s", path, exc)
    return results


# ---------------------------------------------------------------------------
# In-memory backend (last resort)
# ---------------------------------------------------------------------------

_FALLBACK_LOG: list[dict] = []
_FALLBACK_WARNED = False   # warn once, not on every write


def _memory_write(entry: dict) -> None:
    global _FALLBACK_WARNED
    if not _FALLBACK_WARNED:
        _FALLBACK_WARNED = True
        logger.warning(
            "Audit: falling back to in-memory trail (Redis unavailable AND "
            "file backend failed). This trail is PROCESS-LOCAL — entries "
            "written by one gunicorn/uvicorn worker are invisible to others, "
            "and all entries are lost on process exit. For a shared trail "
            "configure Redis, or ensure the temp directory is writable."
        )
    prior = _memory_read(entry.get("batch_id", "unknown"))
    _seal(entry, (prior[-1].get("hash") if prior else None) or GENESIS)
    _FALLBACK_LOG.append(entry)


def _memory_read(batch_id: str) -> list[dict]:
    return [e for e in _FALLBACK_LOG if e.get("batch_id") == batch_id]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ── Durable local store ──
# SQLite between Redis and the lossy tiers: durable, transactional, no server.
# Defaults inside the engine directory, not the temp dir, so a reboot cannot
# erase the trail.
_DB: "sqlite3.Connection | None" = None
_DB_WARNED = False


def _db_path() -> Path:
    configured = os.environ.get("AUDIT_DB_PATH", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / "data" / "audit.sqlite3"


def _get_db():
    """Open (once) the durable store. Returns None if it cannot be opened."""
    global _DB, _DB_WARNED
    if _DB is not None:
        return _DB
    try:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: FastAPI serves requests on a threadpool and
        # the connection is guarded by SQLite's own locking plus the fact that
        # every write here is a single autocommitted statement.
        conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        # WAL so a reader (the flow canvas polling /audit) never blocks the
        # writer (the reconciliation that is still running).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS audit (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   batch_id TEXT NOT NULL,
                   ts TEXT NOT NULL,
                   agent TEXT NOT NULL,
                   detail TEXT NOT NULL
               )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_audit_batch ON audit(batch_id, id)")
        # The chain columns arrived after the table did; a store created
        # before them gains them here, and its older rows stay unchained.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(audit)")}
        for col in ("prev_hash", "hash"):
            if col not in cols:
                conn.execute(f"ALTER TABLE audit ADD COLUMN {col} TEXT")  # nosec B608 - fixed names
        conn.commit()
        _DB = conn
        if not _DB_WARNED:
            logger.info("Audit: durable store at %s (SQLite, WAL).", path)
            _DB_WARNED = True
        return _DB
    except Exception as exc:
        if not _DB_WARNED:
            logger.warning(
                "Audit: could not open the durable store (%s: %s). Falling "
                "back to files, which do NOT survive a temp-dir wipe.",
                type(exc).__name__, exc,
            )
            _DB_WARNED = True
        return None


_DB_WRITE_LOCK = threading.Lock()


def _db_write(entry: dict) -> None:
    conn = _get_db()
    if conn is None:
        raise RuntimeError("durable store unavailable")
    # BEGIN IMMEDIATE takes SQLite's write lock before the head is read, so a
    # second process cannot extend the same head between the read and the
    # insert; the thread lock does the same for this process's connection.
    with _DB_WRITE_LOCK:
        # The connection is shared with the settled ledger, open items and
        # the settlement cycle, which commit their own writes. If one left a
        # transaction open, BEGIN would raise and this entry would fall back
        # to the file tier — splitting the chain across two stores.
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT hash FROM audit WHERE batch_id = ? ORDER BY id DESC LIMIT 1",
                (entry["batch_id"],),
            ).fetchone()
            _seal(entry, (row[0] if row and row[0] else GENESIS))
            conn.execute(
                "INSERT INTO audit (batch_id, ts, agent, detail, prev_hash, hash) "
                "VALUES (?,?,?,?,?,?)",
                (entry["batch_id"], entry["timestamp_utc"], entry["agent"],
                 entry["detail"], entry["prev_hash"], entry["hash"]),
            )
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise


def _db_read(batch_id: str) -> list[dict] | None:
    conn = _get_db()
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT ts, batch_id, agent, detail, prev_hash, hash FROM audit "
            "WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ).fetchall()
    except Exception as exc:
        logger.warning("Audit: durable read failed (%s: %s).", type(exc).__name__, exc)
        return None
    out = []
    for r in rows:
        e = {"timestamp_utc": r[0], "batch_id": r[1], "agent": r[2], "detail": r[3]}
        if r[5]:
            e["prev_hash"], e["hash"] = r[4], r[5]
        out.append(e)
    return out


def storage_status() -> dict:
    """What /health reports, so durability is visible rather than assumed."""
    if _get_redis():
        return {"backend": "redis", "durable": True}
    if _get_db() is not None:
        return {"backend": "sqlite", "durable": True, "path": str(_db_path())}
    return {
        "backend": "file+memory",
        "durable": False,
        "note": "Entries are written under the system temp directory and do "
                "not survive a reboot or temp wipe.",
    }


def log_decision(batch_id: str, agent: str, detail: str) -> dict:
    """
    Record one pipeline decision. Always returns the entry dict so callers
    can include it in a response or assert on it in tests.

    Write order: Redis → SQLite → file → in-memory. The first backend that
    succeeds is used; the others are not tried for this call. The first two
    are durable; the last two are not, and storage_status() says which is in
    use so that is never a guess.
    """
    entry = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "batch_id": batch_id,
        "agent": agent,
        "detail": detail,
    }

    client = _get_redis()
    if client:
        try:
            _redis_append(client, entry)
            return entry
        except Exception as exc:
            # Redis became unavailable mid-session (connection lost, failover).
            # Log and fall through rather than losing the entry.
            logger.warning(
                "Audit: Redis write failed (%s: %s). Falling back to file.",
                type(exc).__name__, exc,
            )

    try:
        _db_write(entry)
        return entry
    except Exception as exc:
        logger.debug("Audit: durable write failed (%s). Falling back to file.", exc)

    try:
        _file_write(entry)
        return entry
    except Exception as exc:
        logger.warning(
            "Audit: file write failed (%s: %s). Falling back to in-memory.",
            type(exc).__name__, exc,
        )

    _memory_write(entry)
    return entry


def _redis_append(client, entry: dict) -> None:
    """Extend the batch's chain in Redis, retrying if another writer won."""
    list_key, head_key = f"audit:{entry['batch_id']}", f"audit:{entry['batch_id']}:head"
    for _ in range(8):
        raw_head = client.get(head_key)
        head = (raw_head.decode() if isinstance(raw_head, bytes) else raw_head) or GENESIS
        _seal(entry, head)
        try:
            won = client.eval(_REDIS_APPEND, 2, list_key, head_key, head,
                              json.dumps(entry), entry["hash"], GENESIS)
        except Exception:
            # A server without scripting: append without the guard. The chain
            # is still written; only the race between two writers is open.
            client.rpush(list_key, json.dumps(entry))
            client.set(head_key, entry["hash"])
            return
        if int(won or 0) == 1:
            return
    raise RuntimeError("audit chain: could not extend the head after 8 attempts")


def verify_chain(batch_id: str, receipt: str = "") -> dict:
    """
    Walk a batch's trail and check every link. Names the first break.

    `receipt` is a head hash a caller was given earlier; if supplied, the
    entry it names must still be in the chain, which is what catches a trail
    whose tail was cut off after the receipt was issued.
    """
    entries = get_audit_trail(batch_id)
    unchained = 0
    while unchained < len(entries) and not entries[unchained].get("hash"):
        unchained += 1
    prev = GENESIS
    verdict: dict[str, Any] = {"batch_id": batch_id, "entries": len(entries),
               "unchained_before_chain_began": unchained, "verified": 0,
               "intact": True, "broken_at": None, "reason": "", "head": None}
    for i in range(unchained, len(entries)):
        e = entries[i]
        if not e.get("hash"):
            verdict.update(intact=False, broken_at=i,
                           reason=f"Entry {i} carries no hash inside the chain: it was "
                                  f"inserted by something that bypassed the audit log.")
            break
        if e.get("prev_hash") != prev:
            verdict.update(intact=False, broken_at=i,
                           reason=f"Entry {i} does not point at the entry before it: "
                                  f"an entry was removed, reordered or inserted there.")
            break
        if entry_hash(e) != e["hash"]:
            verdict.update(intact=False, broken_at=i,
                           reason=f"Entry {i} was changed after it was written: its "
                                  f"content no longer matches its hash.")
            break
        prev = e["hash"]
        verdict["verified"] += 1
    verdict["head"] = prev if verdict["verified"] else None

    if receipt and verdict["intact"]:
        hashes = [e.get("hash") for e in entries]
        if receipt not in hashes:
            verdict.update(intact=False, broken_at=len(entries),
                           reason="The entry this receipt ends at is no longer in the "
                                  "trail: entries at or after it were removed.")
        else:
            verdict["receipt_position"] = hashes.index(receipt)

    if verdict["intact"]:
        verdict["plain"] = (
            f"All {verdict['verified']} chained entries check out: none altered, "
            f"removed or reordered."
            + (f" {unchained} older entr{'y' if unchained == 1 else 'ies'} predate the "
               f"chain and cannot be verified." if unchained else "")
            + (" The receipt's entry is still present." if receipt else ""))
    else:
        verdict["plain"] = "TAMPERING DETECTED. " + verdict["reason"]
    return verdict


def get_audit_trail(batch_id: str) -> list[dict]:
    """
    All recorded decisions for batch_id, in insertion order.

    Reads in the order log_decision writes: Redis if configured and it holds the
    batch, then SQLite, the file, memory. Entries split across backends by a
    mid-session Redis failure are not merged.
    """
    client = _get_redis()
    if client:
        try:
            raw = client.lrange(f"audit:{batch_id}", 0, -1)
            if raw:
                return [json.loads(e) for e in raw]
        except Exception as exc:
            logger.warning(
                "Audit: Redis read failed (%s: %s). Falling back to local.",
                type(exc).__name__, exc,
            )

    rows = _db_read(batch_id)
    if rows:
        return rows

    # Redis unavailable — check file first (cross-process), then memory.
    file_entries = _file_read(batch_id)
    if file_entries:
        return file_entries

    return _memory_read(batch_id)


def clear_trail(batch_id: str) -> None:
    """
    Remove all audit entries for batch_id.

    Intended for test teardown. Does not raise if the batch is not found.
    It used to leave SQLite rows in place — the tier that actually serves —
    which is why tests took to minting a fresh batch id each.
    """
    client = _get_redis()
    if client:
        try:
            client.delete(f"audit:{batch_id}", f"audit:{batch_id}:head")
            return
        except Exception as exc:
            logger.debug("clear_trail: best-effort step skipped (%s: %s)", type(exc).__name__, exc)

    conn = _get_db()
    if conn is not None:
        try:
            with _DB_WRITE_LOCK:
                conn.execute("DELETE FROM audit WHERE batch_id = ?", (batch_id,))
                conn.commit()
        except Exception as exc:
            logger.warning("Audit: could not clear durable entries: %s", exc)
    _FILE_HEADS.pop(batch_id, None)

    # File backend: one file per batch, so clearing is a delete rather than a
    # read-filter-rewrite of everything ever logged.
    with _AUDIT_FILE_LOCK:
        _close_handle(batch_id)
        try:
            _batch_path(batch_id).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Audit: could not clear file entries: %s", exc)

    global _FALLBACK_LOG
    _FALLBACK_LOG = [e for e in _FALLBACK_LOG if e.get("batch_id") != batch_id]


def find_entries(agent: str, contains: str = "", limit: int = 200) -> list[dict]:
    """
    Entries across every batch for one agent (e.g. escalations), newest first.
    Reads the durable store where there is one, else memory.
    """
    needle = (contains or "").lower()

    conn = _get_db()
    if conn is not None:
        try:
            sql = ("SELECT ts, batch_id, agent, detail FROM audit "
                   "WHERE agent = ?")
            params: list = [agent]
            if contains:
                sql += " AND lower(detail) LIKE ?"
                params.append(f"%{needle}%")
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(int(limit))
            rows = conn.execute(sql, params).fetchall()
            return [{"timestamp_utc": r[0], "batch_id": r[1],
                     "agent": r[2], "detail": r[3]} for r in rows]
        except sqlite3.Error as e:
            logger.warning("Cross-batch audit query failed: %s", e)

    # In-memory last resort, when there is no durable store or the query failed.
    out = []
    for entry in _FALLBACK_LOG:
        if entry.get("agent") != agent:
            continue
        if needle and needle not in (entry.get("detail") or "").lower():
            continue
        out.append(entry)
    out.sort(key=lambda e: e.get("timestamp_utc") or "", reverse=True)
    return out[:limit]
