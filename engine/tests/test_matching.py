"""
Proof that the core differentiator works: given a target sum and a pool
containing a real subset plus noise, the DP engine recovers a subset that
sums to the target — the "412 out of 10,000" scenario, at test scale.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datetime import datetime, timezone
from schema import NormalizedTxn, SourceType, TzConfidence
from subset_sum import match_batch, SubsetSumConfig


def make_txn(txn_id: str, amount_cents: int) -> NormalizedTxn:
    return NormalizedTxn(
        source=SourceType.GATEWAY,
        source_txn_id=txn_id,
        ref_id_canonical=f"REF{txn_id}",
        amount_cents=amount_cents,
        currency="INR",
        timestamp_utc=datetime.now(timezone.utc),
        tz_confidence=TzConfidence.HIGH,
    )


def test_recovers_exact_subset_from_noisy_pool():
    # true subset: 150, 275, 900 = 1325 cents
    true_subset_amounts = [150, 275, 900]
    target = sum(true_subset_amounts)

    candidates = [make_txn(f"T{i}", amt) for i, amt in enumerate(true_subset_amounts)]
    # noise: transactions that should NOT be part of the match
    noise = [make_txn(f"N{i}", amt) for i, amt in enumerate([50, 3000, 777, 220])]

    pool = candidates + noise

    # tolerance=0: at this small scale (Rs 13.25 target) a +-5 cent tolerance
    # is ~0.4% of the target and noise items can coincidentally land inside
    # it -- that's the ambiguity check correctly doing its job, not a bug.
    # Zero tolerance isolates "does the engine recover the true unique
    # subset when one genuinely exists," which is what this test checks.
    result = match_batch("BATCH1", pool, target, config=SubsetSumConfig(tolerance_cents=0))

    assert result.cleared
    assert not result.ambiguous
    assert result.matched_sum_cents == target
    assert set(result.matched_txn_ids) == {"T0", "T1", "T2"}


def test_no_match_when_target_unreachable():
    pool = [make_txn("A", 100), make_txn("B", 250)]
    result = match_batch("BATCH2", pool, target_cents=99999)

    assert not result.cleared
    # With the greedy fallback feature, when CP-SAT fails, it returns the best approximation
    assert set(result.matched_txn_ids) == {"A", "B"}


def test_tolerance_band_accepts_small_rounding_diff():
    pool = [make_txn("A", 500), make_txn("B", 499)]  # sums to 999, target 1000
    result = match_batch("BATCH3", pool, target_cents=1000, config=SubsetSumConfig(tolerance_cents=5))

    assert result.cleared
    assert abs(result.matched_sum_cents - 1000) <= 5


def test_ambiguous_match_does_not_auto_clear():
    # two different pairs both sum to 300: {100,200} and {150,150}
    pool = [
        make_txn("A", 100), make_txn("B", 200),
        make_txn("C", 150), make_txn("D", 150),
    ]
    result = match_batch("BATCH4", pool, target_cents=300)

    assert result.ambiguous
    assert not result.cleared          # ambiguous matches must not auto-clear
    assert result.confidence < 1.0
    assert "not uniquely determined" in result.reasoning


# ── the three states a payment can be in that are not "money moved" ───────

def _txn(tid, cents, ref="SETTLE-X", status=""):
    from schema import NormalizedTxn, SourceType
    from datetime import datetime, timezone
    return NormalizedTxn(
        source=SourceType.GATEWAY, source_txn_id=tid, ref_id_canonical=ref,
        amount_cents=cents, currency="INR",
        timestamp_utc=datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc),
        tz_confidence="HIGH",
        extra={"status": status} if status else {},
    )


def test_an_anchored_refund_is_a_forced_member():
    """
    A refund carrying this settlement's reference is not optional. Twenty
    sales of Rs 1,000 less three refunds nets to Rs 17,000 — and so does
    seventeen sales alone, and eighteen sales less one refund. Leaving the
    refunds selectable made every settlement containing one ambiguous.
    """
    import subset_sum
    pool = ([_txn(f"p{i}", 100_000) for i in range(20)]
            + [_txn(f"r{i}", -100_000) for i in range(3)])
    forced = subset_sum._anchored_negatives("SETTLE-X", pool)
    assert forced == {"r0", "r1", "r2"}


def test_a_refund_for_another_settlement_is_not_forced():
    """Forcing an unanchored negative would assert membership the evidence
    does not support — the exact error this engine exists to avoid."""
    import subset_sum
    pool = [_txn("p0", 100_000), _txn("r0", -100_000, ref="SOME-OTHER-BATCH")]
    assert subset_sum._anchored_negatives("SETTLE-X", pool) == set()


def test_the_anchor_is_matched_in_canonical_form():
    """
    ref_id_canonical has punctuation stripped, so comparing a raw batch id
    against it silently matched nothing and every refund stayed optional.
    """
    import subset_sum
    pool = [_txn("r0", -100_000, ref="SETTLEX")]
    assert subset_sum._anchored_negatives("SETTLE-X", pool) == {"r0"}


def test_non_settling_statuses_are_named_and_closed():
    """
    Only states that unambiguously mean the money did not move. A blank or
    unrecognised status must be KEPT — most feeds carry none, and discarding
    a payment on a guess is worse than keeping it.
    """
    from orchestrator import NON_SETTLING_STATUSES
    for state in ("failed", "declined", "authorized", "pending",
                  "cancelled", "voided", "reversed", "rolled_back"):
        assert state in NON_SETTLING_STATUSES
    for settled in ("captured", "settled", "success", "paid", "completed", ""):
        assert settled not in NON_SETTLING_STATUSES


def test_money_that_moved_then_came_back_is_still_a_member():
    """
    The subtlest line in the whole list.

    refunded, chargeback and dispute all describe money that DID move and was
    later clawed back. The clawback is its own row — a negative the engine
    already forces in when anchored. Excluding the original as well would
    subtract the same reversal twice and leave the settlement short by exactly
    the refunded amount: a wrong answer that ties out to nothing and would be
    near-impossible to trace back to a status list.
    """
    from orchestrator import NON_SETTLING_STATUSES
    for moved in ("refunded", "partially_refunded", "chargeback",
                  "chargeback_lost", "dispute", "represented",
                  "deemed_success"):
        assert moved not in NON_SETTLING_STATUSES, moved


def test_authorised_but_uncaptured_never_settles_whatever_it_is_called():
    """Stripe alone spells this four ways; all mean the merchant can still
    walk away, so nothing has settled."""
    from orchestrator import NON_SETTLING_STATUSES
    for name in ("authorized", "authorised", "requires_capture", "uncaptured",
                 "requires_payment_method", "requires_confirmation",
                 "requires_action"):
        assert name in NON_SETTLING_STATUSES, name


def test_a_returned_bank_credit_is_not_settled():
    """Money that visibly arrived and then left is the case most likely to be
    reconciled by mistake."""
    from orchestrator import NON_SETTLING_STATUSES
    for name in ("returned", "bounced", "unposted", "on_hold", "held", "frozen"):
        assert name in NON_SETTLING_STATUSES, name


def test_agent_0_maps_a_status_column():
    """It was unmapped, so a failed payment reached the pool spendable."""
    import file_agent
    rep = file_agent.map_headers_with_report(
        ["txn_id", "amount", "currency", "timestamp", "payment_status"])
    assert rep.mapping.get("payment_status") == "status"
