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


def test_the_durable_store_survives_concurrent_threads():
    """FAILURE_LOG 45: one sqlite3 connection shared across the threadpool
    crashed under the load test. Each thread now has its own."""
    import threading
    from uuid import uuid4

    errors: list[BaseException] = []

    def work():
        try:
            for _ in range(40):
                tid = f"CONC-{uuid4().hex[:10]}"
                settled_ledger.record_settled(f"B-{tid}", [tid])
                assert settled_ledger.owners([tid]) == {tid: f"B-{tid}"}
        except BaseException as exc:  # pragma: no cover - the failure being guarded
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:1]
