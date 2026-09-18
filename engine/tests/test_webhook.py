"""
The webhook endpoint, and specifically its refusals.

An endpoint that accepts unsigned settlement notifications is strictly worse
than the polling it replaces: polling at least talks to an authenticated API,
while an open webhook lets anyone who finds the URL assert that a settlement
of any amount has processed. So the tests that matter here are the ones that
prove it says no — to an absent secret, a forged signature, a tampered body,
a replay, a body too large to buffer, and an event this engine does not act
on.

Two tests cover the happy path. Ten cover the refusals, including three for
the interaction that nearly made the whole endpoint unreachable.
"""

import hashlib
import hmac
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from fastapi.testclient import TestClient

import webhook
import main


SECRET = "whsec_test_not_a_real_secret"


def sign(body: bytes, key: str = SECRET) -> str:
    return hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def event(settlement_id="setl_TEST0001", amount=1234567, event_name="settlement.processed",
          event_id="evt_0001", currency="INR") -> bytes:
    """A delivery shaped like Razorpay's: entity nested under payload."""
    return json.dumps({
        "id": event_id,
        "event": event_name,
        "contains": ["settlement"],
        "payload": {"settlement": {"entity": {
            "id": settlement_id,
            "entity": "settlement",
            "amount": amount,
            "currency": currency,
            "status": "processed",
        }}},
    }).encode()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv(webhook.WEBHOOK_SECRET_ENV, SECRET)
    webhook._reset_for_tests()
    yield TestClient(main.app)
    webhook._reset_for_tests()


@pytest.fixture
def unconfigured_client(monkeypatch):
    monkeypatch.delenv(webhook.WEBHOOK_SECRET_ENV, raising=False)
    webhook._reset_for_tests()
    yield TestClient(main.app)
    webhook._reset_for_tests()


def post(client, body: bytes, signature: str | None = None):
    headers = {"content-type": "application/json"}
    if signature is not None:
        headers[webhook.SIGNATURE_HEADER] = signature
    return client.post("/webhooks/razorpay", content=body, headers=headers)


# ── the refusals ──────────────────────────────────────────────────────────

class TestItRefuses:
    def test_no_secret_configured_is_503_not_a_silent_accept(self, unconfigured_client):
        """
        A deployment that forgot the secret must fail loudly. Returning 200
        would mean accepting unauthenticated instructions about settled money
        and looking healthy while doing it.
        """
        body = event()
        r = post(unconfigured_client, body, sign(body))
        assert r.status_code == 503
        assert r.json()["status"] == "unconfigured"
        assert not webhook.pending(), "nothing may be queued without a secret"

    def test_a_forged_signature_is_rejected(self, client):
        body = event()
        r = post(client, body, sign(body, key="wrong_secret"))
        assert r.status_code == 401
        assert r.json()["status"] == "invalid_signature"
        assert not webhook.pending()

    def test_a_missing_signature_header_is_rejected(self, client):
        r = post(client, event(), signature=None)
        assert r.status_code == 401
        assert not webhook.pending()

    def test_a_tampered_body_is_rejected(self, client):
        """
        The amount is the field an attacker would change. Signing the real
        body and then sending a different one must fail.
        """
        signed = event(amount=1234567)
        tampered = event(amount=999_999_999)
        r = post(client, tampered, sign(signed))
        assert r.status_code == 401
        assert not webhook.pending()

    def test_a_replayed_delivery_is_acknowledged_but_not_processed_twice(self, client):
        """
        Razorpay retries, so a redelivery is normal traffic and must not be
        an error — but processing it twice would record one settlement twice.
        """
        body = event()
        first = post(client, body, sign(body))
        assert first.json()["status"] == "accepted"
        second = post(client, body, sign(body))
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate"
        assert len(webhook.pending()) == 1

    def test_an_oversized_body_is_refused_before_buffering(self, client):
        big = b'{"event":"settlement.processed","pad":"' + b"x" * (webhook.WEBHOOK_MAX_BYTES + 64) + b'"}'
        r = post(client, big, sign(big))
        assert r.status_code == 413

    def test_a_verified_but_uninteresting_event_is_ignored_with_200(self, client):
        """
        200 so Razorpay stops retrying something that needs no retry, and
        nothing queued because this engine does not act on it.
        """
        body = event(event_name="payment.captured", event_id="evt_other")
        r = post(client, body, sign(body))
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"
        assert not webhook.pending()


# ── the one happy path ────────────────────────────────────────────────────

class TestItAccepts:
    def test_a_signed_settlement_event_is_queued_with_its_amount_in_paise(self, client):
        body = event(settlement_id="setl_ABC123", amount=5_500_00)
        r = post(client, body, sign(body))
        assert r.status_code == 200
        payload = r.json()
        assert payload["status"] == "accepted"
        assert payload["settlement_id"] == "setl_ABC123"
        # Razorpay sends integer paise and this engine works in paise, so the
        # value must pass through untouched. A float anywhere here is a bug.
        assert payload["amount_cents"] == 550000
        assert isinstance(payload["amount_cents"], int)

        queued = webhook.pending()
        assert [d.settlement_id for d in queued] == ["setl_ABC123"]
        assert webhook.mark_reconciled("setl_ABC123") is True
        assert not webhook.pending(), "a reconciled notification leaves the queue"

    def test_the_pending_endpoint_says_whether_the_secret_is_set(self, client):
        body = event(settlement_id="setl_PEND", event_id="evt_pend")
        post(client, body, sign(body))
        r = client.get("/webhooks/pending")
        assert r.status_code == 200
        data = r.json()
        assert data["configured"] is True
        assert data["pending"][0]["settlement_id"] == "setl_PEND"


# ── the boundary the docs must not overstate ──────────────────────────────

def test_verification_is_over_the_raw_bytes_not_reserialised_json(client):
    """
    `json.dumps(json.loads(body))` is not byte-identical to what was signed —
    key order, separators and unicode escaping all differ — so verifying a
    round-tripped body would reject every genuine delivery. This pins the
    property by signing a body whose formatting a re-serialisation would
    change.
    """
    body = b'{"event":"settlement.processed","id":"evt_raw","payload":{"settlement":{"entity":{"id":"setl_RAW","amount":100,"currency":"INR"}}}}'
    spaced = json.dumps(json.loads(body)).encode()
    assert body != spaced, "fixture must actually differ once re-serialised"
    r = post(client, body, sign(body))
    assert r.json()["status"] == "accepted"


def test_signature_comparison_is_constant_time():
    """
    `==` on strings short-circuits at the first differing byte, leaking how
    much of a guess was right through timing. Asserting on the source because
    the property is not observable from the outside.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src" / "webhook.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest" in source
    assert "== provided" not in source and "provided ==" not in source


# ── the interaction the API-key middleware nearly broke ───────────────────

class TestApiKeyInteraction:
    """
    The engine's middleware defaults to closed: every path needs an API key
    unless it is named as an exception. Razorpay has no way to send this
    deployment's key — it signs with the webhook secret instead — so leaving
    the receiving path behind the key check would 401 every genuine delivery
    BEFORE verification ran. A webhook that looks configured and silently
    receives nothing is the worst of the available outcomes.
    """

    def test_a_signed_delivery_is_accepted_even_with_the_api_key_enabled(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "k_live_engine_key")
        monkeypatch.setenv(webhook.WEBHOOK_SECRET_ENV, SECRET)
        webhook._reset_for_tests()
        try:
            client = TestClient(main.app)
            body = event(settlement_id="setl_KEYED", event_id="evt_keyed")
            r = post(client, body, sign(body))
            assert r.status_code == 200, (
                "the API-key middleware rejected a validly signed delivery; "
                "Razorpay cannot send an API key, so this endpoint must be "
                "exempt from it"
            )
            assert r.json()["status"] == "accepted"
        finally:
            webhook._reset_for_tests()

    def test_an_unsigned_delivery_is_still_refused_with_the_api_key_enabled(self, monkeypatch):
        """The exemption must not become a way around authentication entirely."""
        monkeypatch.setenv("API_KEY", "k_live_engine_key")
        monkeypatch.setenv(webhook.WEBHOOK_SECRET_ENV, SECRET)
        webhook._reset_for_tests()
        try:
            client = TestClient(main.app)
            r = post(client, event(event_id="evt_unsigned"), signature=None)
            assert r.status_code == 401
            assert not webhook.pending()
        finally:
            webhook._reset_for_tests()

    def test_the_pending_read_stays_behind_the_api_key(self, monkeypatch):
        """
        Only the receiving path is exempt. The queue of what has arrived is an
        internal read with no signature of its own.
        """
        monkeypatch.setenv("API_KEY", "k_live_engine_key")
        monkeypatch.setenv(webhook.WEBHOOK_SECRET_ENV, SECRET)
        webhook._reset_for_tests()
        try:
            client = TestClient(main.app)
            assert client.get("/webhooks/pending").status_code == 401
            ok = client.get("/webhooks/pending",
                            headers={"X-API-Key": "k_live_engine_key"})
            assert ok.status_code == 200
        finally:
            webhook._reset_for_tests()
