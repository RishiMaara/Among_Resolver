"""
Unified transaction schema.

Every source (gateway, bank feed, ERP) gets normalized into this shape
by Agent 1 (ingestion.py) before any matching logic runs. Nothing
downstream should ever touch a raw source record.

CRITICAL: amounts are stored as integer cents (int), never float.
Float arithmetic on money is how audits fail. Convert at the boundary
in ingestion.py and never convert back until final display.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class SourceType(str, Enum):
    GATEWAY = "gateway"
    BANK = "bank"
    ERP = "erp"


class TzConfidence(str, Enum):
    HIGH = "high"      # timezone explicit in source data
    INFERRED = "inferred"  # timezone inferred from known source pattern
    LOW = "low"         # ambiguous — must route to exceptions, never guess silently


class ComplianceStatus(str, Enum):
    PASS = "pass"
    FLAGGED = "flagged"
    BLOCKED = "blocked"


class ComplianceBasis(str, Enum):
    """
    What actually backs a compliance rule. This distinction is not
    cosmetic: presenting an internal risk threshold to an auditor as if
    it were a statutory requirement misrepresents the law. Every rule
    must declare which of these it is.
    """
    STATUTORY = "statutory"                      # written into law//binding rules
    REGULATORY_GUIDANCE = "regulatory_guidance"  # supervisor/standard-setter guidance
    INTERNAL_POLICY = "internal_policy"          # this firm's own risk appetite


@dataclass
class ComplianceFinding:
    """
    A single rule hit recorded against a transaction, carrying enough
    context for a human reviewer to understand and act on it without
    re-running the scan or reading the source.
    """
    rule_id: str
    title: str
    severity: str            # LOW | MEDIUM | HIGH
    action: str              # FLAGGED | BLOCKED
    basis: ComplianceBasis
    authority: str           # who sets the underlying obligation
    source_name: str         # name of the source document
    citation: str            # the specific provision relied on
    reference_url: str       # official source a reviewer can open
    rule_text: str           # what the SOURCE actually requires
    threshold_applied: str   # the parameter THIS ENGINE used
    why: str                 # plain-language reason this rule exists
    observed: str            # what was actually seen in THIS transaction
    remediation: str         # what the reviewer should do next


@dataclass
class NormalizedTxn:
    source: SourceType
    source_txn_id: str          # original ID from the source system, unmodified
    ref_id_canonical: str        # normalized/truncation-tolerant reference key
    amount_cents: int            # ALWAYS integer cents. Never float.
    currency: str                # ISO 4217, e.g. "INR"
    timestamp_utc: datetime
    tz_confidence: TzConfidence
    # Whether the FILE said what currency this is, or the value above is a
    # default. Without this the engine cannot tell "this is INR" from "no
    # currency column was present", and the currency guard in the
    # orchestrator — which exists because 100 INR + 100 INR + 100 USD once
    # summed to 300 and cleared at 0.97 — compares a default against a
    # default and waves it through. Timestamps have carried their confidence
    # since the beginning (tz_confidence); currency did not.
    currency_stated: bool = True
    memo_raw: str = ""
    memo_normalized: str = ""    # lowercased, whitespace-collapsed, for fuzzy/semantic pass
    
    # AML / Compliance Fields
    payer_id: str = ""           
    payee_id: str = ""
    is_cash: bool = False
    is_wire_transfer: bool = False
    compliance_status: ComplianceStatus = ComplianceStatus.PASS
    compliance_findings: list["ComplianceFinding"] = field(default_factory=list)

    extra: dict = field(default_factory=dict)  # anything source-specific worth keeping


@dataclass
class SettlementBatch:
    """A single lump-sum deposit that should decompose into N underlying txns."""
    batch_id: str
    net_amount_cents: int        # what actually hit the bank
    currency: str
    settled_at_utc: datetime
    source: SourceType = SourceType.BANK   # the feed the SETTLEMENT arrived on
    memo: str = ""
    ref_id: str = ""

    declared_deductions_cents: int | None = None
    """
    Exact total deductions from the settlement advice, when the source states
    them.

    The gross target is reconstructed as net + deductions, so an error here
    moves the target and the true subset stops summing to it. Estimating the
    deductions from a rate card is a guess whose error the tolerance band has
    to absorb; where the processor actually tells you what it withheld, that
    guess is unnecessary and strictly worse. `fee_decomposition.py` called this the
    preferred path from the beginning — this is the field that makes it
    reachable.
    """

    member_source: SourceType | None = None
    """
    The feed this settlement's MEMBERS live in, when it is known.

    Distinct from `source`, which is where the settlement record itself came
    from: a bank credit whose components are gateway transactions has
    source=BANK and member_source=GATEWAY.

    This matters because the same payment appears in several feeds carrying
    the same amount — a gateway payment is also an ERP ledger entry — so a
    subset-sum can swap one representation for another without the
    arithmetic noticing, or select both and double-count. Declaring the
    member feed removes that whole class of ambiguity, and it is information
    a production system genuinely has: you always know which ledger you are
    reconciling against.

    Left as None when unknown, in which case the engine falls back to
    inferring the member feed from whichever records name the settlement.
    """


@dataclass
class FeeBreakdown:
    """Output of Agent 2 — what to add back to net to get the gross target."""
    batch_id: str
    gateway_fee_cents: int
    flat_fee_cents: int
    tax_withholding_cents: int

    basis: str = "estimated"
    """Where these figures came from: "declared" (stated by the source) or
    "estimated" (reconstructed from a rate card). A reviewer needs to know
    whether the target rests on a fact or on an assumption."""

    split_known: bool = True
    """False when only the TOTAL deduction is known and the split between fee
    and withheld tax is not. Reported rather than invented, because guessing
    the split would misstate a recoverable tax receivable as an expense."""

    @property
    def total_deductions_cents(self) -> int:
        return self.gateway_fee_cents + self.flat_fee_cents + self.tax_withholding_cents

    def gross_target_cents(self, net_amount_cents: int) -> int:
        return net_amount_cents + self.total_deductions_cents


class MatchMethod(str, Enum):
    EXACT_SUBSET_SUM = "exact_subset_sum"     # Agent 3, deterministic
    FUZZY_SEMANTIC = "fuzzy_semantic"          # Agent 4, probabilistic
    MANUAL_REVIEW = "manual_review"            # never auto-cleared


class ExceptionReason(str, Enum):
    TIMING_LAG = "timing_lag"           # waiting on T+2/T+3 leg
    DUPLICATE = "duplicate"
    PARTIAL_PAYMENT = "partial_payment"  # one leg of a split
    MISSING_ENTRY = "missing_entry"
    LOW_CONFIDENCE = "low_confidence"    # fuzzy match below threshold
    COMPLIANCE_BLOCK = "compliance_block" # hard-blocked by Agent 7
    UNRESOLVED = "unresolved"            # no diagnosis found


@dataclass
class MatchResult:
    batch_id: str
    matched_txn_ids: list[str]
    method: MatchMethod
    confidence: float             # 1.0 for exact subset-sum, <1.0 for fuzzy
    matched_sum_cents: int
    target_cents: int
    cleared: bool                 # True only if confidence/tolerance thresholds met
    reasoning: str = ""           # human-readable trail for audit
    ambiguous: bool = False       # True if multiple distinct subsets hit the same
                                    # target — arithmetic alone can't disambiguate,
                                    # this MUST force human review regardless of cleared

    # WHY it was withheld. `ambiguous` is set from three different situations
    # and they call for different actions: a second subset really does hit the
    # target; nothing corroborates the one that does; or the confidence gate
    # caught it. Collapsing all three into one boolean meant any explanation
    # built on it had to guess, and a plain-English summary confidently told a
    # reviewer "more than one set adds up" for a batch where only one did.
    #   alternate_subset        - a genuinely different subset also sums
    #   no_corroborating_evidence - it sums, but no reference/cluster backs it
    #   below_confidence_gate   - cleared on arithmetic, under the threshold
    withheld_reason: str | None = None


@dataclass
class ExceptionRecord:
    batch_id: str
    candidate_txn_ids: list[str]
    reason: ExceptionReason
    diagnosis_note: str
    requires_human_approval: bool = True
    # Populated for COMPLIANCE_BLOCK exceptions so the UI and the audit
    # trail can state WHY a transaction was stopped and point the
    # reviewer at the authority behind it, rather than just asserting
    # "blocked by the Compliance Agent".
    findings: list[ComplianceFinding] = field(default_factory=list)
