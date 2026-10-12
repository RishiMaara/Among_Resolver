"""
Fees estimated per payment method.

1. A day mixing UPI, cards and netbanking clears: each payment is matched at
   its amount less its own method's fee, against the net payout, and the
   report ties out on the members' own deductions.
2. The same day with the methods unknown reaches no sum, as before.
3. It applies only where it can be right: never with declared deductions,
   never on a card with no percentage fee (a feed already net of fees),
   never on one method alone, and never on a method guessed from a memo.
"""
from datetime import datetime, timedelta, timezone

from fee_decomposition import DEFAULT_RATE_CARD, FeeRateCard, per_method_weights
from orchestrator import reconcile_batch
from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence
from subset_sum import SubsetSumConfig

T0 = datetime(2026, 9, 17, 11, 30, tzinfo=timezone.utc)
CFG = SubsetSumConfig(num_search_workers=1)
NET_TO_NET = FeeRateCard(gateway_fee_bps=0, flat_fee_cents=0, tax_withholding_bps=0)

# Card 2% + 1% withholding, UPI 0% + 1%, netbanking Rs 5 flat + 1%.
DAY = [("m_card", 100_000, "card", 97_000), ("m_upi", 50_000, "upi", 49_500),
       ("m_nb", 30_000, "netbanking", 29_200)]


def _tx(txn_id, amount, method=None, memo="", ref="SETLMIX123456", hours=30):
    extra = {"method": method} if method else {}
    return NormalizedTxn(source=SourceType.GATEWAY, source_txn_id=txn_id, ref_id_canonical=ref,
                         amount_cents=amount, currency="INR", timestamp_utc=T0 - timedelta(hours=hours),
                         tz_confidence=TzConfidence.HIGH, memo_raw=memo, memo_normalized=memo.lower(),
                         extra=extra)


def _pool(with_methods=True):
    members = [_tx(i, a, m if with_methods else None) for i, a, m, _ in DAY]
    noise = [_tx(f"n{k}", 40_000 + 3_371 * k, "card", ref=f"ORDER{k}") for k in range(8)]
    return members + noise


def _batch(declared=None):
    return SettlementBatch(batch_id="setl_MIX123456", net_amount_cents=sum(n for *_, n in DAY),
                           currency="INR", settled_at_utc=T0, source=SourceType.BANK,
                           member_source=SourceType.GATEWAY, declared_deductions_cents=declared)


def test_a_mixed_day_clears_and_ties_out_on_the_members_own_fees():
    report = reconcile_batch(_batch(), _pool(), subset_config=CFG)
    m = report.match_result
    assert m.cleared, m.reasoning
    assert sorted(m.matched_txn_ids) == ["m_card", "m_nb", "m_upi"]
    assert report.fee_basis == "estimated per payment method"
    assert report.matched_gross_cents == 180_000
    assert report.deductions_cents == 180_000 - 175_700
    assert report.ties_out


def test_the_same_day_with_methods_unknown_reaches_no_sum():
    m = reconcile_batch(_batch(), _pool(with_methods=False), subset_config=CFG).match_result
    assert not m.cleared


def test_it_applies_only_where_it_can_be_right():
    pool = _pool()
    assert per_method_weights(DEFAULT_RATE_CARD, pool) is not None
    assert per_method_weights(NET_TO_NET, pool) is None
    one_method = [_tx(f"c{k}", 10_000 + k, "card") for k in range(5)]
    assert per_method_weights(DEFAULT_RATE_CARD, one_method) is None
    guessed = [_tx("g1", 10_000, memo="UPI collect"), _tx("g2", 20_000, memo="Card capture")]
    assert per_method_weights(DEFAULT_RATE_CARD, guessed) is None
    # Declared deductions are a fact, and the single target built from them stands.
    declared = reconcile_batch(_batch(declared=4_300), pool, subset_config=CFG)
    assert declared.fee_basis == "declared" and declared.match_result.cleared
