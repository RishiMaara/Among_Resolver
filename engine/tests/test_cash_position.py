"""
Cash position and posting-proposal tests — Agent 8.

The failure mode that matters here is not a crash, it is a journal entry
that looks fine and is wrong. An unbalanced entry is a corrupt book, and an
entry that balances because something was plugged is worse, because nobody
goes looking for it.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datetime import datetime, timedelta, timezone

from schema import (
    NormalizedTxn, SettlementBatch, SourceType, TzConfidence, ComplianceStatus,
)
from fee_decomposition import DEFAULT_RATE_CARD, compute_fee_breakdown
from cash_position import (
    build_cash_position, build_settlement_journal,
    ACC_BANK, ACC_GATEWAY_CLEARING, ACC_GATEWAY_FEES, ACC_TAX_WITHHELD,
)

BASE = datetime(2026, 8, 20, tzinfo=timezone.utc)


def txn(tid, amount_cents, source=SourceType.GATEWAY, hours=1.0):
    return NormalizedTxn(
        source=source,
        source_txn_id=tid,
        ref_id_canonical=f"REF{tid}",
        amount_cents=amount_cents,
        currency="INR",
        timestamp_utc=BASE + timedelta(hours=hours),
        tz_confidence=TzConfidence.HIGH,
    )


def batch_for(gross_cents, batch_id="STL-TEST-1"):
    """Net that the default rate card (2% + 1%) would leave from `gross`."""
    gw = round(gross_cents * DEFAULT_RATE_CARD.gateway_fee_bps / 10_000)
    tax = round(gross_cents * DEFAULT_RATE_CARD.tax_withholding_bps / 10_000)
    net = gross_cents - gw - tax - DEFAULT_RATE_CARD.flat_fee_cents
    return SettlementBatch(
        batch_id=batch_id,
        net_amount_cents=net,
        currency="INR",
        settled_at_utc=BASE + timedelta(hours=72),
        member_source=SourceType.GATEWAY,
    )


class TestJournalBalance:

    def test_entry_balances_for_a_clean_settlement(self):
        members = [txn(f"T{i}", 100_000) for i in range(5)]
        gross = sum(m.amount_cents for m in members)
        entry = build_settlement_journal(batch_for(gross), members)

        assert entry.is_balanced, entry.rejection_reason
        assert entry.imbalance_cents == 0
        assert entry.total_debits_cents == entry.total_credits_cents == gross

    def test_credit_is_the_observed_gross_not_the_fee_estimate(self):
        """
        Gross comes from the matched transactions, which are observed fact.
        Reconstructing it from net via the rate card is an estimate the
        tolerance band absorbs, and booking an estimate would make the ledger
        disagree with the transactions it claims to represent.
        """
        members = [txn("A", 333_333), txn("B", 666_667)]
        gross = 1_000_000
        entry = build_settlement_journal(batch_for(gross), members)

        credit = next(l for l in entry.lines if l.account == ACC_GATEWAY_CLEARING)
        assert credit.credit_cents == gross

    def test_all_four_accounts_present_with_correct_sides(self):
        members = [txn("A", 500_000)]
        entry = build_settlement_journal(batch_for(500_000), members)
        by_acc = {l.account: l for l in entry.lines}

        # Bank, fees and withheld tax are debits; clearing is the credit.
        assert by_acc[ACC_BANK].debit_cents > 0
        assert by_acc[ACC_GATEWAY_FEES].debit_cents > 0
        assert by_acc[ACC_GATEWAY_CLEARING].credit_cents > 0
        # Withheld tax is a RECEIVABLE debit, not an expense — it is money
        # owed back, and expensing it would understate assets.
        assert by_acc[ACC_TAX_WITHHELD].debit_cents > 0
        assert by_acc[ACC_TAX_WITHHELD].credit_cents == 0

    def test_unbalanced_entry_is_rejected_not_plugged(self):
        """
        If the fee estimate disagrees with the observed gross, the entry must
        be refused with the discrepancy stated. A book that balances because
        a plug was inserted is worse than one that visibly does not, because
        nobody goes looking for the plug.
        """
        members = [txn("A", 500_000)]
        # Net inconsistent with the members: the rate card cannot reconcile it.
        bad = SettlementBatch(
            batch_id="STL-BAD",
            net_amount_cents=123_456,
            currency="INR",
            settled_at_utc=BASE + timedelta(hours=72),
        )
        entry = build_settlement_journal(bad, members)

        assert not entry.is_balanced
        assert entry.status == "rejected"
        assert "does not balance" in entry.rejection_reason
        assert entry.imbalance_cents != 0


class TestGovernance:

    def test_nothing_is_ever_marked_posted(self):
        members = [txn("A", 100_000)]
        entry = build_settlement_journal(batch_for(100_000), members)
        assert entry.status in {"proposed", "rejected"}
        assert entry.status != "posted"

    def test_no_journal_when_settlement_did_not_clear(self):
        members = [txn("A", 100_000)]
        pos = build_cash_position(
            batch_for(100_000), members, matched_txn_ids=[], cleared=False,
        )
        assert pos.journal is None
        assert any("did not clear" in n for n in pos.notes)


class TestCashPosition:

    def test_buckets_separate_confirmed_cash_from_expected_cash(self):
        members = [txn(f"T{i}", 100_000) for i in range(3)]
        gross = sum(m.amount_cents for m in members)
        other_gw = [txn("LATER", 250_000)]
        bank_noise = [txn("BANKX", 900_000, source=SourceType.BANK)]

        pos = build_cash_position(
            batch_for(gross),
            members + other_gw + bank_noise,
            matched_txn_ids=[m.source_txn_id for m in members],
            cleared=True,
        )

        assert pos.bucket("reconciled_settled").amount_cents == gross
        # Captured-but-unsettled is a receivable, NOT cash in hand.
        assert pos.bucket("gateway_in_transit").amount_cents == 250_000
        assert pos.bucket("bank_unexplained").amount_cents == 900_000
        assert pos.confirmed_cash_cents == gross

    def test_blocked_transactions_are_isolated_from_other_buckets(self):
        members = [txn("A", 100_000)]
        held = txn("HELD", 5_000_000)
        held.compliance_status = ComplianceStatus.BLOCKED

        pos = build_cash_position(
            batch_for(100_000), members + [held],
            matched_txn_ids=["A"], cleared=True,
        )

        assert pos.bucket("compliance_hold").amount_cents == 5_000_000
        # Must not also be counted as in-transit — that would double-count it
        # and overstate expected cash.
        assert pos.bucket("gateway_in_transit").amount_cents == 0

    def test_deductions_bucket_matches_the_fee_model(self):
        members = [txn("A", 1_000_000)]
        b = batch_for(1_000_000)
        pos = build_cash_position(b, members, ["A"], cleared=True)
        expected = compute_fee_breakdown(b, DEFAULT_RATE_CARD).total_deductions_cents
        assert pos.bucket("deductions").amount_cents == expected

    def test_serializes_without_losing_the_balance_flag(self):
        members = [txn("A", 100_000)]
        pos = build_cash_position(batch_for(100_000), members, ["A"], cleared=True)
        d = pos.to_dict()
        assert d["journal"]["balanced"] is True
        assert d["journal"]["status"] == "proposed"
        assert len(d["journal"]["lines"]) == 4
