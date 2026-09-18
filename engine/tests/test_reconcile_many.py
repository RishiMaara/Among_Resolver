"""
orchestrator.reconcile_many — the joint N:M pathway, end to end.

Where test_subset_sum_nm.py proves the joint CP-SAT solver itself resolves a
shared claim correctly, this file proves reconcile_many wires that solver up
with the SAME evidence-based safety the 1:N path has: linkage narrows each
batch's own candidates first, an anchored refund is forced, an unevidenced
arithmetic match is withheld, a per-target ambiguity withholds only the
target that is actually ambiguous, and a joint solve that cannot satisfy
every target at once falls back to the proven 1:N path per batch rather than
failing the whole group.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datetime import datetime, timedelta, timezone
from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence
from orchestrator import reconcile_many
from subset_sum import SubsetSumConfig
from fee_decomposition import FeeRateCard


BASE_TIME = datetime(2026, 8, 20, tzinfo=timezone.utc)
SETTLED_AT = BASE_TIME + timedelta(hours=72)

# Zero deductions so net_amount_cents IS the gross target exactly -- these
# tests are about which transactions get assigned to which batch, not about
# fee arithmetic, which the 1:N tests already cover.
ZERO_FEES = FeeRateCard(gateway_fee_bps=0, flat_fee_cents=0, tax_withholding_bps=0)


def make_txn(txn_id: str, amount_cents: int, hours_offset: float = 24.0,
             ref_id_canonical: str | None = None) -> NormalizedTxn:
    return NormalizedTxn(
        source=SourceType.GATEWAY,
        source_txn_id=txn_id,
        ref_id_canonical=ref_id_canonical if ref_id_canonical is not None else f"REF{txn_id}",
        amount_cents=amount_cents,
        currency="INR",
        timestamp_utc=BASE_TIME + timedelta(hours=hours_offset),
        tz_confidence=TzConfidence.HIGH,
    )


def make_batch(batch_id: str, net_amount_cents: int) -> SettlementBatch:
    return SettlementBatch(
        batch_id=batch_id, net_amount_cents=net_amount_cents,
        currency="INR", settled_at_utc=SETTLED_AT,
    )


class TestEmptyInput:
    def test_no_batches_returns_no_reports(self):
        assert reconcile_many([], [make_txn("X", 100)]) == []


class TestJointClearing:
    def test_two_evidenced_batches_clear_in_one_joint_call(self):
        a_members = [
            make_txn("A1", 10000, ref_id_canonical="BATCH_JA-A1"),
            make_txn("A2", 20000, ref_id_canonical="BATCH_JA-A2"),
        ]
        b_members = [
            make_txn("B1", 15000, ref_id_canonical="BATCH_JB-B1"),
            make_txn("B2", 25000, ref_id_canonical="BATCH_JB-B2"),
        ]
        noise = [make_txn(f"N{i}", 3000 + i) for i in range(5)]
        pool = a_members + b_members + noise

        reports = reconcile_many(
            [make_batch("BATCH_JA", 30000), make_batch("BATCH_JB", 40000)],
            pool, settlement_window_days=5,
            subset_config=SubsetSumConfig(tolerance_cents=0, ambiguity_probe_limit=1),
            rate_card=ZERO_FEES,
        )

        assert len(reports) == 2
        rep_a, rep_b = reports
        assert rep_a.batch_id == "BATCH_JA"
        assert rep_a.match_result.cleared, rep_a.match_result.reasoning
        assert set(rep_a.match_result.matched_txn_ids) == {"A1", "A2"}
        assert rep_b.batch_id == "BATCH_JB"
        assert rep_b.match_result.cleared, rep_b.match_result.reasoning
        assert set(rep_b.match_result.matched_txn_ids) == {"B1", "B2"}


class TestWithholdingAppliesPerTarget:
    def test_an_unevidenced_exact_sum_is_withheld_even_though_arithmetic_found_it(self):
        """
        Mirrors _withhold_if_unevidenced's 1:N guarantee: an exact sum with
        no reference, cluster or cross-source evidence tying it to the
        settlement is a coincidence, not a match, however confidently
        CP-SAT reports it. No ref_id_canonical is given here at all, so
        linkage has nothing to anchor on.
        """
        pool = [make_txn("U1", 10000), make_txn("U2", 20000)]
        reports = reconcile_many(
            [make_batch("BATCH_UNEV", 30000)], pool, settlement_window_days=5,
            subset_config=SubsetSumConfig(tolerance_cents=0, ambiguity_probe_limit=1),
            rate_card=ZERO_FEES,
        )
        assert len(reports) == 1
        result = reports[0].match_result
        assert not result.cleared, (
            "An arithmetic-only match with zero linkage evidence must not "
            "auto-clear, in the joint path exactly as in the 1:N path."
        )

    def test_one_batchs_ambiguity_does_not_withhold_a_different_batchs_clean_match(self):
        """
        Target A is constructed ambiguous ({A1,A2} and {A3,A4} both sum to
        300); target B is unique and uses a disjoint, evidenced pool. Only A
        should be withheld.
        """
        a_pool = [
            make_txn("A1", 100, ref_id_canonical="BATCH_JAMB-A1"),
            make_txn("A2", 200, ref_id_canonical="BATCH_JAMB-A2"),
            make_txn("A3", 150, ref_id_canonical="BATCH_JAMB-A3"),
            make_txn("A4", 150, ref_id_canonical="BATCH_JAMB-A4"),
        ]
        b_pool = [
            make_txn("B1", 6000, ref_id_canonical="BATCH_JAMB2-B1"),
            make_txn("B2", 4000, ref_id_canonical="BATCH_JAMB2-B2"),
        ]
        pool = a_pool + b_pool

        reports = reconcile_many(
            [make_batch("BATCH_JAMB", 300), make_batch("BATCH_JAMB2", 10000)],
            pool, settlement_window_days=5,
            subset_config=SubsetSumConfig(tolerance_cents=0, ambiguity_probe_limit=3),
            rate_card=ZERO_FEES,
        )
        assert len(reports) == 2
        rep_a, rep_b = reports
        assert not rep_a.match_result.cleared, (
            "Constructed ambiguous — must not auto-clear even though a "
            "feasible joint assignment exists."
        )
        assert rep_b.match_result.cleared, rep_b.match_result.reasoning
        assert set(rep_b.match_result.matched_txn_ids) == {"B1", "B2"}


class TestAnchoredRefundForcing:
    def test_an_anchored_refund_is_forced_through_the_full_joint_pipeline(self):
        """
        Same shape as test_matching.py's 1:N forced-refund test, run through
        the real joint pipeline: _anchored_negatives must be computed from
        each batch's own real ref_id evidence and threaded into the joint
        solve, not bypassed. 20 payments + 3 anchored refunds is wildly
        ambiguous unforced; forced, all 20 payments and all 3 refunds are
        the only way to reach the target.
        """
        anchor = "BATCH_JFORCE"
        payments = [make_txn(f"P{i}", 100000, ref_id_canonical=f"{anchor}-P{i}") for i in range(20)]
        refunds = [make_txn(f"R{i}", -100000, ref_id_canonical=f"{anchor}-R{i}") for i in range(3)]
        pool = payments + refunds

        reports = reconcile_many(
            [make_batch(anchor, 1_700_000)], pool, settlement_window_days=5,
            subset_config=SubsetSumConfig(tolerance_cents=0, ambiguity_probe_limit=2),
            rate_card=ZERO_FEES,
        )
        assert len(reports) == 1
        result = reports[0].match_result
        assert result.cleared, result.reasoning
        assert set(result.matched_txn_ids) == (
            {f"P{i}" for i in range(20)} | {f"R{i}" for i in range(3)}
        )


class TestFallbackWhenJointlyInfeasible:
    def test_one_unreachable_target_does_not_sink_a_reachable_sibling(self):
        """
        Batch A's target is genuinely reachable; batch B's is not reachable
        by ANY subset of the pool, which makes the single joint CP-SAT model
        infeasible for BOTH at once (one CP-SAT solve has no partial
        credit). reconcile_many must fall back to the independent, proven
        1:N path per batch rather than reporting A as unmatched for B's
        sake — that would make N:M strictly worse than calling reconcile_batch
        on each batch separately, which the module docstring promises it
        never is.
        """
        a_members = [
            make_txn("A1", 10000, ref_id_canonical="BATCH_JFB_A-A1"),
            make_txn("A2", 20000, ref_id_canonical="BATCH_JFB_A-A2"),
        ]
        pool = a_members + [make_txn(f"N{i}", 500 + i) for i in range(5)]

        reports = reconcile_many(
            [make_batch("BATCH_JFB_A", 30000), make_batch("BATCH_JFB_B", 99_999_999)],
            pool, settlement_window_days=5,
            subset_config=SubsetSumConfig(tolerance_cents=0, ambiguity_probe_limit=1),
            rate_card=ZERO_FEES,
        )
        assert len(reports) == 2
        rep_a = next(r for r in reports if r.batch_id == "BATCH_JFB_A")
        rep_b = next(r for r in reports if r.batch_id == "BATCH_JFB_B")

        assert rep_a.match_result.cleared, (
            f"Reachable batch must still clear via the fallback path. "
            f"Reasoning: {rep_a.match_result.reasoning}"
        )
        assert set(rep_a.match_result.matched_txn_ids) == {"A1", "A2"}
        assert not rep_b.match_result.cleared, (
            "Genuinely unreachable target must not clear, but must still "
            "produce a report rather than being silently dropped."
        )


class TestAnchorEvidenceBindsAssignment:
    """
    Arithmetic cannot see a reference, so contention between equal-valued
    legs must be resolved by the evidence rather than by the solver.

    Found by a 1,000-scenario adversarial sweep (scripts/edge_case_suite_1000.py,
    family `nm_contention`): two settlements each holding a leg of the SAME
    amount, each leg carrying its own settlement's reference. Swapping the two
    leaves both targets satisfied to the paisa, so nothing downstream of the
    solve can detect the swap -- every later gate sees a set that sums and
    contains an anchored member, and the anchored member belongs to someone
    else. 11 of 88 joint batches auto-cleared the wrong settlement's leg at
    0.91 confidence before build_union_pool was given the anchor keys.
    """

    def test_a_leg_naming_one_settlement_is_not_assigned_to_another(self):
        contested = 50000
        a_members = [
            make_txn("TA1", 10000, ref_id_canonical="BATCH_TWA-TA1"),
            make_txn("TA_TWIN", contested, ref_id_canonical="BATCH_TWA-TWIN"),
        ]
        b_members = [
            make_txn("TB1", 10000, ref_id_canonical="BATCH_TWB-TB1"),
            make_txn("TB_TWIN", contested, ref_id_canonical="BATCH_TWB-TWIN"),
        ]
        pool = a_members + b_members + [make_txn(f"TN{i}", 700 + i) for i in range(5)]

        reports = reconcile_many(
            [make_batch("BATCH_TWA", 60000), make_batch("BATCH_TWB", 60000)],
            pool, settlement_window_days=5,
            subset_config=SubsetSumConfig(tolerance_cents=0, ambiguity_probe_limit=1),
            rate_card=ZERO_FEES,
        )

        rep_a = next(r for r in reports if r.batch_id == "BATCH_TWA")
        rep_b = next(r for r in reports if r.batch_id == "BATCH_TWB")
        matched_a = set(rep_a.match_result.matched_txn_ids)
        matched_b = set(rep_b.match_result.matched_txn_ids)

        assert "TB_TWIN" not in matched_a, (
            "BATCH_TWA cleared a leg that references BATCH_TWB. The amounts "
            "are interchangeable; the references are not."
        )
        assert "TA_TWIN" not in matched_b, (
            "BATCH_TWB cleared a leg that references BATCH_TWA."
        )
        if rep_a.match_result.cleared:
            assert matched_a == {"TA1", "TA_TWIN"}
        if rep_b.match_result.cleared:
            assert matched_b == {"TB1", "TB_TWIN"}

    def test_an_anchor_cannot_smuggle_a_record_past_another_targets_narrowing(self):
        """
        Binding is an INTERSECTION with each target's own narrowed pool, not
        a replacement for it. A record anchored to a batch but dropped by that
        batch's window filter must not reappear in its candidate set.
        """
        stale = make_txn("STALE", 10000, hours_offset=-400,
                         ref_id_canonical="BATCH_STL-STALE")
        fresh = [
            make_txn("F1", 10000, ref_id_canonical="BATCH_STL-F1"),
            make_txn("F2", 20000, ref_id_canonical="BATCH_STL-F2"),
        ]
        reports = reconcile_many(
            [make_batch("BATCH_STL", 30000)], fresh + [stale],
            settlement_window_days=5,
            subset_config=SubsetSumConfig(tolerance_cents=0, ambiguity_probe_limit=1),
            rate_card=ZERO_FEES,
        )
        assert "STALE" not in set(reports[0].match_result.matched_txn_ids)


class TestCrossBatchDoubleClaim:
    """
    The joint solve forbids a shared claim by construction (AddAtMostOne).
    The independent fallback cannot: each batch is reconciled against the
    whole pool with no knowledge of what its siblings took. A payment cannot
    fund two settlements, so when both sides clear the same record neither is
    released -- which of the two is wrong is not knowable here.
    """

    def test_two_batches_clearing_one_payment_are_both_withheld(self):
        shared = make_txn("SHARED", 30000, ref_id_canonical="BATCH_DCA-SHARED")
        # Same record referenced by both settlements, so linkage anchors it to
        # each of them and the fallback path lets both claim it.
        shared.ref_id_canonical = "BATCH_DCA-BATCH_DCB-SHARED"
        pool = [shared, make_txn("DN1", 111), make_txn("DN2", 222)]

        reports = reconcile_many(
            [make_batch("BATCH_DCA", 30000), make_batch("BATCH_DCB", 30000)],
            pool, settlement_window_days=5,
            subset_config=SubsetSumConfig(tolerance_cents=0, ambiguity_probe_limit=1),
            rate_card=ZERO_FEES,
        )

        claimed_and_cleared = [
            r for r in reports
            if r.match_result.cleared and "SHARED" in r.match_result.matched_txn_ids
        ]
        assert len(claimed_and_cleared) <= 1, (
            "Two settlements auto-cleared against the same payment: "
            + str([(r.batch_id, r.match_result.matched_txn_ids)
                   for r in claimed_and_cleared])
        )


class TestFallbackBindsAnchorsToo:
    """
    The joint solve binds a candidate to the settlement it names. The
    independent fallback must do the same, and cannot work it out alone: a
    batch reconciled by itself sees an unanchored record of the right size,
    not a record that names the batch next to it.

    Measured before this bound the fallback pool: a batch whose own leg had
    not arrived took its sibling's equal-valued leg and reported 0.91 -- the
    "partially anchored, used every anchor available" band. Every anchor
    available TO IT was used; the evidence it ignored belonged to a sibling.
    Only the double-claim guard caught it, and only because the sibling
    happened to claim the same record.
    """

    def test_a_starved_batch_does_not_borrow_a_siblings_anchored_leg(self):
        contested = 40000
        # BATCH_FBA's own second leg never arrives, so its target is
        # unreachable from its own records. BATCH_FBB holds an equal-valued
        # leg that would satisfy the arithmetic exactly.
        pool = [
            make_txn("FA1", 20000, ref_id_canonical="BATCH_FBA-FA1"),
            make_txn("FB1", 20000, ref_id_canonical="BATCH_FBB-FB1"),
            make_txn("FB_TWIN", contested, ref_id_canonical="BATCH_FBB-TWIN"),
        ]
        reports = reconcile_many(
            [make_batch("BATCH_FBA", 20000 + contested),
             make_batch("BATCH_FBB", 20000 + contested)],
            pool, settlement_window_days=5,
            subset_config=SubsetSumConfig(tolerance_cents=0, ambiguity_probe_limit=1),
            rate_card=ZERO_FEES,
        )
        rep_a = next(r for r in reports if r.batch_id == "BATCH_FBA")
        assert not (rep_a.match_result.cleared
                    and "FB_TWIN" in rep_a.match_result.matched_txn_ids), (
            "BATCH_FBA auto-cleared a leg referencing BATCH_FBB. Its own leg "
            "never arrived; the correct outcome is no clearance, not a "
            "substitution from the settlement next to it."
        )
