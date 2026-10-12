"""
Records naming the settlement vouch for a set only when they chose it.

When more records name the settlement than the unanchored limit and the match
leaves some out, the arithmetic chose the members among them. A fee target off
by a blended rate found such a subset among 25 records carrying the
settlement's id and cleared it (FAILURE_LOG 61). Such a set is proposed.
A small named pool with one record left out is unaffected.
"""
from datetime import datetime, timedelta, timezone

from orchestrator import reconcile_batch
from recon_gates import UNANCHORED_AUTOCLEAR_LIMIT
from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence
from subset_sum import SubsetSumConfig

T0 = datetime(2026, 9, 17, 11, 30, tzinfo=timezone.utc)
CFG = SubsetSumConfig(num_search_workers=1)


def _tx(i, amount):
    return NormalizedTxn(source=SourceType.GATEWAY, source_txn_id=f"p{i}", ref_id_canonical="SETLDENSE12345",
                         amount_cents=amount, currency="INR", timestamp_utc=T0 - timedelta(hours=10 + i),
                         tz_confidence=TzConfidence.HIGH, memo_raw="", memo_normalized="")


def _batch(net):
    return SettlementBatch(batch_id="SETLDENSE12345", net_amount_cents=net, currency="INR",
                           settled_at_utc=T0, source=SourceType.BANK,
                           member_source=SourceType.GATEWAY, declared_deductions_cents=0)


# Binary-spaced amounts: every subset has its own sum, so the arithmetic finds
# exactly one set and no rival. Only the naming rule can stop it.
def _named(n):
    return [_tx(i, 1_000 * 2 ** i + i) for i in range(n)]


def test_a_subset_chosen_among_many_named_records_is_proposed():
    pool = _named(UNANCHORED_AUTOCLEAR_LIMIT + 5)
    members = pool[3:8]
    m = reconcile_batch(_batch(sum(t.amount_cents for t in members)), pool, subset_config=CFG).match_result
    assert sorted(m.matched_txn_ids) == sorted(t.source_txn_id for t in members)
    assert not m.cleared
    assert "left out" in m.reasoning
    assert m.confidence == 0.0, "chosen by arithmetic, it claims what arithmetic alone does"


def test_a_small_named_pool_with_one_left_out_still_clears():
    pool = _named(6)
    members = pool[:5]
    m = reconcile_batch(_batch(sum(t.amount_cents for t in members)), pool, subset_config=CFG).match_result
    assert m.cleared, m.reasoning
