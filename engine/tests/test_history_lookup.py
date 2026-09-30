"""
Run history: one batch's runs are found without reading anyone else's, and
the listing is newest first.

Every reconciliation asks for earlier runs of its own batch. The lookup read
and decoded every record ever written before filtering, so a run took longer
with every run before it: 3,305 records, 3.4 seconds each time. And the
listing sorted by file name, which put batches in alphabetical order under a
heading that promised newest first.
"""
import audit
import history


def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "redis_url", lambda: "")
    monkeypatch.setattr(history, "HISTORY_DIR", str(tmp_path / "history"))
    monkeypatch.setattr(history, "_FALLBACK_DIR", str(tmp_path / "fallback"))


def test_a_batchs_runs_are_found_without_reading_other_batches(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    for bid in ("ALPHA", "BETA", "BETA", "GAMMA"):
        history.record_run(bid, {"batch_id": bid}, {"summary": {"batch_id": bid}})
    opened = []
    real = history._read
    monkeypatch.setattr(history, "_read", lambda p: opened.append(p) or real(p))
    runs = history.list_runs(batch_id="BETA")
    assert [r["batch_id"] for r in runs] == ["BETA", "BETA"]
    assert len(opened) == 2 and all("run_BETA_" in p for p in opened)


def test_the_listing_is_newest_first_not_alphabetical(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    for bid in ("ZULU", "ALPHA", "MIKE"):
        history.record_run(bid, {"batch_id": bid}, {"summary": {"batch_id": bid}})
    assert [r["batch_id"] for r in history.list_runs()] == ["MIKE", "ALPHA", "ZULU"]
