"""
Agent 10 — that a failed ERP push is never reported as a successful one.

This file exists because the module got that exactly backwards. An
unreachable endpoint was caught and turned into `return True` with the
journal marked "posted_mock", so a connection refused — the most likely
production failure there is — left the books showing a journal as posted
that no ERP had ever received.

A crash gets investigated. A false success gets reconciled against next
month. So most of what follows asserts on the failure paths.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
import requests

import erp_sync
from cash_position import CashPosition


class _Line:
    def __init__(self, account, debit_cents=0, credit_cents=0, memo=""):
        self.account = account
        self.debit_cents = debit_cents
        self.credit_cents = credit_cents
        self.memo = memo


class _Journal:
    def __init__(self, balanced=True, status="proposed"):
        self.entry_id = "JE-TEST-0001"
        self.date_utc = datetime(2026, 9, 12, tzinfo=timezone.utc)
        self.basis = "settlement SETL-1 cleared"
        self.is_balanced = balanced
        self.status = status
        self.lines = [
            _Line("Bank", debit_cents=98_500, memo="net received"),
            _Line("Gateway Fees", debit_cents=1_500, memo="fees"),
            _Line("Accounts Receivable", credit_cents=100_000, memo="settled"),
        ]


class _Position:
    """Stands in for CashPosition — only batch_id and journal are read."""
    def __init__(self, journal=None):
        self.batch_id = "SETL-1"
        self.journal = journal if journal is not None else _Journal()


class _Response:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(erp_sync.ERP_URL_ENV, raising=False)
    monkeypatch.delenv(erp_sync.ERP_TOKEN_ENV, raising=False)


class TestFailureIsNeverSuccess:
    def test_an_unreachable_endpoint_returns_false_and_says_not_posted(self, monkeypatch):
        """
        The regression this file is named for. Previously: True, and
        status "posted_mock".
        """
        def boom(*a, **k):
            raise requests.ConnectionError("connection refused")
        monkeypatch.setattr(erp_sync.requests, "post", boom)

        pos = _Position()
        assert erp_sync.push_to_erp(pos, erp_url="http://erp.internal/journal") is False
        assert pos.journal.status == "unreachable", (
            "a failed POST must leave a status that says so, not one that "
            "reads as posted"
        )

    def test_no_configured_target_is_distinguishable_from_unreachable(self):
        """
        The old default pointed at localhost, which made "no ERP configured"
        and "the ERP is down" the same observable state — and both returned
        True.
        """
        pos = _Position()
        assert erp_sync.push_to_erp(pos) is False
        assert pos.journal.status == "no_target_configured"

    def test_a_rejection_keeps_the_erps_own_answer(self, monkeypatch):
        monkeypatch.setattr(erp_sync.requests, "post",
                            lambda *a, **k: _Response(422, "account 4000 is closed"))
        pos = _Position()
        assert erp_sync.push_to_erp(pos, erp_url="http://erp.internal/journal") is False
        assert pos.journal.status == "rejected_by_erp"

    def test_an_unbalanced_journal_is_refused_before_it_is_sent(self, monkeypatch):
        sent = []
        monkeypatch.setattr(erp_sync.requests, "post",
                            lambda *a, **k: sent.append(1) or _Response(200))
        pos = _Position(_Journal(balanced=False))
        assert erp_sync.push_to_erp(pos, erp_url="http://erp.internal/journal") is False
        assert pos.journal.status == "skipped_unbalanced"
        assert not sent, "an unbalanced journal must not reach the ERP at all"


class TestSuccessIsLabelled:
    def test_a_real_target_accepting_it_is_posted(self, monkeypatch):
        monkeypatch.setattr(erp_sync.requests, "post", lambda *a, **k: _Response(201))
        pos = _Position()
        assert erp_sync.push_to_erp(pos, erp_url="https://erp.example.com/journal") is True
        assert pos.journal.status == "posted"

    def test_the_local_stand_in_is_labelled_as_such(self, monkeypatch):
        """
        A demo posting to localhost and a genuine posting to a general ledger
        must not leave the same status behind.
        """
        monkeypatch.setattr(erp_sync.requests, "post", lambda *a, **k: _Response(200))
        pos = _Position()
        assert erp_sync.push_to_erp(pos, erp_url=erp_sync.MOCK_ERP_URL) is True
        assert pos.journal.status == "posted_to_mock"


class TestPayload:
    def test_amounts_carry_exact_paise_and_never_pass_through_a_float(self):
        """
        The engine is integer paise throughout. `round(cents / 100, 2)` puts
        binary floating point at the one boundary where a cent must not move,
        so the decimal is built by integer division and the exact integer is
        sent alongside it.
        """
        payload = erp_sync.build_payload(_Position())
        bank = next(l for l in payload["lines"] if l["account"] == "Bank")
        assert bank["debit"] == "985.00"
        assert bank["debit_paise"] == 98_500
        assert isinstance(bank["debit_paise"], int)
        for line in payload["lines"]:
            for key in ("debit", "credit"):
                assert isinstance(line[key], str), "money must not serialise as a float"

    def test_debits_equal_credits_in_what_is_actually_sent(self):
        payload = erp_sync.build_payload(_Position())
        debits = sum(l["debit_paise"] for l in payload["lines"])
        credits = sum(l["credit_paise"] for l in payload["lines"])
        assert debits == credits == 100_000

    def test_a_bearer_token_is_sent_when_configured(self, monkeypatch):
        monkeypatch.setenv(erp_sync.ERP_TOKEN_ENV, "erp_tok_abc")
        seen = {}

        def capture(url, json=None, headers=None, timeout=None):
            seen.update(headers or {})
            return _Response(200)

        monkeypatch.setattr(erp_sync.requests, "post", capture)
        erp_sync.push_to_erp(_Position(), erp_url="https://erp.example.com/journal")
        assert seen.get("Authorization") == "Bearer erp_tok_abc"


def test_the_old_false_success_cannot_come_back():
    """
    The bug had a specific shape: an exception handler that returned True.
    No handler in this module may do that again.

    Parsed with `ast`, not scanned as text. A text scan flagged this file's
    own module docstring, which quotes the old handler verbatim so the
    failure is documented where someone will read it — and a test that
    cannot tell code from prose about code would force that documentation to
    be deleted to stay green. The wrong fix for a false positive is removing
    the evidence.
    """
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src" / "erp_sync.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Return) and isinstance(inner.value, ast.Constant)
                    and inner.value.value is True):
                offenders.append(inner.lineno)
    assert not offenders, (
        f"an except handler returns True at line(s) {offenders} — a failed "
        f"push reporting success is the regression this module was rewritten "
        f"for"
    )

    # Same reasoning, same method: the status string only in real code.
    assigned = {
        n.value.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
    }
    assert "posted_mock" not in assigned, (
        "the status that used to mean 'we reached nothing but are calling it "
        "posted' must not return"
    )
