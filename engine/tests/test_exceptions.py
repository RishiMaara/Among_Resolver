"""
Exception diagnosis tests — Agent 5.

Verifies all 4 root-cause categories: duplicate, partial payment,
timing lag, missing entry. Rule-based classification must be accurate
and deterministic — no LLM involved.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datetime import datetime, timedelta, timezone
from schema import NormalizedTxn, SourceType, TzConfidence, ExceptionReason
from exception_diagnosis import classify_exception, diagnose_batch_exceptions

BASE_TIME = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)


def make_txn(
    txn_id: str,
    amount_cents: int = 10000,
    ref_id: str | None = None,
    hours_offset: float = 0.0,
    source: SourceType = SourceType.GATEWAY,
) -> NormalizedTxn:
    return NormalizedTxn(
        source=source,
        source_txn_id=txn_id,
        ref_id_canonical=ref_id or f"REF{txn_id}",
        amount_cents=amount_cents,
        currency="INR",
        timestamp_utc=BASE_TIME + timedelta(hours=hours_offset),
        tz_confidence=TzConfidence.HIGH,
    )


class TestDuplicateDetection:

    def test_exact_dup_same_ref_and_amount(self):
        original = make_txn("ORIG", 5000, ref_id="RZPCOMMON")
        duplicate = make_txn("DUP", 5000, ref_id="RZPCOMMON")
        pool = [original, duplicate, make_txn("NOISE", 9999)]

        result = classify_exception(original, pool, "BATCH_DUP")
        assert result.reason == ExceptionReason.DUPLICATE
        assert "DUP" in result.candidate_txn_ids

    def test_same_ref_different_amount_not_dup(self):
        """Same ref but different amounts = partial payment, not duplicate."""
        t1 = make_txn("T1", 5000, ref_id="RZP123")
        t2 = make_txn("T2", 3000, ref_id="RZP123")  # different amount
        pool = [t1, t2]

        result = classify_exception(t1, pool, "BATCH_PARTIAL")
        assert result.reason != ExceptionReason.DUPLICATE


class TestPartialPayment:

    def test_split_payment_same_ref_different_amounts(self):
        # two legs of the same payment split across 2 records (different amounts = NOT a dup)
        leg_a = make_txn("LEG_A", 7000, ref_id="SPLIT_REF")
        leg_b = make_txn("LEG_B", 3000, ref_id="SPLIT_REF")  # different amount from leg_a
        noise = make_txn("NOISE", 1234)
        pool = [leg_a, leg_b, noise]

        # leg_a is unmatched, classify it
        result = classify_exception(leg_a, pool, "BATCH_SPLIT")
        assert result.reason == ExceptionReason.PARTIAL_PAYMENT
        assert "LEG_B" in result.candidate_txn_ids


    def test_partial_note_mentions_combined_total(self):
        leg_a = make_txn("A", 7000, ref_id="SPLITZ")
        leg_b = make_txn("B", 3000, ref_id="SPLITZ")
        pool = [leg_a, leg_b]

        result = classify_exception(leg_a, pool, "BATCH_COMBINED")
        assert "10000" in result.diagnosis_note  # 7000+3000=10000


class TestTimingLag:

    def test_timing_lag_when_same_amount_exists_in_another_source(self):
        """
        A genuine lagging counterpart: the SAME payment amount showing up
        in a DIFFERENT source within the settlement window — the other leg
        of the same payment, just not yet reconciled.
        """
        unmatched = make_txn("LATE", 15000, ref_id="UNIQUE_REF", hours_offset=0,
                             source=SourceType.GATEWAY)
        counterpart = make_txn("BANKLEG", 15000, ref_id="OTHER_REF", hours_offset=2,
                               source=SourceType.BANK)
        pool = [unmatched, counterpart]

        result = classify_exception(unmatched, pool, "BATCH_LAG")
        assert result.reason == ExceptionReason.TIMING_LAG
        assert "BANKLEG" in result.candidate_txn_ids

    def test_unrelated_nearby_record_is_not_timing_lag(self):
        """
        Regression test for the 50K stress finding: timing_lag used to fire
        whenever ANY record existed within +/- 3 days, regardless of whether
        it could plausibly be the counterpart. On a dataset spanning less
        than the window that was true for everything, so 100% of unmatched
        records collapsed into a single meaningless timing_lag label
        (20,078 identical exceptions, each citing the same "20082 record(s)").

        A record with a DIFFERENT amount in the SAME source is not a
        counterpart leg — it's just another transaction that happens to be
        nearby in time. It must classify as missing_entry.
        """
        unmatched = make_txn("LATE", 15000, ref_id="UNIQUE_REF", hours_offset=0,
                             source=SourceType.GATEWAY)
        unrelated = make_txn("PEER", 12000, ref_id="OTHER_REF", hours_offset=2,
                             source=SourceType.GATEWAY)
        pool = [unmatched, unrelated]

        result = classify_exception(unmatched, pool, "BATCH_LAG")
        assert result.reason == ExceptionReason.MISSING_ENTRY

    def test_same_amount_in_same_source_is_not_a_counterpart(self):
        """
        Same amount but the SAME source is a coincidence, not the other leg
        of the payment — a counterpart must come from a different system.
        """
        unmatched = make_txn("LATE", 15000, ref_id="UNIQUE_REF", hours_offset=0,
                             source=SourceType.GATEWAY)
        coincidence = make_txn("TWIN", 15000, ref_id="OTHER_REF", hours_offset=2,
                               source=SourceType.GATEWAY)
        pool = [unmatched, coincidence]

        result = classify_exception(unmatched, pool, "BATCH_LAG")
        assert result.reason == ExceptionReason.MISSING_ENTRY

    def test_same_amount_other_source_outside_window_is_not_timing_lag(self):
        """A counterpart 30 days out is not a settlement-timing lag."""
        unmatched = make_txn("LATE", 15000, ref_id="UNIQUE_REF", hours_offset=0,
                             source=SourceType.GATEWAY)
        far_leg = make_txn("FARLEG", 15000, ref_id="OTHER_REF", hours_offset=24 * 30,
                           source=SourceType.BANK)
        pool = [unmatched, far_leg]

        result = classify_exception(unmatched, pool, "BATCH_LAG")
        assert result.reason == ExceptionReason.MISSING_ENTRY


class TestMissingEntry:

    def test_missing_when_no_counterpart_anywhere(self):
        orphan = make_txn("ORPHAN", 99999, ref_id="TOTALLY_UNIQUE")
        # pool has only other transactions with very different refs/amounts
        pool = [orphan, make_txn("OTHER1", 100, ref_id="UNRELATED1")]

        # Make 'other1' far away in time so timing lag doesn't trigger
        other_far = NormalizedTxn(
            source=SourceType.GATEWAY,
            source_txn_id="FAR1",
            ref_id_canonical="UNRELATED_FAR",
            amount_cents=100,
            currency="INR",
            timestamp_utc=BASE_TIME + timedelta(days=30),  # 30 days away
            tz_confidence=TzConfidence.HIGH,
        )
        pool_isolated = [orphan, other_far]

        result = classify_exception(orphan, pool_isolated, "BATCH_MISSING")
        assert result.reason == ExceptionReason.MISSING_ENTRY


class TestBatchDiagnosis:

    def test_all_unmatched_get_classified(self):
        txns = [make_txn(f"T{i}", 1000 * (i + 1)) for i in range(5)]
        # All are "unmatched" — diagnose all
        exceptions = diagnose_batch_exceptions(txns, txns, "BATCH_ALL")
        assert len(exceptions) == len(txns)

    def test_all_exceptions_require_human_approval(self):
        txns = [make_txn("X1", 5000)]
        exceptions = diagnose_batch_exceptions(txns, txns, "BATCH_HUMAN")
        assert all(e.requires_human_approval for e in exceptions)


def test_the_no_match_line_reads_as_english_in_both_directions():
    """
    It said "Rs 1,22,226.35 over of the figure". The template appended a
    hardcoded "of" to both words, which is right for "short of" and wrong for
    "over of" — and this line only appears when a reconciliation has already
    failed, which is exactly when a reader is reading closely.
    """
    from plain_summary import plain_summary

    base = {
        "cleared": False, "matched_count": 40, "total_candidates": 41,
        "confidence": 0.0, "exception_count": 1, "ambiguous": False,
    }
    over = plain_summary({**base, "target_cents": 648765,
                          "tie_out_residual_cents": 12222235})
    assert "over the figure" in over, over
    assert "over of" not in over

    short = plain_summary({**base, "target_cents": 12870000,
                           "tie_out_residual_cents": -12222235})
    assert "short of the figure" in short, short
