"""
A confidence figure is never higher than what was measured for that kind of
answer.

1. When a second subset also sums, the proposed set claims nothing: it was
   right 4 of 12, 2 of 28 and 0 of 70 times on the three corpora.
2. A fuzzy recovery bundle claims nothing: 0 of 212 exact.
3. Bands never measured claim no more than arithmetic alone, which claims
   nothing, and a small pool does not turn no evidence into some.
"""
from datetime import datetime, timedelta, timezone

from linkage import (CONFIDENCE_BANDS, LinkageResult, LinkedCandidate, LinkSignals,
                     link_confidence, txn_key)
from recon_gates import FUZZY_RECOVERY_CONFIDENCE
from schema import NormalizedTxn, SourceType, TzConfidence
from subset_sum import AMBIGUOUS_CONFIDENCE, SubsetSumConfig, match_batch

T0 = datetime(2026, 9, 17, 11, 30, tzinfo=timezone.utc)


def _tx(txn_id, amount):
    return NormalizedTxn(source=SourceType.GATEWAY, source_txn_id=txn_id, ref_id_canonical="",
                         amount_cents=amount, currency="INR", timestamp_utc=T0 - timedelta(hours=5),
                         tz_confidence=TzConfidence.HIGH, memo_raw="", memo_normalized="")


def test_an_ambiguous_set_claims_nothing():
    pool = [_tx("a", 10_000), _tx("b", 20_000), _tx("c", 30_000)]
    result = match_batch("B1", pool, 30_000, SubsetSumConfig(num_search_workers=1))
    assert result.ambiguous and not result.cleared
    assert result.confidence == AMBIGUOUS_CONFIDENCE == 0.0


def test_a_fuzzy_bundle_claims_nothing():
    assert FUZZY_RECOVERY_CONFIDENCE == 0.0


def test_unmeasured_bands_claim_nothing_and_a_small_pool_adds_nothing():
    for band in ("arithmetic_only", "clustered", "anchors_ignored"):
        assert CONFIDENCE_BANDS[band] == 0.0, band
    pool = [_tx("a", 10_000), _tx("b", 20_000)]
    weak = LinkSignals(cross_source_amount_peer=True)
    links = LinkageResult(candidates=pool, pool_before=2, pool_after=2, method="reference_cluster",
                          reasoning="", scored=[LinkedCandidate(t, weak, weak.score()) for t in pool])
    # Two candidates: the +0.03 small-pool bonus would apply to any band that claims something.
    assert link_confidence(links, {txn_key(t) for t in pool}) == 0.0
