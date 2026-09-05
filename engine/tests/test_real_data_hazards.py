"""
Regressions for failure modes that ONLY appear on realistically-shaped data.

Every one of these passed the existing 162 tests, both reference datasets and
the 50K stress run before it was written. They were found by asking what real
feeds do that generated ones do not, and then checking.

The common thread is that both reference datasets are synthetic and share
generator conveniences real systems do not have: fixed-width zero-padded
identifiers, globally unique transaction ids, and a member feed that always
contains the answer. Each convenience hid a defect.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence  # noqa: E402
import linkage  # noqa: E402
import pipeline  # noqa: E402
from ingestion import normalize_batch_with_report
from schema import SourceType
from datetime import datetime, timezone
from main import _settlement_instant

BASE = datetime(2026, 8, 20, tzinfo=timezone.utc)


def _txn(tid, ref, amount_cents, source=SourceType.GATEWAY, hours=0):
    return NormalizedTxn(
        source=source, source_txn_id=tid, ref_id_canonical=ref,
        amount_cents=amount_cents, currency="INR",
        timestamp_utc=BASE + timedelta(hours=hours),
        tz_confidence=TzConfidence.HIGH,
    )



def _usd(tid, ref, amount_cents, source=SourceType.GATEWAY, hours=0):
    """Same shape as _txn, denominated in USD."""
    return NormalizedTxn(
        source=source, source_txn_id=tid, ref_id_canonical=ref,
        amount_cents=amount_cents, currency="USD",
        timestamp_utc=BASE + timedelta(hours=hours),
        tz_confidence=TzConfidence.HIGH,
    )

def _batch(bid, net_cents=1000_00, member_source=None):
    return SettlementBatch(
        batch_id=bid, net_amount_cents=net_cents, currency="INR",
        settled_at_utc=BASE + timedelta(days=1),
        member_source=member_source,
    )


class TestSequentialSettlementIds:
    """
    Settlement ids in the wild are sequential and unpadded. Both reference
    datasets use fixed-width zero-padded ids, where no id is a prefix of
    another — so plain substring containment looked correct for as long as
    only those datasets were used.
    """

    def test_short_id_does_not_anchor_a_longer_one(self):
        pool = [
            _txn("g1", "SETTLE1ORDER001", 100_00),
            _txn("g2", "SETTLE10ORDER002", 200_00),    # belongs to SETTLE-10
            _txn("g3", "SETTLE100ORDER03", 300_00),    # belongs to SETTLE-100
        ]
        r = linkage.build_candidate_links(_batch("SETTLE-1"), pool)
        assert r.anchor_cluster_ids == ["g1"], (
            "SETTLE-1 anchored another settlement's members. An anchor is "
            "worth 0.55 of the linkage score and 0.95 confidence once "
            "matched, so a false one is a confident wrong answer, not a "
            "near miss."
        )

    def test_the_longer_id_still_anchors_its_own(self):
        """The boundary rule must not cost a legitimate anchor."""
        pool = [
            _txn("g1", "SETTLE1ORDER001", 100_00),
            _txn("g2", "SETTLE10ORDER002", 200_00),
        ]
        r = linkage.build_candidate_links(_batch("SETTLE-10"), pool)
        assert r.anchor_cluster_ids == ["g2"]

    @pytest.mark.parametrize("haystack,needle,expected", [
        ("SETTLE10ORDER", "SETTLE1", False),    # digit continues the number
        ("SETTLE1ORDER001", "SETTLE1", True),   # letter ends it
        ("SETTLE100X", "SETTLE1", False),
        ("XSETTLE1", "SETTLE1", True),          # trailing edge of string
        ("ASTL2026001B", "STL2026001", True),   # letters either side
        ("9STL2026", "STL2026", True),          # needle starts with a letter
        ("STL20261", "STL2026", False),         # needle ends with a digit
    ])
    def test_numeric_boundary_rule(self, haystack, needle, expected):
        assert linkage._contains_identifier(haystack, needle) is expected


class TestCrossFeedIdCollision:
    """
    `source_txn_id` is unique per FEED, not globally. Feeds mint their own
    sequences and they overlap constantly. Both reference datasets prefix
    every id by feed (SYNTH-INT-, SYNTH-PROC-), so ids never collided.
    """

    def test_signals_do_not_leak_between_identical_ids(self):
        pool = [
            _txn("1001", "SETTLEX2026ORDER1", 100_00, SourceType.GATEWAY),
            _txn("1001", "TOTALLYUNRELATED", 999_00, SourceType.ERP),
        ]
        r = linkage.build_candidate_links(_batch("SETTLE-X-2026"), pool)
        erp = [c for c in r.scored if c.txn.source is SourceType.ERP][0]
        assert erp.signals.settlement_id_match is False, (
            "An unrelated ERP record inherited the gateway record's anchor "
            "status purely because the two feeds reused the same id."
        )

    def test_anchor_keys_distinguish_feeds(self):
        pool = [
            _txn("1001", "SETTLEX2026ORDER1", 100_00, SourceType.GATEWAY),
            _txn("1001", "TOTALLYUNRELATED", 999_00, SourceType.ERP),
        ]
        r = linkage.build_candidate_links(_batch("SETTLE-X-2026"), pool)
        assert r.anchor_keys == {"gateway:1001"}


class TestNoInScopeCandidates:
    """
    A declared member_source with nothing in that feed linked to the batch is
    a legitimate finding, not an error. It reached the tier loop as None and
    raised AttributeError — a 500 from the upload form the moment someone
    picks the wrong feed.
    """

    def test_declared_feed_with_no_members_reports_rather_than_crashes(self):
        pool = [
            _txn("e1", "SETTLEQ2026ORDER1", 100_00, SourceType.ERP),
            _txn("e2", "SETTLEQ2026ORDER2", 200_00, SourceType.ERP),
        ]
        batch = _batch("SETTLE-Q-2026", 300_00, member_source=SourceType.GATEWAY)

        report = pipeline.reconcile_settlement(batch, pool)

        assert report.summary()["cleared"] is False
        assert "No candidate survived linkage" in report.match_result.reasoning
        assert "gateway" in report.match_result.reasoning


class TestLinkageIsNotComputedTwice:
    """
    Linkage tokenises every reference in the pool. On 50,000 candidates it is
    the most expensive stage in the run, and the pipeline was paying for it
    twice per settlement — once for a log line whose result it discarded.
    """

    def test_single_linkage_pass_per_settlement(self, monkeypatch):
        calls = {"n": 0}
        real = linkage.build_candidate_links

        def counting(*a, **k):
            calls["n"] += 1
            return real(*a, **k)

        import orchestrator
        monkeypatch.setattr(orchestrator, "build_candidate_links", counting)
        monkeypatch.setattr(linkage, "build_candidate_links", counting)

        pool = [_txn(f"g{i}", f"SETTLER2026ORD{i:04d}", (i + 1) * 100)
                for i in range(50)]
        pipeline.reconcile_settlement(_batch("SETTLE-R-2026", 300_00), pool)

        assert calls["n"] == 1, f"linkage ran {calls['n']} times for one batch"


class TestAnchoredEvidenceIsNotTransferable:
    """
    The guard must ask whether the MATCHED SET is anchored, not whether the
    batch has anchors anywhere.

    Those are different questions and the gap between them was a false clear:
    a settlement whose members reference it, but one of whose legs has not
    arrived, has anchors in the pool and no reachable correct answer. The
    solver found five unrelated records summing to the target and cleared
    them, because anchors existed elsewhere and the pool was under the
    small-pool limit.

    Neither reference corpus could show it. benchmark.py's batch ids share no
    canonical form with its references, so anchors were never found and the
    guard always fired; ReconRiver's ids do match, but its data is clean
    enough that the true set is always reachable. It needs both at once —
    anchors present AND the true answer missing — which is what a late leg
    does in production every day.
    """

    def test_unanchored_match_is_withheld_even_when_the_batch_has_anchors(self):
        import orchestrator

        # Three members that name the settlement, one of which never arrived.
        present = [
            _txn("m1", "STL77LEG1", 100_00),
            _txn("m2", "STL77LEG2", 200_00),
        ]
        missing_leg_amount = 300_00
        # Noise that happens to sum to the full target including the absent leg.
        noise = [
            _txn("n1", "NOISE1", 150_00),
            _txn("n2", "NOISE2", 250_00),
            _txn("n3", "NOISE3", 200_00),
        ]
        gross = 100_00 + 200_00 + missing_leg_amount
        batch = SettlementBatch(
            batch_id="STL77",
            net_amount_cents=gross,
            currency="INR",
            settled_at_utc=BASE + timedelta(hours=6),
            declared_deductions_cents=0,
        )

        report = orchestrator.reconcile_batch(
            batch, present + noise, settlement_window_days=5,
        )
        matched = set(report.match_result.matched_txn_ids)

        if report.match_result.cleared:
            assert matched & {"m1", "m2"}, (
                "Cleared a set containing none of the records that reference "
                f"this settlement. Matched {sorted(matched)} while m1/m2 name "
                "STL77. Evidence does not transfer between records."
            )


class TestPartialAnchoringDependsOnWhetherEvidenceWasIGNORED:
    """
    A partially anchored match is trustworthy when it used EVERY anchor the
    pool offered, and not when it left some out.

    This class previously asserted that partial anchoring never clears, on the
    reasoning that one anchored member cannot vouch for four unanchored ones.
    Measuring it showed the rule was too blunt: it withheld ten correct answers
    to prevent two wrong ones, and the two wrong ones differed in a specific,
    causal way — anchors sat in the pool that the matched set did not include.

    "One record names the settlement and the match includes it" and "three
    records name it and the match includes one" are different claims. The first
    reads the evidence; the second contradicts it. Calibration now measures the
    first at 100% correct across 41 observations.
    """

    def test_using_every_available_anchor_is_allowed_to_clear(self):
        import orchestrator

        # One record names the settlement; the match includes it plus two
        # unanchored members. No anchor is left behind.
        pool = [
            _txn("a1", "STL8801LEG1", 100_00),
            _txn("n1", "NOISEAAA", 150_00),
            _txn("n2", "NOISEBBB", 250_00),
            _txn("n3", "NOISECCC", 320_00),
            _txn("n4", "NOISEDDD", 410_00),
        ]
        batch = SettlementBatch(
            batch_id="STL8801", net_amount_cents=500_00, currency="INR",
            settled_at_utc=BASE + timedelta(hours=6), declared_deductions_cents=0,
        )
        report = orchestrator.reconcile_batch(batch, pool, settlement_window_days=5)
        matched = set(report.match_result.matched_txn_ids)

        assert "a1" in matched, "the anchored record should be in the match"
        assert report.match_result.confidence >= 0.85, (
            f"Using every available anchor scored "
            f"{report.match_result.confidence}; calibration puts this band at "
            f"100% correct, so it should sit above the auto-clear gate."
        )

    def test_leaving_an_anchor_unused_still_withholds(self):
        """The original concern, kept — this is the case that stays blocked."""
        import orchestrator

        # TWO records name the settlement. A match that includes one and
        # ignores the other has contradicted the evidence it does have.
        pool = [
            _txn("a1", "STL8802LEG1", 100_00),
            _txn("a2", "STL8802LEG2", 999_00),   # anchored, and not in any sum below
            _txn("n1", "NOISEAAA", 150_00),
            _txn("n2", "NOISEBBB", 250_00),
            _txn("n3", "NOISECCC", 320_00),
        ]
        batch = SettlementBatch(
            batch_id="STL8802", net_amount_cents=500_00, currency="INR",
            settled_at_utc=BASE + timedelta(hours=6), declared_deductions_cents=0,
        )
        report = orchestrator.reconcile_batch(batch, pool, settlement_window_days=5)

        if report.match_result.cleared:
            matched = set(report.match_result.matched_txn_ids)
            assert {"a1", "a2"} <= matched, (
                f"Cleared {sorted(matched)} while an anchored record was left "
                f"out. An ignored anchor is evidence pointing elsewhere."
            )


class TestAutoclearThresholdMatchesCalibration:
    """
    The gate must sit where measured reliability begins, not on a round number.

    calibration.py buckets every prediction against its outcome and finds
    everything at or above 0.85 correct in all 93 observations. A threshold of
    0.90 withheld the 50K stress run — fully anchored, penalised only for pool
    size, scoring 0.87, finding all 55 true members with precision and recall
    of 1.0 — for being three hundredths under an arbitrary line.
    """

    def test_threshold_is_the_measured_boundary(self):
        import orchestrator

        assert orchestrator.MIN_AUTOCLEAR_CONFIDENCE == 0.85, (
            "The auto-clear gate should sit at the measured reliability "
            "boundary. Re-run scripts/calibration.py before moving it."
        )

    def test_a_fully_anchored_large_pool_match_still_clears(self):
        """0.95 minus the large-pool penalty is 0.87, which must clear."""
        import orchestrator
        from subset_sum import SubsetSumConfig

        members = [_txn(f"m{i}", f"STL9902LEG{i}", (i + 1) * 1_000) for i in range(4)]
        noise = [_txn(f"z{i}", f"UNREL{i:04d}", 700 + i * 37) for i in range(240)]
        gross = sum(t.amount_cents for t in members)
        batch = SettlementBatch(
            batch_id="STL9902",
            net_amount_cents=gross,
            currency="INR",
            settled_at_utc=BASE + timedelta(hours=6),
            declared_deductions_cents=0,
            member_source=SourceType.GATEWAY,
        )

        report = orchestrator.reconcile_batch(
            batch, members + noise,
            subset_config=SubsetSumConfig(tolerance_cents=0, ambiguity_probe_limit=2),
            settlement_window_days=5,
        )
        assert set(report.match_result.matched_txn_ids) == {"m0", "m1", "m2", "m3"}
        assert report.match_result.cleared, (
            f"A fully anchored match over a large pool scored "
            f"{report.match_result.confidence} and did not clear."
        )


class TestNoLinkageSignalNeverAutoClears:
    """
    When linkage reports it found NOTHING, arithmetic is all that is left —
    and arithmetic alone is what this engine exists to say is insufficient.

    Found on a real bank statement rather than a generated one. Eight genuine
    UPI debits, no settlement reference among them because a UPI RRN
    identifies the payment and not any settlement. Four summed to a Rs 500
    credit to the paisa, and the engine cleared it at 0.22 confidence — a band
    calibration measures at 27.6% accurate. Those four payments went to four
    unrelated people.

    The small-pool exemption assumed a unique sum over few candidates means
    the answer is determined. That holds only when the answer is IN the pool.
    Over a window of unrelated traffic a unique sum is a coincidence, and on
    that statement 69 of 189 credits had one.
    """

    def _upi_pool(self):
        # Real amounts and RRN shapes from the statement, ids anonymised.
        raw = [("A1", 40_00), ("A2", 18_00), ("A3", 170_00), ("A4", 270_00),
               ("A5", 50_00), ("A6", 80_00), ("A7", 210_00), ("A8", 130_00)]
        # 40 + 170 + 80 + 210 = 500 exactly, and nothing links them.
        return [_txn(f"UPI_{i}", f"RRN{i}", amt) for i, amt in raw]

    def test_arithmetic_alone_does_not_clear_however_small_the_pool(self):
        import orchestrator

        pool = self._upi_pool()
        batch = SettlementBatch(
            batch_id="SBI-UPI-20260404",
            net_amount_cents=500_00,
            currency="INR",
            settled_at_utc=BASE + timedelta(hours=12),
            declared_deductions_cents=0,
            member_source=SourceType.GATEWAY,
        )

        report = orchestrator.reconcile_batch(batch, pool, settlement_window_days=4)

        assert report.match_result.cleared is False, (
            "Cleared a set backed by nothing but the sum. Linkage reported "
            "no_linkage_signal, which means no reference, no cluster and no "
            "cross-source peer exists in the pool."
        )
        assert "arithmetic alone" in report.match_result.reasoning

    def test_linkage_reports_no_signal_for_this_pool(self):
        """The guard keys off linkage's own verdict, so pin that verdict."""
        r = linkage.build_candidate_links(
            _batch("SBI-UPI-20260404", 500_00), self._upi_pool(),
        )
        assert r.method == "no_linkage_signal"


class TestOperability:
    """
    The things an operator needs to be true before trusting a deployment.

    Each is something that can be silently wrong: an audit trail written where
    the OS will delete it, an open API on a host that can reach the internet,
    a rate card that is somebody else's contract. None of them fail a
    reconciliation, which is exactly why they need asserting.
    """

    def test_the_audit_store_is_durable(self):
        import audit
        st = audit.storage_status()
        assert st["durable"] is True, (
            f"Audit backend is {st['backend']!r} and not durable. An audit "
            f"trail a reboot can erase is a log, not an audit trail."
        )

    def test_health_reports_what_is_misconfigured(self, monkeypatch):
        import importlib, auth
        monkeypatch.delenv("API_KEY", raising=False)
        importlib.reload(auth)
        assert auth.status_label() == "disabled"
        monkeypatch.setenv("API_KEY", "k")
        importlib.reload(auth)
        assert auth.status_label() == "enabled"

    def test_a_wrong_key_is_rejected_in_constant_time_style(self, monkeypatch):
        """Not a timing measurement — that compare_digest is used at all."""
        import inspect, auth
        src = inspect.getsource(auth)
        assert "compare_digest" in src, (
            "Key comparison must not use ==. String equality short-circuits "
            "on the first differing byte and leaks the key by timing."
        )

    def test_the_rate_card_can_be_overridden_per_run(self):
        """
        A 1.5 basis point error takes auto-clear to 0%, so the card being
        hardcoded was the single most likely way a live demo dies.
        """
        import main
        card = main._rate_card(350, None, None)
        assert card.gateway_fee_bps == 350
        assert card.tax_withholding_bps == main.DEFAULT_RATE_CARD.tax_withholding_bps
        assert main._rate_card(None, None, None) is main.DEFAULT_RATE_CARD


class TestCurrencyIsPartOfTheComparison:
    """
    amount_cents carries no unit, and nothing downstream re-checks it.

    The solver adds integers. Give it 100 USD and 100 INR and it reports 200,
    with an exact tie-out and real anchors — every signal the engine reasons
    with says yes, because the unit was never part of the comparison.

    Measured before the fix: a three-leg INR 300 settlement cleared at 0.97
    confidence against two INR legs and one USD leg. That is a false clear in
    the top confidence band, and no other guard catches it.
    """

    def _mixed_pool(self):
        return [
            _txn("inr1", "STL777LEG1", 100_00),
            _txn("inr2", "STL777LEG2", 100_00),
            _usd("usd1", "STL777LEG3", 100_00),
        ]

    def test_a_foreign_currency_leg_is_never_summed_in(self):
        import orchestrator

        batch = SettlementBatch(
            batch_id="STL777", net_amount_cents=300_00, currency="INR",
            settled_at_utc=BASE + timedelta(hours=6),
            declared_deductions_cents=0, member_source=SourceType.GATEWAY,
        )
        report = orchestrator.reconcile_batch(
            batch, self._mixed_pool(), settlement_window_days=5,
        )
        matched = set(report.match_result.matched_txn_ids)
        assert "usd1" not in matched, (
            f"A USD leg was summed into an INR settlement: {sorted(matched)}. "
            f"Integer minor units are not comparable across currencies."
        )

    def test_the_exclusion_is_recorded(self):
        import orchestrator, audit

        batch = SettlementBatch(
            batch_id="STL778", net_amount_cents=300_00, currency="INR",
            settled_at_utc=BASE + timedelta(hours=6),
            declared_deductions_cents=0, member_source=SourceType.GATEWAY,
        )
        orchestrator.reconcile_batch(batch, self._mixed_pool(), settlement_window_days=5)
        trail = audit.get_audit_trail("STL778")
        assert any(e["agent"] == "currency_filter" for e in trail), (
            "Dropping a candidate must be recorded. A silent exclusion is "
            "indistinguishable from a candidate that was never supplied."
        )


class TestCurrencyIsStatedOrKnownToBeAssumed:
    """
    The engine could not distinguish "this is INR" from "no currency column
    was present" — both were recorded as INR.

    That silently defeated the orchestrator's currency guard, which exists
    because 100 INR + 100 INR + 100 USD once summed to 300 and cleared at
    0.97 confidence. A USD feed with no currency column became INR, so the
    guard compared a default against a default and passed.

    Timestamps have carried tz_confidence since the beginning. This is the
    same idea applied to the field where getting it wrong is arithmetic.
    """

    def _feed(self, with_currency: bool):
        row = {"txn_id": "T1", "amount": 100.00,
               "timestamp": "2026-03-09T10:00:00Z", "status": "captured"}
        if with_currency:
            row["currency"] = "USD"
        return [row]

    def test_a_stated_currency_is_marked_stated(self):
        rep = normalize_batch_with_report(self._feed(True), SourceType.GATEWAY)
        txn = rep.normalized[0]
        assert txn.currency == "USD"
        assert txn.currency_stated is True

    def test_an_absent_currency_is_not_passed_off_as_inr(self):
        rep = normalize_batch_with_report(self._feed(False), SourceType.GATEWAY)
        txn = rep.normalized[0]
        # The value still defaults, because a single-currency merchant with no
        # currency column is the common case and must keep working.
        assert txn.currency == "INR"
        # But the engine now knows it invented that, and can say so.
        assert txn.currency_stated is False


class TestSettlementAnchorIgnoresServerTimezone:
    """
    A settlements file routinely holds a bare date — "2026-03-03". dp.parse
    returns a NAIVE datetime for that, and the endpoints then called
    .astimezone(timezone.utc), which interprets naive as the MACHINE's local
    zone. On an IST host the anchor became 2026-03-02T18:30Z.

    Two consequences, both worse than the shift. The same file reconciles
    differently in Mumbai and in Virginia. And the anchor was compared against
    transaction timestamps that ingestion normalises by a different rule — it
    never uses server-local — so a settlement and its own members sat on two
    different clocks.

    Found by running a queue of 12 settlements built from real USAspending
    FY2022 amounts: the window kept 20 of 126 candidates, excluded true
    members dated after 18:30 on the anchor day, and every batch came back
    unmatched.
    """

    def test_a_bare_date_is_midnight_utc_not_midnight_local(self):
        got = _settlement_instant("2026-03-03")
        assert got == datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
        assert got.date().isoformat() == "2026-03-03"

    def test_an_explicit_offset_is_still_honoured(self):
        # A string that states its zone is authoritative and must convert.
        got = _settlement_instant("2026-03-03T05:30:00+05:30")
        assert got == datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)

    def test_naive_datetime_with_a_time_is_also_utc(self):
        got = _settlement_instant("2026-03-03T09:15:00")
        assert got == datetime(2026, 3, 3, 9, 15, tzinfo=timezone.utc)

    def test_the_anchor_does_not_move_with_the_host(self, monkeypatch):
        # Whatever TZ the process thinks it is in, a bare date is the same
        # instant. This is the property that makes a run reproducible.
        import os, time
        before = _settlement_instant("2026-03-03")
        monkeypatch.setenv("TZ", "America/New_York")
        if hasattr(time, "tzset"):
            time.tzset()
        assert _settlement_instant("2026-03-03") == before
