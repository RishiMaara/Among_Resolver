"""
Separation of duties: whoever accepted the match cannot approve the posting.

The first attempt at this shipped as an RBAC module with a module-level
`_current_user` global — a race in any web server, and wired to nothing. What
is enforced now reads the audit trail, which is durable and shared across
instances, and sits on the endpoint a reviewer actually calls.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from fastapi.testclient import TestClient

import audit
import four_eyes
import main


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def batch():
    # A fresh id per test. The audit trail is durable by design — that is the
    # point of it — so tests that share a batch id inherit each other's
    # decisions, and "no prior acceptance" quietly stops being true.
    bid = f"SOD-BATCH-{uuid.uuid4().hex[:8]}"
    audit.clear_trail(bid)
    yield bid
    audit.clear_trail(bid)


def accept_match(client, bid, reviewer):
    return client.post(f"/settlement/{bid}/decision",
                       json={"decision": "confirmed", "reviewer": reviewer})


def approve_posting(client, bid, reviewer):
    return client.post(f"/settlement/{bid}/journal/decision",
                       json={"decision": "approved", "reviewer": reviewer,
                             "entry_id": "JE-1"})


class TestTheRule:
    def test_the_same_person_cannot_approve_the_posting_they_accepted(self, client, batch):
        assert accept_match(client, batch, "Rishi").status_code == 200
        r = approve_posting(client, batch, "Rishi")
        assert r.status_code == 409
        plain = r.json()["detail"]["plain"]
        assert "cannot approve" in plain and "Rishi" in plain, plain

    def test_a_second_person_can(self, client, batch):
        accept_match(client, batch, "Rishi")
        assert approve_posting(client, batch, "Priya").status_code == 200

    def test_the_same_name_typed_differently_is_the_same_person(self, client, batch):
        accept_match(client, batch, "Rishi")
        assert approve_posting(client, batch, "  rishi ").status_code == 409, (
            "case and stray spaces are not a different reviewer"
        )

    def test_accepting_the_oldest_first_convention_also_counts_as_accepting(
            self, client, batch):
        """
        accept-fifo is the other way a person says 'this set is right'. It has
        to disqualify them from approving the posting too, or the rule is a
        stile with a gate beside it.
        """
        audit.log_decision(
            batch_id=batch, agent="human_reviewer",
            detail=four_eyes.marker("Rishi", four_eyes.ACT_FIFO_ACCEPTANCE).strip()
                   + " Rishi ACCEPTED the oldest-first convention.",
        )
        assert approve_posting(client, batch, "Rishi").status_code == 409

    def test_rejecting_your_own_proposal_is_allowed(self, client, batch):
        """
        Refusing your own work needs no second pair of eyes, and blocking it
        would only teach people to route rejections through someone else.
        """
        accept_match(client, batch, "Rishi")
        r = client.post(f"/settlement/{batch}/journal/decision",
                        json={"decision": "rejected", "reviewer": "Rishi"})
        assert r.status_code == 200

    def test_a_posting_with_no_prior_acceptance_is_not_blocked(self, client, batch):
        assert approve_posting(client, batch, "Rishi").status_code == 200

    def test_the_refusal_is_on_the_record(self, client, batch):
        accept_match(client, batch, "Rishi")
        approve_posting(client, batch, "Rishi")
        trail = audit.get_audit_trail(batch)
        assert any("REFUSED" in e["detail"] and "separation of duties" in e["detail"]
                   for e in trail), "a refused approval must leave a trace"


class TestHowActorsAreRead:
    def test_actors_come_from_a_marker_not_from_prose(self, batch):
        """
        Names are read from a structured marker, not parsed out of the
        sentence around them — a reviewer called "Approved" would otherwise
        be a puzzle the parser loses.
        """
        audit.log_decision(
            batch_id=batch, agent="human_reviewer",
            detail=four_eyes.marker("Approved", four_eyes.ACT_BATCH_DECISION).strip()
                   + " Approved APPROVED this batch.",
        )
        assert four_eyes.actors(batch, four_eyes.MAKER_ACTS) == {"approved"}

    def test_an_empty_name_never_matches_anyone(self, batch):
        assert four_eyes.conflict(batch, "") is None
        assert four_eyes.conflict(batch, "   ") is None

    def test_the_identity_limit_is_stated_rather_than_implied(self):
        assert "exactly as strong as" in four_eyes.__doc__, (
            "the docstring must keep saying that a typed name is not authentication"
        )
