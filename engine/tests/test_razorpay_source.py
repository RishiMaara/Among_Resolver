"""
Reading settlements from Razorpay instead of from a hand-exported CSV.

Every test here runs against recorded response shapes, not the live service.
That is a real limitation, stated in razorpay_source's docstring too: no test
credentials existed when this was written, so the field names, paths and
subunit convention come from Razorpay's published reference, and the
arithmetic assumption is checked by verify_tie_out() against a real account
before anyone trusts a demo to it.

What these tests do cover is the part that would actually break: the mapping.
A settlement pulled from the recon report reconciles only if credit/debit, the
epoch timestamp, the settlement_id anchor and the sign of a refund are all
handled correctly — and each of those is a place where a wrong guess produces
a plausible-looking number rather than an error.
"""

import base64
import logging
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import razorpay_source as rz  # noqa: E402
from schema import SourceType, TzConfidence  # noqa: E402


def payment(entity_id="pay_1", gross=100_000, settlement_id="setl_A", when=1_757_000_000):
    fee = gross * 2 // 100
    tax = fee * 18 // 100
    return {
        "entity_id": entity_id, "type": "payment", "amount": gross,
        "debit": 0, "credit": gross - fee - tax, "fee": fee, "tax": tax,
        "currency": "INR", "settled": True, "settled_at": when,
        "settlement_id": settlement_id, "order_id": "order_1", "method": "upi",
    }


def refund(entity_id="rfnd_1", amount=50_000, settlement_id="setl_A", when=1_757_000_100):
    return {
        "entity_id": entity_id, "type": "refund", "amount": amount,
        "debit": amount, "credit": 0, "fee": 0, "tax": 0, "currency": "INR",
        "settled": True, "settled_at": when, "settlement_id": settlement_id,
    }


class TestAmounts:
    def test_a_payment_contributes_what_actually_reached_the_account(self):
        # gross 100000, fee 2000, tax 360 -> 97640 net. The settlement pays the
        # net, so the net is what has to sum to it. Using `amount` would
        # overstate every member by its fee and nothing would tie out.
        assert rz.recon_item_to_txn(payment()).amount_cents == 97_640

    def test_a_refund_is_negative(self):
        # Not dropped, not absolute. A refund reduces the payout, and the
        # orchestrator's forced-anchored-negatives handling expects the sign.
        assert rz.recon_item_to_txn(refund()).amount_cents == -50_000

    def test_amounts_stay_integers(self):
        # Subunits in, integer paise out, no float anywhere between.
        assert isinstance(rz.recon_item_to_txn(payment(gross=1_234_567)).amount_cents, int)

    def test_a_line_that_moved_nothing_is_not_a_candidate(self):
        item = payment()
        item["credit"] = item["debit"] = 0
        # It changes no sum, so it is only a free variable for the solver.
        assert rz.recon_item_to_txn(item) is None

    def test_a_line_with_no_entity_id_is_not_a_candidate(self):
        assert rz.recon_item_to_txn(dict(payment(), entity_id="")) is None


class TestTheAnchor:
    def test_settlement_id_becomes_the_reference(self):
        t = rz.recon_item_to_txn(payment(settlement_id="setl_XYZ789"))
        # Canonicalised the way linkage compares references.
        assert t.ref_id_canonical == "SETLXYZ789"

    def test_members_are_selectable_by_settlement(self):
        items = [payment("pay_1", settlement_id="setl_A"),
                 payment("pay_2", settlement_id="setl_B"),
                 refund("rfnd_1", settlement_id="setl_A")]
        assert {i["entity_id"] for i in rz.members_of("setl_A", items)} == {"pay_1", "rfnd_1"}


class TestTimestamps:
    def test_the_epoch_is_converted_rather_than_dropped(self):
        # settled_at is unix seconds. The ingestion timestamp parser does not
        # accept epochs -- a documented gap -- so this path converts it here.
        # Without that, every row on this feed would be dropped silently.
        t = rz.recon_item_to_txn(payment(when=1_757_000_000))
        assert t.timestamp_utc.tzinfo is not None
        assert t.timestamp_utc.utcoffset().total_seconds() == 0

    def test_a_missing_timestamp_is_marked_low_confidence(self):
        item = payment()
        item.pop("settled_at")
        assert rz.recon_item_to_txn(item).tz_confidence == TzConfidence.LOW


class TestTheBatch:
    def test_the_settlement_becomes_the_target(self):
        b = rz.settlement_to_batch({"id": "setl_A", "amount": 47_640,
                                    "currency": "INR", "created_at": 1_757_001_000,
                                    "fees": 0, "tax": 0})
        assert b.batch_id == "setl_A"
        assert b.net_amount_cents == 47_640
        assert b.source == SourceType.BANK

    def test_the_member_feed_is_declared_not_guessed(self):
        # These members came from this settlement's own recon report, so the
        # feed is known. Declaring it is worth 62% -> 75.33% on the benchmark.
        b = rz.settlement_to_batch({"id": "setl_A", "amount": 1, "created_at": 1})
        assert b.member_source == SourceType.GATEWAY

    def test_stated_fees_become_declared_deductions(self):
        b = rz.settlement_to_batch({"id": "setl_A", "amount": 100, "fees": 200,
                                    "tax": 36, "created_at": 1})
        # Declared, so the gross target is a fact rather than a rate-card guess.
        assert b.declared_deductions_cents == 236

    def test_no_fees_means_nothing_is_declared(self):
        b = rz.settlement_to_batch({"id": "setl_A", "amount": 100, "fees": 0,
                                    "tax": 0, "created_at": 1})
        # None, not 0: "no deductions stated" and "deductions stated as zero"
        # are different claims, and the report distinguishes them.
        assert b.declared_deductions_cents is None


class TestTieOut:
    def test_it_reports_a_clean_tie_out(self):
        items = [payment("pay_1", gross=100_000), payment("pay_2", gross=200_000),
                 refund("rfnd_1", amount=50_000)]
        net = sum(i["credit"] - i["debit"] for i in items)
        r = rz.verify_tie_out({"id": "setl_A", "amount": net}, items)
        assert r["ties_out"] is True
        assert r["residual_paise"] == 0
        assert r["members"] == 3

    def test_it_reports_the_discrepancy_rather_than_hiding_it(self):
        r = rz.verify_tie_out({"id": "setl_A", "amount": 999}, [payment("pay_1", gross=100_000)])
        assert r["ties_out"] is False
        # Size and direction, so a mapping error is diagnosable.
        assert r["residual_paise"] == 97_640 - 999


class TestItOnlyTalksToRazorpay:
    """
    Bandit's B310: urlopen follows file:// and custom schemes happily, so a URL
    assembled from a caller-supplied fragment can become a file read or a
    request to somebody else's host. Every caller today passes a literal, but
    that is a property of the callers rather than of this function.
    """

    def test_a_path_that_escapes_the_host_is_refused(self):
        import pytest
        creds = rz.RazorpayCredentials("rzp_test_x", "y")
        # "https://api.razorpay.com/v1" + "@evil.example.com/x" is a URL whose
        # real host is evil.example.com — the api.razorpay.com part becomes
        # userinfo. It passes a naive startswith check and must not pass this one.
        for escape in ("@evil.example.com/steal", ".evil.example.com/x"):
            with pytest.raises(rz.RazorpayError, match="Refusing to open"):
                rz._get(escape, {}, creds, timeout=5)


class TestCredentials:
    def test_absent_credentials_are_none_not_an_error(self, monkeypatch):
        # The engine runs identically without this feed configured.
        monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
        monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
        assert rz.credentials_from_env() is None

    def test_credentials_build_a_basic_auth_header(self, monkeypatch):
        monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abc")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
        c = rz.credentials_from_env()
        assert c.auth_header() == "Basic " + base64.b64encode(b"rzp_test_abc:secret").decode()
        assert c.is_test_mode is True

    def test_a_live_key_is_not_reported_as_test_mode(self):
        assert rz.RazorpayCredentials("rzp_live_x", "s").is_test_mode is False


def test_a_full_settlement_reconciles_against_decoys():
    """
    End to end through the real engine, with the members hidden among lines
    belonging to other settlements — otherwise the pool contains only the
    answer and the test proves nothing.
    """
    from fee_decomposition import FeeRateCard
    from pipeline import reconcile_settlement

    members = [payment(f"pay_M{i}", gross=50_000 + i * 37_000) for i in range(10)]
    members.append(refund("rfnd_M0", amount=41_000))
    others = [payment(f"pay_O{i}", gross=60_000 + i * 11_000,
                      settlement_id=f"setl_OTHER{i % 5}", when=1_757_000_500 + i)
              for i in range(120)]

    net = sum(i["credit"] - i["debit"] for i in members)
    settlement = {"id": "setl_A", "amount": net, "currency": "INR",
                  "created_at": 1_757_001_000, "fees": 0, "tax": 0}
    assert rz.verify_tie_out(settlement, members)["ties_out"]

    pool = [t for t in (rz.recon_item_to_txn(i) for i in members + others) if t]
    logging.disable(logging.WARNING)
    try:
        report = reconcile_settlement(
            rz.settlement_to_batch(settlement), pool, settlement_window_days=5,
            # The recon report's credit is already net of fee and tax, so there
            # is nothing to add back. A default 2%+1% card here would move the
            # target off the answer entirely.
            rate_card=FeeRateCard(gateway_fee_bps=0, flat_fee_cents=0,
                                  tax_withholding_bps=0),
        )
    finally:
        logging.disable(logging.NOTSET)

    m = report.match_result
    assert set(m.matched_txn_ids) == {i["entity_id"] for i in members}
    assert m.cleared is True
    assert report.target_cents - m.matched_sum_cents == 0
