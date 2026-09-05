"""
The HTTP surface.

Every endpoint in this engine was previously verified by hand — a curl during
development, a click in the browser — and pinned by nothing. That is the same
class of gap the frontend had: the code works today and there is no test that
notices when it stops.

These cover the decision endpoints and the escalations list in particular,
because those carry the promises that are easiest to break silently. An
unattributed decision must be refused, a statutory clearance must not read as
discharging a reporting duty, and an escalation must be findable across every
batch rather than only inside the one it came from.
"""

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def batch(request):
    """A batch id unique to the test, so decisions cannot bleed between them."""
    return f"TEST-{request.node.name}"


# ── read-only endpoints ───────────────────────────────────────────────────

def test_health_reports_its_storage_backend(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    storage = body.get("audit_storage") or body.get("storage") or {}
    # Durability must be reported rather than assumed — an operator should
    # never have to guess whether the trail survives a restart.
    assert "backend" in storage
    assert "durable" in storage


def test_rulebook_publishes_every_rule_with_its_basis(client):
    r = client.get("/compliance/rulebook")
    assert r.status_code == 200
    rules = r.json()["rules"]
    assert len(rules) >= 12
    for rule in rules:
        # Basis is the distinction the whole compliance screen rests on. A
        # rule that cannot say whether it is law has no business being shown.
        assert rule["basis"] in (
            "statutory", "regulatory_guidance", "internal_policy",
        ), rule["rule_id"]
        for field in ("rule_id", "title", "why", "remediation", "threshold_applied"):
            assert rule.get(field), f"{rule['rule_id']} missing {field}"


def test_audit_trail_404s_for_a_batch_that_never_ran(client):
    r = client.get("/audit/NO-SUCH-BATCH-EVER")
    assert r.status_code == 404


# ── the batch decision ────────────────────────────────────────────────────

def test_decision_is_recorded_and_readable_back(client, batch):
    r = client.post(f"/settlement/{batch}/decision", json={
        "decision": "confirmed",
        "reviewer": "alice@example.com",
        "note": "Checked against the merchant dashboard.",
        "txn_ids": ["T1", "T2"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "confirmed"
    assert body["reviewer"] == "alice@example.com"

    back = client.get(f"/settlement/{batch}/decisions").json()
    assert back["count"] == 1
    detail = back["decisions"][0]["detail"]
    assert "alice@example.com" in detail
    assert "CONFIRMED" in detail
    assert "Checked against the merchant dashboard." in detail


def test_an_unattributed_decision_is_refused(client, batch):
    """A decision nobody is named against is what this endpoint exists to stop."""
    r = client.post(f"/settlement/{batch}/decision", json={
        "decision": "confirmed", "reviewer": "   ",
    })
    assert r.status_code == 422
    assert "attributed to someone" in r.json()["detail"]["plain"]


def test_an_unknown_verdict_is_refused(client, batch):
    r = client.post(f"/settlement/{batch}/decision", json={
        "decision": "maybe", "reviewer": "alice@example.com",
    })
    assert r.status_code == 422
    assert "'confirmed' or 'rejected'" in r.json()["detail"]["message"]


def test_the_decision_says_it_does_not_alter_the_verdict(client, batch):
    """
    The recorded text has to carry this. A trail where a human decision could
    be read as the engine's own is worth nothing — and the moment confirming
    reads as clearing, the zero-false-clears figure stops meaning anything.
    """
    r = client.post(f"/settlement/{batch}/decision", json={
        "decision": "confirmed", "reviewer": "alice@example.com",
    })
    assert "does not alter the engine's own verdict" in r.json()["audit_entry"]["detail"]


def test_redeciding_appends_rather_than_replaces(client, batch):
    for verdict in ("confirmed", "rejected"):
        client.post(f"/settlement/{batch}/decision", json={
            "decision": verdict, "reviewer": "alice@example.com",
        })
    back = client.get(f"/settlement/{batch}/decisions").json()
    # People change their minds with new information, and the sequence is
    # itself part of the record.
    assert back["count"] == 2


# ── the posting proposal ──────────────────────────────────────────────────

def test_journal_approval_records_that_nothing_was_posted(client, batch):
    r = client.post(f"/settlement/{batch}/journal/decision", json={
        "decision": "approved",
        "reviewer": "alice@example.com",
        "entry_id": "JE-1",
        "note": "Fees agree to the rate card.",
    })
    assert r.status_code == 200
    body = r.json()
    # Approving is a sign-off, not a posting. This engine never writes to a
    # ledger and the record has to say so in as many words.
    assert body["posted"] is False
    assert "Nothing has been posted to any ledger" in body["audit_entry"]["detail"]


def test_journal_approval_requires_a_reviewer(client, batch):
    r = client.post(f"/settlement/{batch}/journal/decision", json={
        "decision": "approved", "reviewer": "",
    })
    assert r.status_code == 422


def test_journal_rejects_an_unknown_verdict(client, batch):
    r = client.post(f"/settlement/{batch}/journal/decision", json={
        "decision": "posted", "reviewer": "alice@example.com",
    })
    assert r.status_code == 422


# ── compliance findings ───────────────────────────────────────────────────

def test_clearing_a_statutory_finding_does_not_discharge_the_duty(client, batch):
    """
    The carve-out that matters most.

    A CTR obligation is a duty to REPORT. Deciding the transaction is
    legitimate does not remove it, and a reviewer who clicks a button and
    believes they have filed something is worse off than one with no button.
    """
    r = client.post(f"/settlement/{batch}/compliance/decision", json={
        "rule_id": "CTR_THRESHOLD",
        "decision": "cleared",
        "reviewer": "alice@example.com",
        "basis": "statutory",
        "note": "Verified as a corporate order.",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["discharges_reporting_obligation"] is False
    detail = body["audit_entry"]["detail"]
    assert "STATUTORY" in detail
    assert "does NOT discharge any reporting duty" in detail


def test_clearing_an_internal_finding_carries_no_statutory_caveat(client, batch):
    r = client.post(f"/settlement/{batch}/compliance/decision", json={
        "rule_id": "DUPLICATE_TX",
        "decision": "cleared",
        "reviewer": "alice@example.com",
        "basis": "internal_policy",
    })
    assert "reporting duty" not in r.json()["audit_entry"]["detail"]


def test_compliance_verbs_are_cleared_or_escalated(client, batch):
    """Nobody 'approves' a suspected structuring pattern."""
    r = client.post(f"/settlement/{batch}/compliance/decision", json={
        "rule_id": "STRUCTURING_PATTERN",
        "decision": "approved",
        "reviewer": "alice@example.com",
    })
    assert r.status_code == 422
    assert "'cleared' or 'escalated'" in r.json()["detail"]["message"]


# ── escalations ───────────────────────────────────────────────────────────

def test_an_escalation_is_findable_across_batches(client):
    """
    "Escalate" names a destination. Before this list existed the button wrote
    a line into the batch's own trail and stopped, so nobody downstream could
    find it.
    """
    bid = "TEST-ESCALATION-VISIBLE"
    client.post(f"/settlement/{bid}/compliance/decision", json={
        "rule_id": "STRUCTURING_PATTERN",
        "decision": "escalated",
        "reviewer": "alice@example.com",
        "note": "Four sub-threshold orders in a day.",
        "txn_ids": ["T1", "T2", "T3", "T4"],
    })

    r = client.get("/escalations")
    assert r.status_code == 200
    body = r.json()
    mine = [e for e in body["escalations"] if e["batch_id"] == bid]
    assert len(mine) == 1
    row = mine[0]
    assert row["rule_id"] == "STRUCTURING_PATTERN"
    assert row["escalated_by"] == "alice@example.com"
    assert row["payment_count"] == 4
    assert row["note"] == "Four sub-threshold orders in a day."


def test_a_cleared_finding_is_not_an_escalation(client):
    bid = "TEST-ESCALATION-EXCLUDES-CLEARED"
    client.post(f"/settlement/{bid}/compliance/decision", json={
        "rule_id": "DUPLICATE_TX",
        "decision": "cleared",
        "reviewer": "alice@example.com",
    })
    body = client.get("/escalations").json()
    assert [e for e in body["escalations"] if e["batch_id"] == bid] == []


# ── the fungible settlement ───────────────────────────────────────────────

def _fungible_report(wholly=True, proposal=True):
    """A stored run for a merchant selling one item at one price."""
    return {
        "summary": {"batch_id": "CHAI-1", "cleared": False, "ambiguous": True},
        "interchangeable": {
            "groups": [{"amount_cents": 500, "currency": "INR",
                        "picked": 3, "identical_available": 2000,
                        "txn_ids": ["p1", "p2", "p3"]}],
            "wholly_interchangeable": wholly,
            "fifo_proposal": ({
                "basis": "oldest_first", "count": 3,
                "txn_ids": ["p1", "p2", "p3"],
                "earliest": "2026-08-17 06:00:00+00:00",
                "latest": "2026-08-17 06:02:00+00:00",
                "pool_size": 2000,
            } if proposal else None),
        },
    }


def test_accepting_the_convention_consumes_the_payments(client):
    import settlement_qa
    settlement_qa.store_result("CHAI-1", _fungible_report())

    r = client.post("/settlement/CHAI-1/accept-fifo", json={
        "reviewer": "alice@example.com",
        "note": "Chai stall — every sale is Rs 5.",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["basis"] == "oldest_first"
    assert body["consumed_count"] == 3

    # The whole point: this is NOT a clear.
    assert body["clears_the_batch"] is False
    detail = body["audit_entry"]["detail"]
    assert "NOT on evidence" in detail
    assert "any set of the same size would have been equally correct" in detail
    assert "engine's own verdict is unchanged" in detail


def test_a_second_settlement_cannot_take_the_same_payments(client):
    """The one risk that is real when payments are fungible."""
    import settlement_qa
    settlement_qa.store_result("CHAI-A", _fungible_report())
    client.post("/settlement/CHAI-A/accept-fifo",
                json={"reviewer": "alice@example.com"})

    # A different batch, proposing the very same payments.
    settlement_qa.store_result("CHAI-B", _fungible_report())
    r = client.post("/settlement/CHAI-B/accept-fifo",
                    json={"reviewer": "alice@example.com"})
    assert r.status_code == 409
    assert "already recorded against" in r.json()["detail"]["plain"]


def test_the_convention_is_refused_when_the_choice_is_real(client):
    """
    FIFO is only defensible when nothing distinguishes the candidates. Using
    it on a batch withheld for any other reason would launder a real question
    into a convention.
    """
    import settlement_qa
    settlement_qa.store_result("REAL-1", _fungible_report(wholly=False))
    r = client.post("/settlement/REAL-1/accept-fifo",
                    json={"reviewer": "alice@example.com"})
    assert r.status_code == 422
    assert "needs an answer rather than a convention" in r.json()["detail"]["plain"]


def test_the_convention_requires_a_named_reviewer(client):
    import settlement_qa
    settlement_qa.store_result("CHAI-N", _fungible_report())
    r = client.post("/settlement/CHAI-N/accept-fifo", json={"reviewer": "  "})
    assert r.status_code == 422


def test_accepting_needs_a_run_to_accept(client):
    r = client.post("/settlement/NEVER-RAN/accept-fifo",
                    json={"reviewer": "alice@example.com"})
    assert r.status_code == 404


def test_cross_batch_search_survives_having_no_durable_store():
    """
    find_entries' in-memory fallback read a name that did not exist, so it
    raised NameError rather than falling back — and /escalations is what calls
    it. The page a reviewer is SENT to would have broken exactly when the
    audit store was already in trouble.

    Pylint found this; no test reached it, because the path only runs when the
    database is unavailable.
    """
    import audit

    real_db = audit._get_db
    audit._get_db = lambda: None          # force the last-resort path
    try:
        audit._FALLBACK_LOG.clear()
        audit._FALLBACK_LOG.extend([
            {"timestamp_utc": "2026-09-02T10:00:00Z", "batch_id": "B1",
             "agent": "human_reviewer", "detail": "alice ESCALATED finding X"},
            {"timestamp_utc": "2026-09-02T11:00:00Z", "batch_id": "B2",
             "agent": "subset_sum", "detail": "not a human decision"},
        ])
        found = audit.find_entries("human_reviewer", "ESCALATED", limit=10)
        assert len(found) == 1
        assert found[0]["batch_id"] == "B1"
    finally:
        audit._get_db = real_db
        audit._FALLBACK_LOG.clear()
