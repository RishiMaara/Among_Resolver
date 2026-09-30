"""
A settlement is cleared when the records carrying its number are exactly the
matched set, and only then.

1. The band applies to the whole named set: one named record left out, or one
   record without the number let in, and it does not.
2. With no reference column, a large pool and every payment copied into the
   ERP ledger, the payments that carry the settlement's number clear: the
   copies do not carry it, so they are not substitutes.
3. An unrelated record carrying the same number, or copies carrying it too,
   and the set is proposed, not cleared.
4. A record carrying the settlement's number is not an unreferenced member,
   however small the tier it was solved in.
"""
from datetime import datetime, timedelta, timezone

from linkage import confidence_band, txn_key
from orchestrator import reconcile_batch
from recon_gates import _unreferenced_members
from schema import (MatchMethod, MatchResult, NormalizedTxn, SettlementBatch, SourceType,
                    TzConfidence)
from subset_sum import SubsetSumConfig

T0 = datetime(2026, 9, 17, 11, 30, tzinfo=timezone.utc)
CFG = SubsetSumConfig(num_search_workers=1)
AMOUNTS = (120_011, 230_017, 310_023)


def _tx(txn_id, amount, *, ref="", memo="", source=SourceType.GATEWAY, hours=40):
    return NormalizedTxn(source=source, source_txn_id=txn_id,
                         ref_id_canonical="".join(ch for ch in ref if ch.isalnum()).upper(),
                         amount_cents=amount, currency="INR",
                         timestamp_utc=T0 - timedelta(hours=hours),
                         tz_confidence=TzConfidence.HIGH, memo_raw=memo,
                         memo_normalized=memo.lower())


def _pool(mirror_memo="Ledger entry"):
    members = [_tx(f"m{i}", a, memo=f"Payout 77310 line {i}") for i, a in enumerate(AMOUNTS)]
    mirrors = [_tx(f"m{i}", a, memo=mirror_memo, source=SourceType.ERP, hours=38)
               for i, a in enumerate(AMOUNTS)]
    noise = [_tx(f"n{i}", 50_000 + 7_919 * i, memo=f"Order {90_000 + i}") for i in range(60)]
    return members, members + mirrors + noise


def _batch(net):
    return SettlementBatch(batch_id="SETTLE-77310", net_amount_cents=net, currency="INR",
                           settled_at_utc=T0, source=SourceType.BANK,
                           declared_deductions_cents=0)


def test_the_band_needs_the_whole_named_set_and_nothing_else():
    a, b, c = "gateway:a", "gateway:b", "gateway:c"
    assert confidence_band(set(), {a, b}, 0.5, None, {a, b}) == "named_complete"
    assert confidence_band(set(), {a, b}, 0.5, None, {a, b, c}) != "named_complete"
    assert confidence_band(set(), {a, b, c}, 0.5, None, {a, b}) != "named_complete"


def test_payments_carrying_the_settlements_number_clear_past_their_ledger_copies():
    members, pool = _pool()
    result = reconcile_batch(_batch(sum(AMOUNTS)), pool, subset_config=CFG).match_result
    assert result.cleared, result.reasoning
    assert set(result.matched_keys) == {txn_key(t) for t in members}
    assert result.confidence >= 0.85


def test_an_unrelated_record_carrying_the_number_blocks_the_clear():
    members, pool = _pool()
    stray = _tx("x1", 99_991, memo="Refund note 77310")
    result = reconcile_batch(_batch(sum(AMOUNTS)), pool + [stray], subset_config=CFG).match_result
    assert not result.cleared


def test_copies_carrying_the_number_too_block_the_clear():
    _, pool = _pool(mirror_memo="Ledger entry for payout 77310")
    result = reconcile_batch(_batch(sum(AMOUNTS)), pool, subset_config=CFG).match_result
    assert not result.cleared


def test_a_record_carrying_the_number_is_not_an_unreferenced_member():
    anchor = _tx("a", 1000, ref="SETTLE77310", memo="Payout 77310")
    named = [_tx(f"b{i}", 2000 + i, memo=f"Payout 77310 line {i}") for i in range(3)]
    other = [_tx(f"o{i}", 5000 + i, memo="Order") for i in range(2)]
    tier = [anchor, *named, *other]
    matched = [anchor, *named]
    result = MatchResult(batch_id="SETTLE-77310", matched_txn_ids=[t.source_txn_id for t in matched],
                         method=MatchMethod.EXACT_SUBSET_SUM, confidence=1.0,
                         matched_sum_cents=sum(t.amount_cents for t in matched),
                         target_cents=sum(t.amount_cents for t in matched), cleared=True)
    anchors = {txn_key(anchor)}
    # Judged over this six-record tier alone, the number looks like boilerplate.
    assert _unreferenced_members(result, tier, anchors) == named
    assert _unreferenced_members(result, tier, anchors, {txn_key(t) for t in named}) == []
