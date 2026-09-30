"""
Linkage reads identifier numbers from a record's description, and nothing else
from it.

1. Numbers of four or more digits are read; words, years, whole dates and the
   record's own amount are not.
2. Records whose descriptions share an id cluster, so a settlement whose
   reference column is empty is still identified.
3. A description naming the whole settlement id anchors, except on the feed
   the settlement itself arrived on.
4. Descriptions that carry only words change nothing.
"""
from datetime import datetime, timedelta, timezone

from linkage import build_candidate_links, description_tokens, txn_key
from orchestrator import reconcile_batch
from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence
from subset_sum import SubsetSumConfig

T0 = datetime(2026, 9, 17, 11, 30, tzinfo=timezone.utc)
CFG = SubsetSumConfig(num_search_workers=1)


def _tx(txn_id, amount, *, ref="", memo="", source=SourceType.GATEWAY, hours=40):
    return NormalizedTxn(source=source, source_txn_id=txn_id,
                         ref_id_canonical="".join(ch for ch in ref if ch.isalnum()).upper(),
                         amount_cents=amount, currency="INR",
                         timestamp_utc=T0 - timedelta(hours=hours),
                         tz_confidence=TzConfidence.HIGH, memo_raw=memo,
                         memo_normalized=memo.lower())


def _batch(bid, net):
    return SettlementBatch(batch_id=bid, net_amount_cents=net, currency="INR",
                           settled_at_utc=T0, source=SourceType.BANK,
                           member_source=SourceType.GATEWAY, declared_deductions_cents=0)


def test_only_identifier_numbers_are_read():
    t = _tx("p1", 150000, memo="Settlement STL0042 paid 2026-09-17 on 20260917 Rs 1500 ref 88121")
    assert description_tokens(t) == {"0042", "88121"}
    assert description_tokens(_tx("p2", 100, memo="Unrelated payment")) == frozenset()
    assert description_tokens(_tx("p3", 100)) == frozenset()


def test_shared_description_id_identifies_a_settlement_with_no_references():
    members = [_tx(f"m{i}", a, memo=f"Receipt against batch 77310 leg {i}")
               for i, a in enumerate((120_011, 230_017, 310_023))]
    noise = [_tx(f"n{i}", 50_000 + 7_919 * i, memo=f"Order {90_000 + i}") for i in range(30)]
    batch = _batch("PAYOUT-SEP-17", sum(t.amount_cents for t in members))
    links = build_candidate_links(batch, members + noise)
    linked = {txn_key(c) for c in links.candidates}
    assert {txn_key(t) for t in members} <= linked
    result = reconcile_batch(batch, members + noise, subset_config=CFG).match_result
    assert sorted(result.matched_txn_ids) == ["m0", "m1", "m2"]


def test_full_settlement_id_in_a_description_anchors_but_not_on_the_settlements_own_feed():
    member = _tx("m1", 1000, memo="Payout setl_QX91abcd7788 line 1")
    credit = _tx("b1", 1000, memo="RAZORPAY setl_QX91abcd7788", source=SourceType.BANK)
    links = build_candidate_links(_batch("setl_QX91abcd7788", 1000), [member, credit])
    anchored = {txn_key(c.txn) for c in links.scored if c.signals.settlement_id_match}
    assert anchored == {txn_key(member)}


def test_word_only_descriptions_change_nothing():
    pool = [_tx(f"o{i}", 40_000 + 3_331 * i, memo="Order") for i in range(10)]
    batch = _batch("PAYOUT-SEP-17", pool[0].amount_cents + pool[3].amount_cents)
    links = build_candidate_links(batch, pool)
    assert links.method == "no_linkage_signal"
    assert not reconcile_batch(batch, pool, subset_config=CFG).match_result.cleared
