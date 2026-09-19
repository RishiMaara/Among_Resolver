"""
The AI's answer is checked after it is written, not just instructed before.

The Q&A system prompt already forbids inventing figures. These tests pin the
part that does not depend on the model obeying: every figure, identifier and
date in an answer must trace back to the settlement's recorded results, or
the answer is withheld and the reviewer gets the engine's own summary.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

import audit
import grounding_check
import settlement_qa

GROUNDING = {
    "batch_id": "SETTLE-001",
    "summary": {
        "batch_id": "SETTLE-001", "cleared": True, "matched_count": 14,
        "confidence": 0.95, "target_cents": 6646636, "net_amount_cents": 10000000,
        "tie_out_residual_cents": 0, "settled_at": "2026-09-02T00:00:00Z",
    },
    "matched_txn_ids_sample": ["p0003", "p0007", "T1"],
    "exceptions": [{"reason": "compliance_block", "diagnosis_note": "pay_x: ceiling exceeded"}],
    "exception_total": 2,
    "decision_trail": [{"agent": "fee_decomposition",
                        "detail": "net=100c -> gross_target=103c"}],
}


def check(answer, question=""):
    return grounding_check.verify(answer, GROUNDING, question)


class TestWhatPasses:
    def test_figures_quoted_in_their_everyday_units_trace_back(self):
        v = check("14 payments matched at confidence 0.95 (95%), totalling "
                  "₹66,466.36, with a residual of 0.")
        assert v.ok, v.describe()
        assert v.checked >= 4

    def test_a_rounded_amount_is_still_the_recorded_amount(self):
        assert check("It came to roughly ₹66,466.").ok

    def test_indian_digit_grouping_is_read(self):
        assert check("The net settlement was ₹1,00,000.").ok

    def test_figures_inside_the_engines_own_text_count(self):
        """'103c' in a trail entry grounds both '103 paise' and '₹1.03'."""
        assert check("The gross target was 103 paise, i.e. ₹1.03.").ok

    def test_ids_and_dates_that_appear_in_the_results_pass(self):
        assert check("p0003 and p0007 matched; SETTLE-001 settled on 2026-09-02.").ok

    def test_what_the_person_asked_about_may_be_repeated(self):
        v = check("pay_ZZ99 does not appear in this settlement.",
                  question="What happened to pay_ZZ99?")
        assert v.ok, v.describe()

    def test_layout_and_ordinals_are_not_claims(self):
        assert check("1. It cleared.\n2. 14 matched.\nThe 2nd exception is a block.").ok


class TestWhatIsCaught:
    def test_an_invented_amount(self):
        v = check("The settlement was ₹70,000.")
        assert not v.ok
        assert "70,000" in v.ungrounded_figures

    def test_an_invented_transaction_id(self):
        v = check("Payment p9999 was excluded.")
        assert not v.ok
        assert "p9999" in v.ungrounded_ids

    def test_an_invented_date(self):
        v = check("It settled on 2026-10-01.")
        assert not v.ok

    def test_a_confidence_that_was_not_recorded(self):
        """'90%' is not a rounding of 0.95 — a model 'simplifying' is caught."""
        assert not check("The engine was 90% confident.").ok

    def test_an_invented_total(self):
        """A sum the engine never computed, however plausible, does not trace."""
        assert not check("Across both, ₹1,66,466.36 moved.").ok


# ── end to end: the reviewer never sees an untraceable answer ─────────────

@pytest.fixture
def seeded(monkeypatch):
    bid = "B-QA-grounded"
    audit.clear_trail(bid)
    audit.log_decision(bid, "subset_sum", "Exact match found.")
    report = {"summary": {"batch_id": bid, "cleared": True, "matched_count": 3,
                          "confidence": 0.95, "target_cents": 123456,
                          "tie_out_residual_cents": 0},
              "matched_txn_ids": ["T1", "T2", "T3"], "exceptions": []}
    settlement_qa.store_result(bid, report)
    monkeypatch.setattr(settlement_qa, "is_enabled", lambda: True)
    yield bid
    audit.clear_trail(bid)


def _model_says(monkeypatch, text):
    monkeypatch.setattr(settlement_qa.llm_provider, "generate", lambda *a, **k: text)


def test_a_grounded_answer_is_shown_and_says_how_much_was_checked(seeded, monkeypatch):
    _model_says(monkeypatch, "It cleared: 3 payments (T1, T2, T3) totalling ₹1,234.56.")
    r = settlement_qa.answer_question(seeded, "Did it clear?")
    assert r["grounded"] is True
    assert "₹1,234.56" in r["answer"]
    assert r["checked_figures"] >= 4


def test_an_answer_with_an_invented_amount_is_withheld(seeded, monkeypatch):
    _model_says(monkeypatch, "It cleared: 3 payments totalling ₹9,99,999.")
    r = settlement_qa.answer_question(seeded, "Did it clear?")
    assert r["grounded"] is False and r["withheld"] is True
    assert "9,99,999" in r["ungrounded"]
    assert "9,99,999" in r["answer"] and "not shown" in r["answer"], (
        "the reviewer must be told WHY, not handed silence"
    )
    trail = audit.get_audit_trail(seeded)
    assert any("WITHHELD" in e["detail"] for e in trail), "a withheld answer must be on the record"


def test_a_withheld_answer_still_leaves_the_reviewer_something_true(seeded, monkeypatch):
    _model_says(monkeypatch, "Payment pay_FAKE01 settled ₹50.")
    r = settlement_qa.answer_question(seeded, "What settled?")
    assert r["withheld"] is True
    assert "What the engine recorded" in r["answer"]


def test_the_value_not_meaning_limit_is_real_and_documented():
    """
    Pinning the edge the docstring admits: a real number attached to the wrong
    noun passes. If this ever starts failing, the check got stronger and the
    documentation should say so.
    """
    assert check("There were 14 exceptions.").ok      # 14 is the match count
    assert "VALUE, not by meaning" in grounding_check.__doc__
