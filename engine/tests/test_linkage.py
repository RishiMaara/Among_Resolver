"""
Linkage tests — Agent 2b.

Focused on the anchor, because the anchor is what makes the arithmetic
determinate. Without it the engine is back to subset-sum over an
under-determined pool, which measured 0.0% auto-clear accuracy.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datetime import datetime, timedelta, timezone

from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence
from linkage import build_candidate_links, canonical_key, MIN_CANONICAL_ANCHOR_LEN

BASE = datetime(2026, 1, 3, tzinfo=timezone.utc)


def txn(tid, ref, amount=10_000, source=SourceType.GATEWAY, hours=1.0):
    return NormalizedTxn(
        source=source, source_txn_id=tid, ref_id_canonical=ref,
        amount_cents=amount, currency="USD",
        timestamp_utc=BASE + timedelta(hours=hours),
        tz_confidence=TzConfidence.HIGH,
    )


def batch(bid="SYNTH-BATCH-USD-2026-01-03-0000"):
    return SettlementBatch(
        batch_id=bid, net_amount_cents=30_000, currency="USD",
        settled_at_utc=BASE + timedelta(hours=48),
        member_source=SourceType.GATEWAY,
    )


class TestSeparatorAgnosticAnchoring:
    """
    Ingestion stores references with separators stripped, while batch_id keeps
    whatever punctuation the source used. Matching them by token intersection
    alone therefore fails whenever the settlement id contains a separator —
    which found ZERO anchors on the ReconRiver dataset and failed every batch,
    including clean ones that tie to the cent. Our own 50K data passed only
    because its settlement id happened to have no internal separators.
    """

    def test_anchors_when_reference_is_canonicalised_and_batch_id_is_not(self):
        b = batch("SYNTH-BATCH-USD-2026-01-03-0000")
        pool = [
            txn("P1", "SYNTHBATCHUSD202601030000SYNTHORDER000001"),
            txn("P2", "SYNTHBATCHUSD202601030000SYNTHORDER000002"),
            txn("N1", "SYNTHORDER999999"),
        ]
        r = build_candidate_links(b, pool, 10)
        assert set(r.anchor_cluster_ids) == {"P1", "P2"}

    def test_the_same_id_written_three_ways_all_anchor(self):
        """The same identifier is written 'STL-2026-001', 'STL_2026_001' and
        'STL2026001' by three systems on the same payment."""
        b = batch("STL-2026-001")
        pool = [
            txn("A", "STL2026001ORD1"),
            txn("B", canonical_key("STL_2026_001") + "ORD2"),
            txn("C", "STL2026001"),
            txn("Z", "UNRELATED55"),
        ]
        r = build_candidate_links(b, pool, 10)
        assert set(r.anchor_cluster_ids) == {"A", "B", "C"}

    def test_short_batch_ids_do_not_anchor_by_containment(self):
        """A two-character id appears inside countless references; anchoring on
        it would be worse than not anchoring at all."""
        assert MIN_CANONICAL_ANCHOR_LEN >= 6
        b = batch("B1")
        pool = [txn("X", "AB1CDEF"), txn("Y", "ZB1ZZZ")]
        r = build_candidate_links(b, pool, 10)
        assert r.anchor_cluster_ids == []

    def test_unrelated_references_never_anchor(self):
        b = batch("SYNTH-BATCH-USD-2026-01-03-0000")
        pool = [txn("N1", "SYNTHORDER111"), txn("N2", "JRNL4002")]
        r = build_candidate_links(b, pool, 10)
        assert r.anchor_cluster_ids == []


class TestDegradation:

    def test_no_signal_anywhere_passes_the_pool_through_untouched(self):
        """On a feed with no usable references this module must be a no-op,
        not a shredder — narrowing on no evidence risks discarding the answer."""
        b = batch()
        pool = [txn(f"T{i}", "", amount=1000 + i) for i in range(5)]
        r = build_candidate_links(b, pool, 10)
        assert r.method == "no_linkage_signal"
        assert len(r.candidates) == len(pool)
