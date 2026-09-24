"""
Test isolation for the durable audit store.

The audit trail became durable — SQLite rather than temp files — which is the
right behaviour for the engine and the wrong behaviour for a test suite. Tests
that write audit entries and then count them started accumulating across runs:
a test asserting 200 entries saw 600 on its third invocation.

So every test session gets its own database in a temp directory, created
before any engine module is imported. The alternative — unique batch ids per
test — would work too, but it puts the burden on every future test rather
than solving it once, and it still leaves the suite writing into the store an
operator reads.
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

# Must be set before `audit` is imported, since it resolves the path lazily on
# first use but caches the connection.
_TEST_DB = Path(tempfile.gettempdir()) / f"among_resolver_test_{uuid.uuid4().hex}.sqlite3"
os.environ["AUDIT_DB_PATH"] = str(_TEST_DB)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def pytest_sessionfinish(session, exitstatus):
    """Leave nothing behind. WAL and shared-memory sidecars included."""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(_TEST_DB) + suffix)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_settlement_cycle():
    """
    A learned settlement cycle is durable by design, which inside one test
    session means a clear in one test changes linkage in the next. Every test
    starts with nothing learned; tests about learning build their own history.
    """
    import settlement_cycle
    settlement_cycle.reset()
    yield


@pytest.fixture(autouse=True)
def _no_real_model(monkeypatch):
    """
    No test reaches a real model. main.py loads engine/.env, so on a machine
    with a key every test that touched a model path made a real, paid-for,
    nondeterministic call — found when a scan test got an answer from Gemini.
    Tests that need a model fake one explicitly.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    # Nor the ECB: the FX reference note is advisory and tests stay offline.
    monkeypatch.setenv("FX_REFERENCE", "0")
    yield
