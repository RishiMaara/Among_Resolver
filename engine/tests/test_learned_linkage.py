"""
Learned linkage: Fellegi-Sunter weights, EM, and the settlement cycle.

The claims tested here are narrow on purpose. The model must decline when
nothing identifies members — it may not invent evidence. Given a cycle
learned from verified clears, it must point at the capture-day cohort that
cycle implies, in the settlement's currency, and the arithmetic must still
decide. And what it proposes is a proposal: below the auto-clear gate until
enough measured cases justify more.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

import linkage
import linkage_em
import settlement_cycle
from pipeline import reconcile_settlement
from fee_decomposition import FeeRateCard
from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence

NET = FeeRateCard(gateway_fee_bps=0, flat_fee_cents=0, tax_withholding_bps=0)
SETTLED = datetime(2026, 9, 16, 6, tzinfo=timezone.utc)


def txn(i, amount, days_before, currency="INR", ref=None):
    return NormalizedTxn(
        source=SourceType.GATEWAY, source_txn_id=f"pay_{i:04d}",
        ref_id_canonical=ref or f"ORDER{i:06d}", amount_cents=amount, currency=currency,
        timestamp_utc=SETTLED - timedelta(days=days_before, hours=3),
        tz_confidence=TzConfidence.HIGH,
    )


def pool_with_a_t_plus_one_cycle():
    """Members captured the day before; decoys on other days and in USD."""
    members = [txn(i, 10_000 + 137 * i, 1) for i in range(8)]
    decoys = [txn(100 + i, 10_000 + 211 * i, d) for i, d in
              enumerate([0, 0, 0, 2, 2, 3, 3, 5, 6, 9, 9, 12])]
    foreign = [txn(200 + i, 10_000 + 97 * i, 1, currency="USD") for i in range(6)]
    return members, members + decoys + foreign


def batch_for(members):
    return SettlementBatch(batch_id="SETL-LEARN", net_amount_cents=sum(t.amount_cents for t in members),
                           currency="INR", settled_at_utc=SETTLED,
                           member_source=SourceType.GATEWAY)


def teach_t_plus_one(n=3):
    for k in range(n):
        when = SETTLED - timedelta(days=20 + k)
        settlement_cycle.record_clear(f"HIST-{k}", "gateway", "INR", when,
                                      [when - timedelta(days=1)] * 12)


class TestTheModelDeclinesWithoutEvidence:
    def test_no_anchor_and_no_cycle_means_no_fit(self):
        """
        A single informative comparison cannot identify a two-component
        mixture: any split matching the pool's marginals fits equally well.
        Measured on ReconRiver: EM collapsed to "no members" on every pool.
        """
        vectors = [{"anchor": "no", "ref_cluster": "no", "ref_names_batch": "no",
                    "amount_peer": "no", "currency": "yes", "lag": lag}
                   for lag in ["1"] * 10 + ["0"] * 10 + ["other"] * 30]
        assert linkage_em.fit(vectors, expected_members=10) is None

    def test_linkage_reports_nothing_learned(self):
        members, pool = pool_with_a_t_plus_one_cycle()
        res = linkage.build_candidate_links(batch_for(members), pool, settlement_window_days=15)
        assert res.em is None and not res.learned_keys

    def test_it_can_be_switched_off(self, monkeypatch):
        monkeypatch.setenv("LINKAGE_EM", "0")
        teach_t_plus_one()
        members, pool = pool_with_a_t_plus_one_cycle()
        res = linkage.build_candidate_links(batch_for(members), pool, settlement_window_days=15)
        assert res.em is None


class TestTheSettlementCycle:
    def test_one_clear_is_not_a_cycle(self):
        teach_t_plus_one(n=settlement_cycle.MIN_SETTLEMENTS - 1)
        assert settlement_cycle.profile("gateway", "INR") is None

    def test_enough_clears_are(self):
        teach_t_plus_one()
        prof = settlement_cycle.profile("gateway", "INR")
        assert prof["settlements"] == 3
        assert max(prof["m"], key=prof["m"].get) == "1"

    def test_rerunning_a_batch_is_not_new_evidence(self):
        teach_t_plus_one()
        teach_t_plus_one()
        assert settlement_cycle.profile("gateway", "INR")["settlements"] == 3

    def test_a_cycle_is_kept_per_currency(self):
        teach_t_plus_one()
        assert settlement_cycle.profile("gateway", "USD") is None

    def test_learning_can_be_switched_off(self, monkeypatch):
        monkeypatch.setenv("SETTLEMENT_CYCLE_LEARNING", "0")
        teach_t_plus_one()
        assert settlement_cycle.profile("gateway", "INR") is None

    def test_an_unseen_lag_is_improbable_not_impossible(self):
        teach_t_plus_one()
        m = settlement_cycle.profile("gateway", "INR")["m"]
        assert 0 < m["3"] < 0.05


class TestTheCohort:
    def test_the_cohort_is_the_capture_day_the_cycle_points_at(self):
        teach_t_plus_one()
        members, pool = pool_with_a_t_plus_one_cycle()
        res = linkage.build_candidate_links(batch_for(members), pool, settlement_window_days=15)
        cohort = {c.txn.source_txn_id for c in res.scored if linkage.txn_key(c.txn) in res.learned_keys}
        assert cohort == {t.source_txn_id for t in members}

    def test_the_right_day_in_the_wrong_currency_is_not_in_it(self):
        teach_t_plus_one()
        members, pool = pool_with_a_t_plus_one_cycle()
        res = linkage.build_candidate_links(batch_for(members), pool, settlement_window_days=15)
        usd = {linkage.txn_key(t) for t in pool if t.currency == "USD"}
        assert not (usd & res.learned_keys)

    def test_the_model_says_what_it_learned(self):
        teach_t_plus_one()
        members, pool = pool_with_a_t_plus_one_cycle()
        em = linkage.build_candidate_links(batch_for(members), pool, settlement_window_days=15).em
        assert em["cycle_learned_from"]["settlements"] == 3
        assert em["cohort_size"] == len(members)
        top = em["strongest_evidence"][0]
        assert {"comparison", "level", "m", "u", "weight_bits"} <= set(top)

    def test_constant_comparisons_do_not_drag_lambda_to_zero(self):
        teach_t_plus_one()
        prof = settlement_cycle.profile("gateway", "INR")["m"]
        vectors = [{"anchor": "no", "ref_cluster": "yes", "ref_names_batch": "no",
                    "amount_peer": "yes", "currency": "yes", "lag": lag}
                   for lag in ["1"] * 15 + ["0"] * 15 + ["other"] * 70]
        model = linkage_em.fit(vectors, expected_members=14, lag_m=prof)
        assert model.lam == pytest.approx(0.14, abs=0.01), "the seed stands without anchors"

    def test_anchors_let_em_learn_the_timing_itself(self):
        """With records naming the settlement, EM learns their lag unaided."""
        vectors = ([{"anchor": "yes", "ref_cluster": "no", "ref_names_batch": "no",
                     "amount_peer": "no", "currency": "yes", "lag": "1"}] * 6
                   + [{"anchor": "no", "ref_cluster": "no", "ref_names_batch": "no",
                       "amount_peer": "no", "currency": "yes", "lag": lag}
                      for lag in ["1"] * 2 + ["0"] * 20 + ["other"] * 60])
        model = linkage_em.fit(vectors, expected_members=8)
        assert max(model.m["lag"], key=model.m["lag"].get) == "1"
        truncated = {"anchor": "no", "ref_cluster": "no", "ref_names_batch": "no",
                     "amount_peer": "no", "currency": "yes", "lag": "1"}
        elsewhere = dict(truncated, lag="0")
        assert model.weight(truncated) > model.weight(elsewhere) + 2


class TestEndToEnd:
    def test_the_exact_set_is_proposed_and_not_auto_cleared(self):
        """
        Measured on ReconRiver, the learned band's proposals at its own
        confidence were right 7 of 7 — too few to clear without a person, so
        the band sits below the 0.85 gate and the set is a proposal.
        """
        teach_t_plus_one()
        members, pool = pool_with_a_t_plus_one_cycle()
        report = reconcile_settlement(batch_for(members), pool, settlement_window_days=15,
                                      rate_card=NET)
        m = report.match_result
        assert set(m.matched_txn_ids) == {t.source_txn_id for t in members}
        assert not m.cleared
        # The band plus the small-pool bonus (+0.03) must stay under the gate.
        assert linkage.CONFIDENCE_BANDS["learned_cycle"] + 0.03 < 0.85
        assert m.confidence < 0.85

    def test_without_the_cycle_the_engine_behaves_as_before(self):
        members, pool = pool_with_a_t_plus_one_cycle()
        os.environ["LINKAGE_EM"] = "0"
        try:
            before = reconcile_settlement(batch_for(members), pool, settlement_window_days=15,
                                          rate_card=NET).match_result
        finally:
            os.environ.pop("LINKAGE_EM", None)
        after = reconcile_settlement(batch_for(members), pool, settlement_window_days=15,
                                     rate_card=NET).match_result
        assert before.cleared == after.cleared
        assert set(before.matched_txn_ids) == set(after.matched_txn_ids)

    def test_only_clears_teach(self):
        members, pool = pool_with_a_t_plus_one_cycle()
        batch = batch_for(members)
        settlement_cycle.learn_from(batch, pool, [t.source_txn_id for t in members])
        body = settlement_cycle._load(settlement_cycle._key("gateway", "INR"))
        assert body["members"] == len(members), "USD rows and decoys are not members"


class TestItIsVisible:
    def test_the_report_carries_what_was_learned(self):
        teach_t_plus_one()
        members, pool = pool_with_a_t_plus_one_cycle()
        report = reconcile_settlement(batch_for(members), pool, settlement_window_days=15,
                                      rate_card=NET)
        learned = report.summary()["learned_linkage"]
        assert learned["cohort_size"] == len(members)
        assert learned["cycle_learned_from"]["settlements"] == 3

    def test_the_audit_trail_says_it_in_words(self):
        import audit
        teach_t_plus_one()
        members, pool = pool_with_a_t_plus_one_cycle()
        audit.clear_trail("SETL-LEARN")
        reconcile_settlement(batch_for(members), pool, settlement_window_days=15, rate_card=NET)
        lines = [e["detail"] for e in audit.get_audit_trail("SETL-LEARN")]
        assert any(d.startswith("Learned linkage (Fellegi-Sunter") for d in lines)


class TestOneCyclePerMerchant:
    """Two merchants on one deployment: one settles T+1, the other T+3."""

    def _teach(self, merchant, lag_days):
        from datetime import datetime, timedelta, timezone
        paid = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
        for k in range(3):
            settlement_cycle.record_clear(f"{merchant or 'solo'}-{k}", "gateway", "INR", paid,
                                          [paid - timedelta(days=lag_days)] * 5, merchant=merchant)

    def test_one_merchants_payouts_do_not_teach_anothers(self):
        self._teach("acme-foods", 1)
        self._teach("bolt-retail", 3)
        acme = settlement_cycle.profile("gateway", "INR", merchant="acme-foods")["m"]
        bolt = settlement_cycle.profile("gateway", "INR", merchant="bolt-retail")["m"]
        assert max(acme, key=acme.get) != max(bolt, key=bolt.get)
        assert settlement_cycle.profile("gateway", "INR") is None, "nothing leaked to the default"

    def test_a_single_merchant_deployment_keeps_its_history_key(self):
        self._teach("", 2)
        assert settlement_cycle.profile("gateway", "INR")["settlements"] == 3
        assert settlement_cycle._key("gateway", "INR") == "gateway:INR"
        assert settlement_cycle.merchant_key("  Acme Foods! ") == "acmefoods"
