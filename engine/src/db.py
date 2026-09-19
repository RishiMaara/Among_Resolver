import sqlite3
import logging
from pathlib import Path
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_DB_CONNECTION: "sqlite3.Connection | None" = None

def get_db_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "amongresolver.sqlite3"

def init_db() -> None:
    global _DB_CONNECTION
    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    
    # Check_same_thread=False because FastAPI workers share it
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
    
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.commit()
        logger.info("Database initialized at %s (SQLite, WAL)", path)
        _DB_CONNECTION = conn
        _create_tables(conn)
    except sqlite3.Error as e:
        logger.error("Failed to initialize database: %s", e)
        raise

def _create_tables(conn: sqlite3.Connection) -> None:
    # Bi-temporal ledger support for chargebacks and audits
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settled_batches (
            batch_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            cleared_at_utc TEXT,
            maker_id TEXT,
            checker_id TEXT,
            valid_from_utc TEXT NOT NULL,
            valid_to_utc TEXT,
            is_reversal BOOLEAN DEFAULT 0
        )
    """)
    conn.commit()

@contextmanager
def get_db_cursor():
    """Provides a transactional cursor. Commits on success, rollbacks on error."""
    global _DB_CONNECTION
    if _DB_CONNECTION is None:
        init_db()
        
    cursor = _DB_CONNECTION.cursor()
    try:
        yield cursor
        _DB_CONNECTION.commit()
    except Exception:
        _DB_CONNECTION.rollback()
        raise
    finally:
        cursor.close()
