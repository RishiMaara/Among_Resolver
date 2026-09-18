"""
What a hosted deployment depends on that a laptop never exercises.

Locally the engine runs as one long-lived process on a writable disk, so a
whole class of assumption is invisible there: that the code directory can be
written to, that the next request reaches the process that served the last
one, that a list fetched into the repo root exists at all. A serverless host
breaks each of those, and none of them fails loudly — history silently stops
recording, the agent-flow view silently finds no trail, screening silently
drops to four illustrative names.

So this file pins the behaviours a deploy needs, plus one guard that is about
the demo rather than the host: the result each sample preset PROMISES in the
UI has to be the result the engine actually returns.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
ROOT = ENGINE.parent
sys.path.insert(0, str(ENGINE / "src"))

import audit  # noqa: E402
import history  # noqa: E402


# ── the Vercel entrypoint ─────────────────────────────────────────────────

def test_the_entrypoint_serves_the_app_from_outside_src():
    """
    engine/api/index.py is what Vercel imports, and it does so with a working
    directory that is not src/. The modules import each other flat, so the
    entrypoint has to put src/ on the path itself — run it in a fresh process
    from a neutral directory to prove it does.
    """
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('entry', r'{ENGINE / 'api' / 'index.py'}')\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "from fastapi.testclient import TestClient\n"
        "r = TestClient(m.app).get('/health')\n"
        "print(r.status_code)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-800:]
    assert out.stdout.strip().splitlines()[-1] == "200"


def test_the_entry_directory_cannot_shadow_the_route_package():
    """
    src/api/ is the package holding the route modules. engine/api/ holds the
    entrypoint. If engine/api/ ever gained an __init__.py it would become a
    regular package that could shadow src/api/ depending on sys.path order.
    """
    assert (ENGINE / "src" / "api" / "__init__.py").is_file()
    assert not (ENGINE / "api" / "__init__.py").exists()


# ── state that has to survive a read-only code directory ──────────────────

def test_history_falls_back_when_its_directory_cannot_be_written(tmp_path, monkeypatch):
    """
    On a serverless host the code sits on a read-only filesystem, so the
    default history directory cannot be created. That must move the record,
    not lose it — the history page would otherwise be empty forever.
    """
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("a file where a directory is expected")
    monkeypatch.setattr(history, "HISTORY_DIR", str(blocker / "history"))
    fallback = tmp_path / "fallback"
    monkeypatch.setattr(history, "_FALLBACK_DIR", str(fallback))
    monkeypatch.setattr(audit, "redis_url", lambda: "")

    where = history.record_run("FALLBACK-1", {"batch_id": "FALLBACK-1"},
                               {"summary": {"batch_id": "FALLBACK-1", "cleared": True}})
    assert Path(where).parent == fallback
    runs = history.list_runs()
    assert [r["batch_id"] for r in runs] == ["FALLBACK-1"]
    assert history.storage_status()["fallback"] is True


class _FakeRedis:
    """Just the commands history uses, so the shared path runs without a server."""

    def __init__(self):
        self.hashes: dict = {}
        self.lists: dict = {}

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hdel(self, key, *fields):
        for f in fields:
            self.hashes.get(key, {}).pop(f, None)

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    def lrange(self, key, start, end):
        items = self.lists.get(key, [])
        return items[start:] if end == -1 else items[start:end + 1]

    def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[start:end + 1]

    def pipeline(self):
        outer = self

        class _P:
            def __init__(self):
                self.ops = []

            def __getattr__(self, name):
                return lambda *a: self.ops.append((name, a))

            def execute(self):
                for name, a in self.ops:
                    getattr(outer, name)(*a)
        return _P()


class TestSharedHistory:
    """
    Several serverless instances serve one app. A run recorded by one has to
    appear on the history page served by another, which only a shared store
    can do.
    """

    @pytest.fixture
    def shared(self, monkeypatch, tmp_path):
        fake = _FakeRedis()
        monkeypatch.setattr(audit, "redis_url", lambda: "rediss://example")
        monkeypatch.setattr(audit, "_get_redis", lambda: fake)
        # Point disk somewhere empty, so a pass here cannot come from files.
        monkeypatch.setattr(history, "HISTORY_DIR", str(tmp_path / "unused"))
        monkeypatch.setattr(history, "_FALLBACK_DIR", str(tmp_path / "unused2"))
        return fake

    def test_a_run_recorded_is_listed_and_readable_from_the_shared_store(self, shared):
        where = history.record_run("SHARED-1", {"batch_id": "SHARED-1"},
                                   {"summary": {"batch_id": "SHARED-1", "cleared": True},
                                    "audit_trail": [{"agent": "x"}]})
        assert where.startswith("redis:")
        runs = history.list_runs()
        assert runs[0]["batch_id"] == "SHARED-1"
        assert history.run_detail(runs[0]["record"])["inputs"]["batch_id"] == "SHARED-1"
        assert history.storage_status() == {"backend": "redis", "shared": True}

    def test_the_shared_store_is_capped(self, shared, monkeypatch):
        monkeypatch.setattr(history, "MAX_SHARED_RECORDS", 3)
        for i in range(5):
            history.record_run(f"CAP-{i}", {"batch_id": f"CAP-{i}"},
                               {"summary": {"batch_id": f"CAP-{i}"}})
        assert len(shared.lists[history._REDIS_INDEX]) == 3
        assert len(shared.hashes[history._REDIS_RECORDS]) == 3, (
            "trimming the index without deleting the records it named would "
            "leak storage forever"
        )
        assert [r["batch_id"] for r in history.list_runs()] == ["CAP-4", "CAP-3", "CAP-2"]

    def test_redis_is_opt_in_by_url_not_by_a_local_server_happening_to_run(self, monkeypatch):
        monkeypatch.setattr(audit, "redis_url", lambda: "")
        monkeypatch.setattr(audit, "_get_redis", lambda: _FakeRedis())
        assert history._shared_store() is None


# ── the audit store's connection to a hosted Redis ─────────────────────────

class TestRedisUrl:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        monkeypatch.setattr(audit, "_REDIS_CLIENT", None)
        monkeypatch.setattr(audit, "_REDIS_RETRY_AT", 0.0)
        for var in audit.REDIS_URL_ENV_VARS + ("REDIS_HOST", "REDIS_PORT"):
            monkeypatch.delenv(var, raising=False)

    def test_vercels_kv_url_is_accepted_as_well_as_redis_url(self, monkeypatch):
        monkeypatch.setenv("KV_URL", "rediss://default:pw@host:6379")
        assert audit.redis_url() == "rediss://default:pw@host:6379"

    def test_a_configured_url_gets_timeouts_that_fit_a_tls_handshake(self, monkeypatch):
        import redis
        seen = {}

        class _Client:
            def ping(self):
                return True

        def from_url(url, **kw):
            seen["url"], seen["kw"] = url, kw
            return _Client()

        monkeypatch.setenv("REDIS_URL", "rediss://default:secret@host:6379")
        monkeypatch.setattr(redis.Redis, "from_url", staticmethod(from_url))
        assert audit._get_redis() is not None
        assert seen["url"] == "rediss://default:secret@host:6379"
        assert seen["kw"]["socket_connect_timeout"] >= 1.0, (
            "the quarter-second budget meant for probing localhost cannot "
            "fit a TLS handshake to another region"
        )

    def test_the_password_in_the_url_is_never_logged(self, monkeypatch, caplog):
        import redis

        def from_url(url, **kw):
            raise ConnectionError(f"could not reach {url}")

        monkeypatch.setenv("REDIS_URL", "rediss://default:hunter2@host:6379")
        monkeypatch.setattr(redis.Redis, "from_url", staticmethod(from_url))
        with caplog.at_level("DEBUG"):
            assert audit._get_redis() is None
        assert "hunter2" not in caplog.text

    def test_a_configured_store_is_retried_rather_than_abandoned(self, monkeypatch):
        """
        One failed ping on a cold start must not downgrade a serverless
        instance to private state for its whole life when shared state was
        explicitly asked for.
        """
        import redis
        calls = {"n": 0}

        class _Client:
            def ping(self):
                return True

        def from_url(url, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("cold start blip")
            return _Client()

        monkeypatch.setenv("REDIS_URL", "rediss://default:pw@host:6379")
        monkeypatch.setattr(redis.Redis, "from_url", staticmethod(from_url))
        assert audit._get_redis() is None
        monkeypatch.setattr(audit, "_REDIS_RETRY_AT", 0.0)  # the 30s has passed
        assert audit._get_redis() is not None


# ── /health says what a deploy needs to know ───────────────────────────────

def test_health_warns_when_serverless_state_is_not_shared(monkeypatch):
    from fastapi.testclient import TestClient
    import main

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(audit, "redis_url", lambda: "")
    body = TestClient(main.app).get("/health").json()
    assert "history_storage" in body
    assert any("without a shared store" in w for w in body["warnings"])


# ── the real sanctions list reaches a deploy rooted at engine/ ────────────

def test_a_deploy_rooted_at_engine_still_finds_a_real_sanctions_list():
    """
    Vercel builds the engine from engine/ and never sees the repo root, where
    a local fetch writes. Without the tracked snapshot, the live engine would
    screen four illustrative names.
    """
    import compliance_agent

    snapshot = ENGINE / "data" / "sanctions" / "un_consolidated.txt"
    assert snapshot in compliance_agent.DEFAULT_SANCTIONS_PATHS
    assert snapshot.is_file(), "the deploy snapshot must be tracked"
    names, meta = compliance_agent._read_list_file(str(snapshot))
    assert len(names) > 3000
    assert meta.get("retrieved"), "a snapshot must say when it was taken"


# ── the demo must deliver what it promises ─────────────────────────────────

_PRESET_FILE = ROOT / "src" / "lib" / "sample-preset.ts"
_FIELDS = ("label", "batchId", "netAmount", "settledAt", "windowDays",
           "memberSource", "declaredDeductions", "expected")


def _presets() -> list[dict]:
    source = _PRESET_FILE.read_text(encoding="utf-8")
    body = source[source.index("SAMPLE_PRESETS"):]
    out, current = [], {}
    for field, value in re.findall(r'(\w+):\s*"([^"]*)"', body):
        if field not in _FIELDS:
            continue
        if field == "label" and current:
            out.append(current)
            current = {}
        current[field] = value
    if current:
        out.append(current)
    return [p for p in out if "expected" in p]


@pytest.mark.parametrize("preset", _presets(), ids=lambda p: p["label"])
def test_each_sample_preset_delivers_the_result_its_label_promises(preset, monkeypatch):
    """
    The UI shows `expected` BEFORE the engine runs, as a promise. It went
    stale once already: the ambiguity constant was re-measured from 0.54 to
    0.36 and the "withholds" preset kept telling every visitor 0.54 while the
    engine answered 0.36 underneath it. Nothing caught that, because nothing
    compared the two.
    """
    from fastapi.testclient import TestClient
    import main

    samples = ROOT / "public" / "sample-data"
    data = {
        "batch_id": preset["batchId"],
        "net_amount": preset["netAmount"],
        "settled_at": f"{preset['settledAt']}:00Z",
        "settlement_window_days": preset["windowDays"],
        "currency": "INR",
        "declared_deductions": preset["declaredDeductions"],
    }
    if preset["memberSource"]:
        data["member_source"] = preset["memberSource"]
    files = {
        "gateway_file": ("gateway_report.csv", (samples / "gateway_report.csv").read_bytes(), "text/csv"),
        "bank_file": ("bank_statement.csv", (samples / "bank_statement.csv").read_bytes(), "text/csv"),
        "erp_file": ("erp_ledger.json", (samples / "erp_ledger.json").read_bytes(), "application/json"),
    }
    r = TestClient(main.app).post("/reconcile/upload", data=data, files=files)
    assert r.status_code == 200, r.text[:400]
    s = r.json()["summary"]

    promised = preset["expected"]
    verdict = "cleared" if s["cleared"] else "withheld"
    assert promised.startswith(verdict), f"UI promises '{promised}', engine says {verdict}"
    conf = re.search(r"confidence ([0-9.]+)", promised)
    assert conf and abs(float(conf.group(1)) - float(s["confidence"])) < 0.005, (
        f"UI promises '{promised}', engine returned confidence {s['confidence']}"
    )
    matched = re.search(r"(\d+) matched", promised)
    assert matched and int(matched.group(1)) == s["matched_count"], (
        f"UI promises '{promised}', engine matched {s['matched_count']}"
    )
