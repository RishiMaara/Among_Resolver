"""
Every in-process fallback is registered in stores.py, so one place can say
what this instance holds that other instances cannot see.
"""
from fastapi.testclient import TestClient

import main
import settled_ledger
import stores


def test_a_modules_fallback_is_the_registered_one():
    assert settled_ledger._memory is stores.local("settled_ledger", dict)


def test_health_reports_what_this_instance_holds(monkeypatch):
    monkeypatch.setitem(settled_ledger._memory, "REGISTRY-TEST-1", "BATCH")
    assert stores.process_local_state().get("settled_ledger", 0) >= 1
    body = TestClient(main.app).get("/health").json()
    assert body["process_local_state"].get("settled_ledger", 0) >= 1
