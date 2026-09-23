"""
The substitutability guard, shared by the 1:N tiers and the joint (N:M) path.

A matched payment with an equal-amount copy in another feed, outside the pool
it was solved over, is not identified by the arithmetic: either copy would
do. The 1:N path withheld those; the joint path did not check at all.
"""
from datetime import datetime, timedelta, timezone

import orchestrator
import tiered_solve
from schema import MatchMethod, MatchResult, NormalizedTxn, SettlementBatch, SourceType, TzConfidence
from linkage import txn_key
from subset_sum import SubsetSumConfig

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _tx(source, txn_id, amount, ref=""):
    return NormalizedTxn(source=source, source_txn_id=txn_id, ref_id_canonical=ref,
                         amount_cents=amount, currency="INR", timestamp_utc=T0,
                         tz_confidence=TzConfidence.HIGH, memo_raw="", memo_normalized="")


def _batch(bid, net, member_source=None):
    return SettlementBatch(batch_id=bid, net_amount_cents=net, currency="INR",
                           settled_at_utc=T0 + timedelta(days=1), member_source=member_source,
                           declared_deductions_cents=0)


def _cleared(ids):
    return MatchResult(batch_id="B", matched_txn_ids=[t.source_txn_id for t in ids],
                       matched_keys=[txn_key(t) for t in ids], method=MatchMethod.EXACT_SUBSET_SUM,
                       confidence=0.95, matched_sum_cents=sum(t.amount_cents for t in ids),
                       target_cents=sum(t.amount_cents for t in ids), cleared=True)


GW = _tx(SourceType.GATEWAY, "G1", 12_345)
TWIN = _tx(SourceType.ERP, "E1", 12_345)


def test_a_twin_outside_the_pool_withholds():
    result = _cleared([GW])
    tiered_solve._withhold_if_substitutable(_batch("B", 12_345), result, [GW], [GW, TWIN],
                                            set(), "joint", outside="this settlement's pool")
    assert result.cleared is False and result.ambiguous is True
    assert "outside this settlement's pool" in result.reasoning


def test_a_declared_member_feed_or_an_anchor_settles_which_copy():
    declared = _cleared([GW])
    tiered_solve._withhold_if_substitutable(_batch("B", 12_345, SourceType.GATEWAY), declared,
                                            [GW], [GW, TWIN], set(), "joint")
    anchored = _cleared([GW])
    tiered_solve._withhold_if_substitutable(_batch("B", 12_345), anchored, [GW], [GW, TWIN],
                                            {txn_key(GW)}, "joint")
    assert declared.cleared and anchored.cleared


def test_the_joint_path_checks_every_settlement(monkeypatch):
    seen = []
    real = orchestrator._withhold_if_substitutable

    def spy(batch, result, *args, **kwargs):
        seen.append((batch.batch_id, args[-1] if args else kwargs.get("label")))
        return real(batch, result, *args, **kwargs)

    monkeypatch.setattr(orchestrator, "_withhold_if_substitutable", spy)
    pool = [_tx(SourceType.GATEWAY, "A1", 10_000, "SETTLEA0001"),
            _tx(SourceType.GATEWAY, "A2", 20_000, "SETTLEA0001"),
            _tx(SourceType.GATEWAY, "B1", 15_000, "SETTLEB0002"),
            _tx(SourceType.GATEWAY, "B2", 25_000, "SETTLEB0002")]
    orchestrator.reconcile_many(
        [_batch("SETTLEA0001", 30_000, SourceType.GATEWAY),
         _batch("SETTLEB0002", 40_000, SourceType.GATEWAY)],
        pool, subset_config=SubsetSumConfig(num_search_workers=1))
    assert sorted(b for b, _ in seen) == ["SETTLEA0001", "SETTLEB0002"]
