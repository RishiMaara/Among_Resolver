"""
Agent 8 — Cash Position & Posting Proposals.

WHY THIS EXISTS
---------------
The track statement opens "Run the books and the cash position." Everything
upstream of this module answers "which transactions make up this
settlement?" — which is the matching problem, not the finance-ops loop. A
controller does not want a matched set; they want to know what the cash
position is and what to post to the ledger. Without this the loop stops one
step short of the thing the question actually asked for.

So this module turns a reconciliation result into the two artifacts a
finance function actually consumes:

  1. A CASH POSITION — where the money is right now, split into buckets that
     mean something operationally: confirmed in bank, sitting unexplained in
     bank, captured at the gateway but not yet settled, held by compliance,
     and deducted as fees/withholding.

  2. A POSTING PROPOSAL — balanced double-entry journal lines for the
     settlement.

GOVERNANCE
----------
Nothing here posts. Entries are PROPOSED and carry status="proposed"
permanently; there is no code path in this module that marks one posted,
because the orchestrator's governing rule is that no automated component
writes back to a ledger. A human approves, and that approval lives outside
this system.

The balance check is not decorative. An unbalanced journal entry is not a
cosmetic defect — it is a corrupt book. `JournalEntry.is_balanced` is
asserted before any entry is returned, and an entry that cannot be balanced
is returned as a REJECTED proposal with the discrepancy stated, rather than
being quietly rounded into shape.
"""

from __future__ import annotations

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
    Propose the double-entry for a settled gateway batch.

    The economics: the gateway collected `gross` on our behalf, kept a fee,
    withheld tax, and remitted the remainder to the bank. So the bank
    increase, the fee expense and the withheld-tax receivable together
    discharge the gateway clearing balance:

        Dr Bank                     (net actually received)
        Dr Gateway fees             (expense we incurred)
        Dr Tax withheld             (receivable — recoverable, not a cost)
            Cr Gateway clearing     (gross the gateway collected for us)

    Tax withheld is a DEBIT to a receivable rather than an expense on
    purpose: it is money we are owed back, and booking it as a cost would
    understate assets and overstate expenses.

    The gross is taken from the MATCHED TRANSACTIONS, not from the fee model.
    Reconstructing gross from net via the rate card is an estimate the
    tolerance band absorbs; the matched sum is the observed fact. Where the
    two disagree the difference is surfaced as an unexplained residual line
    rather than being absorbed silently — a book that balances because a
    plug was inserted is worse than one that visibly does not.
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
    matched = [t for t in candidates if t.source_txn_id in matched_ids]

    blocked = [t for t in candidates if t.compliance_status is ComplianceStatus.BLOCKED]
    blocked_ids = {t.source_txn_id for t in blocked}

    bank_unmatched = [
        t for t in candidates
        if t.source is SourceType.BANK
        and t.source_txn_id not in matched_ids
        and t.source_txn_id not in blocked_ids
    ]
    gateway_unsettled = [
        t for t in candidates
        if t.source is SourceType.GATEWAY
        and t.source_txn_id not in matched_ids
        and t.source_txn_id not in blocked_ids
    ]

    fees = compute_fee_breakdown(batch, rate_card, matched)
    deductions = fees.total_deductions_cents

    buckets = [
        # `cleared` decides what this bucket is allowed to CLAIM.
        #
        # matched_txn_ids is populated on a failed match too — it holds the
        # best candidate set the solver could assemble, which the engine
        # deliberately does not treat as a clearance. This bucket read it
        # unconditionally, so a settlement the engine had just refused was
        # reported here as "confirmed against the bank credit... received and
        # explained". Measured on a three-settlement pool: the verdict said
        # cleared=False against a target of Rs 51,546, and this line said Rs
        # 1,30,000 was reconciled — the whole pool, including two other
        # settlements' payments, at 2.5x the target.
        #
        # The posting proposal below already gates on `cleared`. The buckets
        # did not, so the panel contradicted the verdict directly above it.
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
