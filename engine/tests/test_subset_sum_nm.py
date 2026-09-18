"""
The N:M joint solver, tested on its own — before the orchestrator ever
touches it. subset_sum_nm.py's job is: given several targets and a pool
where a transaction may be eligible for more than one of them, decide every
target's assignment AT ONCE, so a shared claim is resolved by the arithmetic
rather than by whichever target happened to be considered first.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datetime import datetime, timezone
from schema import NormalizedTxn, SourceType, TzConfidence
import subset_sum_nm as nm


def _txn(tid, cents, source=SourceType.GATEWAY):
    return NormalizedTxn(
        source=source, source_txn_id=tid, ref_id_canonical=f"REF{tid}",
        amount_cents=cents, currency="INR",
        timestamp_utc=datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc),
        tz_confidence=TzConfidence.HIGH,
    )


def _key(tid, source="gateway"):
    return f"{source}:{tid}"


class TestBuildUnionPool:
    def test_a_candidate_in_one_pool_is_eligible_for_only_that_target(self):
        a = [_txn("a1", 100)]
        b = [_txn("b1", 200)]
        union, eligible = nm.build_union_pool([a, b])
        assert len(union) == 2
        by_id = {t.source_txn_id: i for i, t in enumerate(union)}
        assert eligible[by_id["a1"]] == {0}
        assert eligible[by_id["b1"]] == {1}

    def test_the_same_transaction_in_two_pools_appears_once_eligible_for_both(self):
        shared = _txn("x", 100)
        union, eligible = nm.build_union_pool([[shared], [shared]])
        assert len(union) == 1
        assert eligible[0] == {0, 1}

    def test_empty_input_produces_an_empty_pool(self):
        union, eligible = nm.build_union_pool([[], []])
        assert union == []
        assert eligible == []


class TestJointSolve:
    def test_two_disjoint_targets_solve_independently_within_one_call(self):
        a = [_txn("a1", 100), _txn("a2", 200)]
        b = [_txn("b1", 150), _txn("b2", 350)]
        union, eligible = nm.build_union_pool([a, b])
        result = nm.exact_subset_sum_nm(union, eligible, [300, 500], tolerance_cents=0)

        assert result is not None
        assert {t.source_txn_id for t in result.matched[0]} == {"a1", "a2"}
        assert {t.source_txn_id for t in result.matched[1]} == {"b1", "b2"}
        assert result.achieved_sums == [300, 500]

    def test_a_contested_candidate_is_resolved_by_the_other_targets_needs(self):
        """
        x=100 is the ONLY candidate eligible for target A (target 100), so the
        joint model is feasible ONLY if x is assigned to A — assigning it to
        B leaves A with nothing to satisfy its target, which is infeasible.
        B has its own alternative (b1=100) and does not need x at all. A
        solver that decided target by target, independently, would see this
        as two separately-ambiguous 100-target matches (x vs nothing for A,
        x vs b1 for B); solved jointly there is exactly one feasible
        assignment, and it is the one that leaves neither target stranded.
        """
        x = _txn("x", 100)
        b1 = _txn("b1", 100)
        union, eligible = nm.build_union_pool([[x], [x, b1]])
        result = nm.exact_subset_sum_nm(union, eligible, [100, 100], tolerance_cents=0)

        assert result is not None
        assert {t.source_txn_id for t in result.matched[0]} == {"x"}
        assert {t.source_txn_id for t in result.matched[1]} == {"b1"}

    def test_forcing_collapses_the_classic_refund_ambiguity(self):
        """
        Same shape as subset_sum's 1:N forced-refund test: 20 payments of
        Rs 1,000 and 3 refunds of Rs 1,000 all anchored to one target of
        Rs 17,000. Unforced this is wildly ambiguous (17 payments alone,
        18 minus one refund, 19 minus two, 20 minus three — all valid).
        Forcing the 3 refunds in leaves exactly one way to reach the target:
        every payment, since 20 payments include only sums to 20,000 and the
        3 forced refunds are needed to bring it down to 17,000 exactly.
        """
        payments = [_txn(f"p{i}", 1000) for i in range(20)]
        refunds = [_txn(f"r{i}", -1000) for i in range(3)]
        pool = payments + refunds
        union, eligible = nm.build_union_pool([pool])
        forced = {_key(f"r{i}") for i in range(3)}

        result = nm.exact_subset_sum_nm(
            union, eligible, [17000], tolerance_cents=0, forced_per_target=[forced],
        )
        assert result is not None
        matched_ids = {t.source_txn_id for t in result.matched[0]}
        assert matched_ids == {f"p{i}" for i in range(20)} | {f"r{i}" for i in range(3)}

    def test_an_unreachable_target_makes_the_whole_joint_model_infeasible(self):
        a = [_txn("a1", 100)]
        b = [_txn("b1", 200)]
        union, eligible = nm.build_union_pool([a, b])
        result = nm.exact_subset_sum_nm(union, eligible, [100, 999_999], tolerance_cents=0)
        assert result is None

    def test_an_empty_union_pool_returns_none_rather_than_raising(self):
        result = nm.exact_subset_sum_nm([], [], [100], tolerance_cents=0)
        assert result is None


class TestPerTargetAmbiguityProbe:
    def test_only_the_target_whose_set_actually_varies_is_flagged(self):
        # Target A: {100,200} and {150,150} both sum to 300 -- ambiguous.
        a = [_txn("a1", 100), _txn("a2", 200), _txn("a3", 150), _txn("a4", 150)]
        # Target B: a single unambiguous pair, disjoint pool.
        b = [_txn("b1", 60), _txn("b2", 40)]
        union, eligible = nm.build_union_pool([a, b])
        baseline = nm.exact_subset_sum_nm(union, eligible, [300, 100], tolerance_cents=0)
        assert baseline is not None

        varied = nm.probe_for_alternate_nm_assignment(
            union, eligible, baseline, [300, 100],
            tolerance_cents=0, time_limit_s=5.0, probe_limit=5,
        )
        assert varied == [True, False], (
            "Expected target A (constructed ambiguous) to vary and target B "
            "(constructed unique, disjoint pool) to stay stable across probes."
        )

    def test_no_alternate_within_budget_leaves_every_target_unflagged(self):
        a = [_txn("a1", 100), _txn("a2", 200)]
        union, eligible = nm.build_union_pool([a])
        baseline = nm.exact_subset_sum_nm(union, eligible, [300], tolerance_cents=0)
        assert baseline is not None

        varied = nm.probe_for_alternate_nm_assignment(
            union, eligible, baseline, [300],
            tolerance_cents=0, time_limit_s=5.0, probe_limit=3,
        )
        assert varied == [False]


class TestIdentityIsCollisionSafe:
    def test_a_bare_id_collision_across_two_targets_pools_does_not_merge_records(self):
        """
        source_txn_id is unique per feed, not globally (linkage.txn_key). A
        gateway "r0" and an unrelated ERP "r0" reusing the same bare id must
        stay two separate union entries, each eligible only for the target(s)
        that actually admitted it -- not silently merged into one.
        """
        gw = _txn("r0", 500, source=SourceType.GATEWAY)
        erp = _txn("r0", 700, source=SourceType.ERP)
        union, eligible = nm.build_union_pool([[gw], [erp]])
        assert len(union) == 2
        keys = {f"{t.source.value}:{t.source_txn_id}" for t in union}
        assert keys == {_key("r0", "gateway"), _key("r0", "erp")}
