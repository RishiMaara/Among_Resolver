"""
The investigator proposes; the verifier decides whether anyone sees it.

Every model path here is mocked. What the tests pin is the part that has to
hold whoever proposes: a match that is not this settlement's arithmetic, a
wait that ends on a holiday, a write-off that is not the residual, a reason
that cites a figure the case does not contain — each is rejected before a
reviewer sees it. And the true set is never rejected.
"""

from __future__ import annotations

import json
import random
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import investigation_agent as inv
import llm_provider
import main
import settled_ledger
from orchestrator import reconcile_batch
from subset_sum import SubsetSumConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from benchmark import build_scenario  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[2] / "public" / "sample-data"
CFG = SubsetSumConfig(tolerance_cents=10, solver_time_limit_s=5.0,
                      ambiguity_probe_limit=2, num_search_workers=1)


@pytest.fixture(scope="module")
def withheld():
    """A benchmark settlement the engine will not clear: only some members name it."""
    for seed in range(40):
        sc = build_scenario(900 + seed, "ref_partial", True, "sparse", random.Random(seed))
        report = reconcile_batch(sc.batch, sc.candidates, subset_config=CFG, settlement_window_days=5)
        if not report.match_result.cleared:
            case = inv.build_case(sc.batch, sc.candidates, report,
                                  tolerance_cents=CFG.tolerance_cents, workers=1)
            return sc, report, case
    pytest.skip("no withheld ref_partial scenario in 40 seeds")


@pytest.fixture
def no_model(monkeypatch):
    monkeypatch.setattr(llm_provider, "is_configured", lambda: False)


def match(ids, reason="These transactions sum to the target."):
    return inv.Proposal("MATCH_PROPOSAL", txn_ids=list(ids), reason=reason)


class TestTheCase:
    def test_it_carries_what_a_reviewer_needs(self, withheld):
        _, _, case = withheld
        public = inv._public(case)
        assert {"target_cents", "tolerance_cents", "engine_proposal", "alternatives",
                "exceptions", "next_working_day"} <= set(public)
        assert "_pool" not in public, "the model sees the case, not the raw pool index"

    def test_alternatives_really_reach_the_target(self, withheld):
        _, _, case = withheld
        for alt in case["alternatives"]:
            total = sum(r["amount_cents"] for r in alt)
            assert abs(total - case["target_cents"]) <= case["tolerance_cents"]


class TestTheVerifier:
    def test_the_true_set_passes(self, withheld):
        sc, _, case = withheld
        assert inv.verify(match(sorted(sc.truth_ids)), case)["valid"]

    def test_an_id_outside_the_pool_is_rejected(self, withheld):
        sc, _, case = withheld
        v = inv.verify(match(sorted(sc.truth_ids) + ["INVENTED-1"]), case)
        assert not v["valid"] and any("not in this settlement's pool" in f for f in v["failed"])

    def test_a_transaction_named_twice_is_rejected(self, withheld):
        sc, _, case = withheld
        ids = sorted(sc.truth_ids)
        assert not inv.verify(match(ids + ids[:1]), case)["valid"]

    def test_a_set_that_misses_the_target_is_rejected(self, withheld):
        sc, _, case = withheld
        v = inv.verify(match(sorted(sc.truth_ids)[:-1]), case)
        assert not v["valid"] and any("from the target" in f for f in v["failed"])

    def test_a_set_already_paid_out_elsewhere_is_rejected(self, withheld):
        sc, _, case = withheld
        ids = sorted(sc.truth_ids)
        settled_ledger.record_settled("SOMEONE-ELSES-PAYOUT", ids[:1])
        try:
            v = inv.verify(match(ids), case)
            assert not v["valid"] and any("already paid out" in f for f in v["failed"])
        finally:
            # The ledger is durable by design; this claim must not follow the
            # module's shared case into the tests after this one.
            conn = settled_ledger._db()
            if conn is not None:
                conn.execute("DELETE FROM settled_payments WHERE batch_id = ?",
                             ("SOMEONE-ELSES-PAYOUT",))
                conn.commit()
            settled_ledger._memory.pop(ids[0], None)

    def test_a_wait_ending_on_a_holiday_is_rejected(self, withheld):
        _, _, case = withheld
        case = dict(case, settled_on="2026-09-10")
        v = inv.verify(inv.Proposal("WAIT_FOR_DATA", until_date="2026-09-12",
                                    reason="Wait for the counterpart."), case)
        assert not v["valid"] and any("second Saturday" in f for f in v["failed"])

    def test_a_wait_of_weeks_is_rejected(self, withheld):
        _, _, case = withheld
        case = dict(case, settled_on="2026-09-01")
        v = inv.verify(inv.Proposal("WAIT_FOR_DATA", until_date="2026-10-15",
                                    reason="Wait for the counterpart."), case)
        assert not v["valid"]

    def test_a_write_off_must_be_the_residual_and_small(self, withheld):
        _, _, case = withheld
        case = dict(case, residual_cents=40)
        assert inv.verify(inv.Proposal("WRITE_OFF_ROUNDING", amount_cents=40,
                                       reason="Rounding."), case)["valid"]
        assert not inv.verify(inv.Proposal("WRITE_OFF_ROUNDING", amount_cents=39,
                                           reason="Rounding."), case)["valid"]
        big = dict(case, residual_cents=5_000)
        assert not inv.verify(inv.Proposal("WRITE_OFF_ROUNDING", amount_cents=5_000,
                                           reason="Rounding."), big)["valid"]

    def test_a_request_needs_someone_who_can_answer(self, withheld):
        _, _, case = withheld
        v = inv.verify(inv.Proposal("REQUEST_SOURCE", party="the universe",
                                    request="the report", reason="Ask."), case)
        assert not v["valid"]

    def test_an_arithmetic_tie_is_not_evidence(self, withheld):
        """
        Another listed set reaching the same target, and nothing more naming
        the settlement in the chosen one: that is a guess, and it is escalated.
        """
        sc, _, case = withheld
        truth = sorted(sc.truth_ids)
        rival = [i for i, v in case["_pool"].items()
                 if i not in sc.truth_ids and v["feed"] == case["member_feed"]][:3]
        tied = dict(case, _sets=[truth, rival],
                    _pool={i: dict(v, named=False) for i, v in case["_pool"].items()})
        v = inv.verify(match(truth), tied)
        assert not v["valid"] and any("no more evidence" in f for f in v["failed"])

    def test_more_evidence_than_every_rival_passes(self, withheld):
        sc, _, case = withheld
        truth = sorted(sc.truth_ids)
        rival = [i for i, v in case["_pool"].items()
                 if i not in sc.truth_ids and v["feed"] == case["member_feed"]][:3]
        pool = {i: dict(v, named=i in sc.truth_ids) for i, v in case["_pool"].items()}
        assert inv.verify(match(truth), dict(case, _sets=[truth, rival], _pool=pool))["valid"]

    def test_a_reason_citing_an_invented_figure_is_rejected(self, withheld):
        sc, _, case = withheld
        v = inv.verify(match(sorted(sc.truth_ids), reason="They sum to Rs 9,87,654.32."), case)
        assert not v["valid"] and any("not in the case" in f for f in v["failed"])


class TestTheProposers:
    def test_without_a_model_the_rules_propose(self, withheld, no_model):
        sc, report, _ = withheld
        out = inv.investigate(sc.batch, sc.candidates, report, use_model=True)
        assert out["proposal"]["proposer"] == "rules"

    def test_the_model_proposal_is_verified_like_any_other(self, withheld, monkeypatch):
        sc, report, _ = withheld
        monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
        monkeypatch.setattr(llm_provider, "generate", lambda *a, **k: json.dumps(
            {"action": "MATCH_PROPOSAL", "txn_ids": ["INVENTED-9"], "reason": "Looks right."}))
        out = inv.investigate(sc.batch, sc.candidates, report, use_model=True)
        assert out["proposal"]["proposer"] == "model"
        assert not out["verification"]["valid"]
        assert out["verification"]["plain"].startswith("REJECTED")

    def test_a_malformed_model_answer_falls_back_to_rules(self, withheld, monkeypatch):
        sc, report, _ = withheld
        monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
        monkeypatch.setattr(llm_provider, "generate", lambda *a, **k: '{"action": "DELETE_LEDGER"}')
        out = inv.investigate(sc.batch, sc.candidates, report, use_model=True)
        assert out["proposal"]["proposer"] == "rules"

    def test_a_cleared_settlement_has_nothing_to_investigate(self):
        sc = build_scenario(7, "clean", True, "sparse", random.Random(1))
        report = reconcile_batch(sc.batch, sc.candidates, subset_config=CFG, settlement_window_days=5)
        if report.match_result.cleared:
            assert inv.investigate(sc.batch, sc.candidates, report) is None


class TestInTheApp:
    def test_an_upload_can_ask_for_an_investigation(self, no_model):
        c = TestClient(main.app)
        files = {
            "gateway_file": ("g.csv", (SAMPLES / "gateway_report.csv").read_bytes(), "text/csv"),
            "bank_file": ("b.csv", (SAMPLES / "bank_statement.csv").read_bytes(), "text/csv"),
            "erp_file": ("e.json", (SAMPLES / "erp_ledger.json").read_bytes(), "application/json"),
        }
        r = c.post("/reconcile/upload", data={
            "batch_id": "INVESTIGATE-ME", "net_amount": "66466.36",
            "settled_at": "2026-09-02T00:00:00Z", "settlement_window_days": "5",
            "currency": "INR", "declared_deductions": "2055.66", "member_source": "gateway",
            "investigate": "true"}, files=files)
        body = r.json()
        assert not body["summary"]["cleared"]
        found = body["investigation"]
        assert found["proposal"]["action"] in inv.ACTIONS
        assert "valid" in found["verification"]
        import audit
        assert any(e["agent"] == "investigator"
                   for e in audit.get_audit_trail("INVESTIGATE-ME")), "the proposal is on the record"


@pytest.fixture(scope="module")
def undeclared():
    """The demo's withheld preset: the sample, with no member feed declared."""
    captured = {}
    real = inv.investigate

    def spy(batch, candidates, report, use_model=False):
        captured.update(batch=batch, candidates=candidates, report=report)
        return real(batch, candidates, report, use_model=False)

    mp = pytest.MonkeyPatch()
    mp.setattr(inv, "investigate", spy)
    try:
        files = {
            "gateway_file": ("g.csv", (SAMPLES / "gateway_report.csv").read_bytes(), "text/csv"),
            "bank_file": ("b.csv", (SAMPLES / "bank_statement.csv").read_bytes(), "text/csv"),
            "erp_file": ("e.json", (SAMPLES / "erp_ledger.json").read_bytes(), "application/json"),
        }
        body = TestClient(main.app).post("/reconcile/upload", data={
            "batch_id": "SETTLE-001", "net_amount": "66466.36",
            "settled_at": "2026-09-02T00:00:00Z", "settlement_window_days": "5",
            "currency": "INR", "declared_deductions": "2055.66",
            "investigate": "true"}, files=files).json()
    finally:
        mp.undo()
    case = inv.build_case(captured["batch"], captured["candidates"], captured["report"],
                          workers=1)
    return body, case


def feed_case(target_cents, declared=False, sets=()):
    """A small case with one payment recorded in both feeds (FC_pay_1 / FC_JV1)."""
    pool = {
        "FC_pay_1": {"amount_cents": 1000, "currency": "INR", "named": True,
                     "feed": "gateway", "ref": "ORD1"},
        "FC_JV1": {"amount_cents": 1000, "currency": "INR", "named": True,
                   "feed": "erp", "ref": "ORD1"},
        "FC_pay_2": {"amount_cents": 2000, "currency": "INR", "named": True,
                     "feed": "gateway", "ref": "ORD2"},
        "FC_pay_3": {"amount_cents": 2000, "currency": "INR", "named": False,
                     "feed": "gateway", "ref": "ORD3"},
    }
    return {"batch_id": "FEED-CASE", "currency": "INR", "target_cents": target_cents,
            "tolerance_cents": 5, "residual_cents": 0, "settled_on": "2026-09-02",
            "withheld_reason": "alternate_subset", "confidence": 0.3,
            "member_feed": "gateway", "member_feed_declared": declared,
            "engine_proposal": [], "engine_proposal_sum_cents": 0, "alternatives": [],
            "alternative_sums_cents": [], "pool_size": 3, "exceptions": [],
            "next_working_day": "2026-09-03", "_pool": pool, "_sets": [list(x) for x in sets]}


class TestTheMemberFeed:
    """FAILURE_LOG 30: what a set may be drawn from, checked on a case built by hand."""

    @pytest.fixture(autouse=True)
    def nothing_paid_out(self, monkeypatch):
        monkeypatch.setattr(settled_ledger, "owners", lambda ids: {})

    def test_a_payment_counted_in_both_feeds_is_rejected(self):
        # Sums to the target exactly, and is still one payment counted twice.
        verdict = inv.verify(match(["FC_pay_1", "FC_JV1"]), feed_case(2000))
        failed = " ".join(verdict["failed"])
        assert not verdict["valid"]
        assert "1 payment(s) counted twice" in failed and "FC_JV1 and FC_pay_1" in failed

    def test_a_record_from_another_feed_is_rejected(self):
        verdict = inv.verify(match(["FC_JV1", "FC_pay_2"]), feed_case(3000))
        failed = " ".join(verdict["failed"])
        assert "not from the gateway feed" in failed and "declare it and re-run" in failed
        declared = inv.verify(match(["FC_JV1", "FC_pay_2"]), feed_case(3000, declared=True))
        assert "declare it" not in " ".join(declared["failed"])

    def test_a_ledger_copy_is_read_as_the_payment_it_copies(self):
        # The listed set holds FC_pay_1's ledger booking. Read as the payments
        # it stands for, it IS the chosen set, so it cannot tie it — on CI it
        # did, and the true set was rejected.
        case = feed_case(3000, sets=[["FC_JV1", "FC_pay_2"]])
        assert inv.verify(match(["FC_pay_1", "FC_pay_2"]), case)["valid"]

    def test_a_ledger_copy_still_carries_its_payments_evidence(self):
        # Dropping such a set instead let 4 more wrong proposals through.
        case = feed_case(3000, sets=[["FC_JV1", "FC_pay_2"]])
        verdict = inv.verify(match(["FC_pay_1", "FC_pay_3"]), case)
        assert not verdict["valid"] and "escalate" in " ".join(verdict["failed"])

    def test_an_equal_rival_inside_the_feed_still_forces_escalation(self):
        case = feed_case(3000, sets=[["FC_pay_1", "FC_pay_3"]])
        rival_named_too = dict(case["_pool"])
        rival_named_too["FC_pay_3"] = {**rival_named_too["FC_pay_3"], "named": True}
        case["_pool"] = rival_named_too
        verdict = inv.verify(match(["FC_pay_1", "FC_pay_2"]), case)
        assert not verdict["valid"] and "escalate" in " ".join(verdict["failed"])


class TestAnUndeclaredMemberFeed:
    """The demo's withheld preset. Which set the engine proposes differs by
    machine (parallel CP-SAT), so these assert only what holds on any of them."""

    def test_the_case_shows_every_record_the_engine_proposed(self, undeclared):
        body, case = undeclared
        assert not body["summary"]["cleared"]
        shown = [r["id"] for r in case["engine_proposal"]]
        assert sorted(shown) == sorted(body["matched_txn_ids"])
        assert all(r["feed"] in ("gateway", "erp") for r in case["engine_proposal"])
        assert case["member_feed"] == "gateway" and not case["member_feed_declared"]

    def test_the_engines_set_is_stopped_when_it_leaves_the_member_feed(self, undeclared):
        body, case = undeclared
        off_feed = [r for r in case["engine_proposal"] if r["feed"] != "gateway"]
        if not off_feed:
            pytest.skip("this machine's solver proposed a set inside the member feed")
        verdict = inv.verify(match(body["matched_txn_ids"]), case)
        assert not verdict["valid"]
        assert "declare it and re-run" in " ".join(verdict["failed"])

    def test_the_payout_that_every_record_names_still_passes(self, undeclared, monkeypatch):
        _, case = undeclared
        # Other tests clear this sample under other batch ids; whether these
        # payments were paid out elsewhere is not what this checks.
        monkeypatch.setattr(settled_ledger, "owners", lambda ids: {})
        truth = [f"pay_{i:04d}" for i in range(14)]
        verdict = inv.verify(match(truth), case)
        assert verdict["valid"], verdict["failed"]
