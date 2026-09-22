"""
Cash position and posting proposals from a reconciliation result.

Turns a matched set into what finance consumes: where the money is (in bank,
unexplained in bank, captured not yet settled, held by compliance, deducted
as fees/tax) and a balanced double-entry proposal. Nothing here posts;
entries stay "proposed". Balance is asserted, and an entry that cannot
balance is returned REJECTED with the discrepancy, never rounded into shape.
"""

from __future__ import annotations

from linkage import txn_key

from dataclasses import dataclass, field
from datetime import datetime, timezone

from schema import (
    NormalizedTxn,
    SettlementBatch,
    SourceType,
    ComplianceStatus,
    ExceptionReason,
)
from fee_decomposition import compute_fee_breakdown, FeeRateCard, DEFAULT_RATE_CARD


# Chart-of-accounts labels. Deliberately generic — a real deployment maps
# these to its own GL codes, and hard-coding someone else's codes here would
# be worse than useless.
ACC_BANK = "1010 · Bank — Settlement Account"
ACC_GATEWAY_CLEARING = "1200 · Gateway Clearing"
ACC_GATEWAY_FEES = "6100 · Payment Gateway Fees"
ACC_TAX_WITHHELD = "1450 · Tax Withheld at Source (receivable)"


@dataclass
class JournalLine:
    account: str
    debit_cents: int = 0
    credit_cents: int = 0
    memo: str = ""


@dataclass
class JournalEntry:
    entry_id: str
    batch_id: str
    date_utc: datetime
    lines: list[JournalLine]
    basis: str
    status: str = "proposed"      # never becomes "posted" in this module
    rejection_reason: str = ""

    @property
    def total_debits_cents(self) -> int:
        return sum(l.debit_cents for l in self.lines)

    @property
    def total_credits_cents(self) -> int:
        return sum(l.credit_cents for l in self.lines)

    @property
    def imbalance_cents(self) -> int:
        return self.total_debits_cents - self.total_credits_cents

    @property
    def is_balanced(self) -> bool:
        return self.imbalance_cents == 0


@dataclass
class CashBucket:
    key: str
    label: str
    count: int
    amount_cents: int
    description: str


@dataclass
class CashPosition:
    batch_id: str
    as_of_utc: datetime
    buckets: list[CashBucket]
    journal: JournalEntry | None = None
    notes: list[str] = field(default_factory=list)

    def bucket(self, key: str) -> CashBucket | None:
        return next((b for b in self.buckets if b.key == key), None)

    @property
    def confirmed_cash_cents(self) -> int:
        b = self.bucket("reconciled_settled")
        return b.amount_cents if b else 0

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "as_of_utc": self.as_of_utc.isoformat(),
            "buckets": [
                {
                    "key": b.key,
                    "label": b.label,
                    "count": b.count,
                    "amount_cents": b.amount_cents,
                    "amount_inr": round(b.amount_cents / 100, 2),
                    "description": b.description,
                }
                for b in self.buckets
            ],
            "journal": (
                {
                    "entry_id": self.journal.entry_id,
                    "date_utc": self.journal.date_utc.isoformat(),
                    "status": self.journal.status,
                    "basis": self.journal.basis,
                    "balanced": self.journal.is_balanced,
                    "imbalance_cents": self.journal.imbalance_cents,
                    "rejection_reason": self.journal.rejection_reason,
                    "total_debits_inr": round(self.journal.total_debits_cents / 100, 2),
                    "total_credits_inr": round(self.journal.total_credits_cents / 100, 2),
                    "lines": [
                        {
                            "account": l.account,
                            "debit_inr": round(l.debit_cents / 100, 2),
                            "credit_inr": round(l.credit_cents / 100, 2),
                            "memo": l.memo,
                        }
                        for l in self.journal.lines
                    ],
                }
                if self.journal
                else None
            ),
            "notes": self.notes,
        }


def build_settlement_journal(
    batch: SettlementBatch,
    matched: list[NormalizedTxn],
    rate_card: FeeRateCard = DEFAULT_RATE_CARD,
) -> JournalEntry:
    """
    Propose the double entry for a settled gateway batch:

        Dr Bank              net received
        Dr Gateway fees      expense
        Dr Tax withheld      receivable (recoverable, not a cost)
        Cr Gateway clearing  gross collected

    Gross comes from the matched transactions, not the fee model; any gap is an
    explicit unexplained-residual line rather than a silent plug.
    """
    gross = sum(t.amount_cents for t in matched)
    net = batch.net_amount_cents
    fees = compute_fee_breakdown(batch, rate_card, matched)

    fee_cents = fees.gateway_fee_cents + fees.flat_fee_cents
    tax_cents = fees.tax_withholding_cents

    lines = [
        JournalLine(ACC_BANK, debit_cents=net,
                    memo=f"Settlement {batch.batch_id} received"),
        JournalLine(ACC_GATEWAY_FEES, debit_cents=fee_cents,
                    memo="Gateway processing fees"),
        JournalLine(ACC_TAX_WITHHELD, debit_cents=tax_cents,
                    memo="Tax withheld at source (recoverable)"),
        JournalLine(ACC_GATEWAY_CLEARING, credit_cents=gross,
                    memo=f"{len(matched)} transaction(s) reconciled to this settlement"),
    ]

    entry = JournalEntry(
        entry_id=f"JE-{batch.batch_id}",
        batch_id=batch.batch_id,
        date_utc=batch.settled_at_utc,
        lines=lines,
        basis=(
            f"Gross Rs {gross / 100:,.2f} from {len(matched)} matched transaction(s); "
            f"net Rs {net / 100:,.2f} received; fees Rs {fee_cents / 100:,.2f}; "
            f"tax withheld Rs {tax_cents / 100:,.2f}."
        ),
    )

    if not entry.is_balanced:
        # Do not plug it. State the discrepancy and refuse the proposal —
        # the fee estimate disagreeing with the observed gross is exactly
        # the kind of thing a controller must see, not have smoothed over.
        diff = entry.imbalance_cents
        entry.status = "rejected"
        entry.rejection_reason = (
            f"Entry does not balance by Rs {abs(diff) / 100:,.2f} "
            f"(debits Rs {entry.total_debits_cents / 100:,.2f} vs credits "
            f"Rs {entry.total_credits_cents / 100:,.2f}). The rate-card fee "
            f"estimate does not reconcile with the observed matched gross. "
            f"Confirm the actual deductions from the settlement advice before "
            f"posting; this proposal is withheld rather than balanced with a plug."
        )

    return entry


def build_cash_position(
    batch: SettlementBatch,
    candidates: list[NormalizedTxn],
    matched_txn_ids: list[str],
    exceptions: list | None = None,
    matched_keys: list[str] | None = None,
    cleared: bool = False,
    rate_card: FeeRateCard = DEFAULT_RATE_CARD,
    as_of: datetime | None = None,
) -> CashPosition:
    """
    Where the money is, given this reconciliation.

    Buckets are chosen to be operationally actionable rather than merely
    descriptive — each one implies a different next move for a controller:
    unexplained bank credits need investigating, in-transit gateway captures
    need chasing, compliance holds need a decision.
    """
    as_of = as_of or datetime.now(timezone.utc)
    matched_ids = set(matched_txn_ids)
    # By txn_key when the result carries keys: a bare id can also name a
    # record in another feed (FAILURE_LOG 38).
    keys = set(matched_keys or [])
    matched = ([t for t in candidates if txn_key(t) in keys] if keys
               else [t for t in candidates if t.source_txn_id in matched_ids])
    matched_key_set = {txn_key(t) for t in matched}

    blocked = [t for t in candidates if t.compliance_status is ComplianceStatus.BLOCKED]
    blocked_ids = {t.source_txn_id for t in blocked}

    bank_unmatched = [
        t for t in candidates
        if t.source is SourceType.BANK
        and txn_key(t) not in matched_key_set
        and t.source_txn_id not in blocked_ids
    ]
    gateway_unsettled = [
        t for t in candidates
        if t.source is SourceType.GATEWAY
        and txn_key(t) not in matched_key_set
        and t.source_txn_id not in blocked_ids
    ]

    fees = compute_fee_breakdown(batch, rate_card, matched)
    deductions = fees.total_deductions_cents

    buckets = [
        # Only a CLEARED result may claim money as reconciled: a withheld result still
        # carries its best candidate set, which this bucket once reported as confirmed.
        CashBucket(
            key="reconciled_settled",
            label=("Reconciled & settled" if cleared
                   else "Proposed set — NOT confirmed"),
            count=len(matched),
            amount_cents=sum(t.amount_cents for t in matched),
            description=(
                (
                    "Gross value of transactions matched to this settlement "
                    "and confirmed against the bank credit. This is the only "
                    "cash that is both received and explained."
                ) if cleared else (
                    "This settlement did NOT clear, so nothing here is "
                    "confirmed. These are the transactions the matcher could "
                    "assemble before it stopped; they have not been tied to "
                    "the bank credit and must not be treated as received or "
                    "explained. Resolve the exceptions first."
                )
            ),
        ),
        CashBucket(
            key="deductions",
            label="Fees & tax withheld",
            count=1,
            amount_cents=deductions,
            description=(
                "Gateway fees plus tax withheld at source. Deducted from "
                "gross before remittance; the withheld tax is recoverable, "
                "not a cost."
            ),
        ),
        CashBucket(
            key="bank_unexplained",
            label="In bank, unexplained",
            count=len(bank_unmatched),
            amount_cents=sum(t.amount_cents for t in bank_unmatched),
            description=(
                "Bank credits in the settlement window not attributed to this "
                "settlement. Cash is in hand but unaccounted for — these need "
                "investigating before period close."
            ),
        ),
        CashBucket(
            key="gateway_in_transit",
            label="Captured, not yet settled",
            count=len(gateway_unsettled),
            amount_cents=sum(t.amount_cents for t in gateway_unsettled),
            description=(
                "Gateway transactions in the window not part of this "
                "settlement. Expected to arrive in a later payout — this is "
                "the receivable position, not cash in hand."
            ),
        ),
        CashBucket(
            key="compliance_hold",
            label="Held by compliance",
            count=len(blocked),
            amount_cents=sum(t.amount_cents for t in blocked),
            description=(
                "Blocked by the Compliance Agent and excluded from "
                "reconciliation. Cannot be released without a human decision."
            ),
        ),
    ]

    notes: list[str] = []
    journal: JournalEntry | None = None

    if cleared and matched:
        journal = build_settlement_journal(batch, matched, rate_card)
        if journal.status == "rejected":
            notes.append(
                "Posting proposal withheld — see rejection_reason on the journal."
            )
        else:
            notes.append(
                "Posting proposal is balanced and ready for human approval. "
                "Nothing has been posted; this engine never writes to a ledger."
            )
    else:
        notes.append(
            "No posting proposed: the settlement did not clear, so there is no "
            "confirmed set of transactions to book against. Resolve the "
            "exceptions first."
        )

    if blocked:
        notes.append(
            f"{len(blocked)} transaction(s) are on compliance hold and are "
            f"excluded from every bucket above except 'Held by compliance'."
        )

    return CashPosition(
        batch_id=batch.batch_id,
        as_of_utc=as_of,
        buckets=buckets,
        journal=journal,
        notes=notes,
    )
