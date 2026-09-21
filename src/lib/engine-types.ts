/**
 * The shapes the engine actually returns.
 *
 * These were `any` throughout the results panel — 18 of them — which meant
 * TypeScript could not catch the class of defect this project kept hitting:
 * a field renamed on the engine side, or read under the wrong name, showed up
 * as a blank panel at runtime instead of a compile error. `compliance_review`
 * changing from a list to a triaged object is exactly that kind of change.
 *
 * Deliberately permissive at the edges. Fields the engine may omit are
 * optional, and an unknown extra field is not an error — the aim is to catch
 * a typo in a field name, not to freeze the API.
 */

export interface AuditEntry {
  agent: string;
  detail: string;
  timestamp_utc?: string;
  ts?: string;
  batch_id?: string;
}

export interface JournalLine {
  account: string;
  memo?: string;
  debit_inr?: number | null;
  credit_inr?: number | null;
}

export interface Journal {
  entry_id: string;
  status: string;
  balanced: boolean;
  imbalance_cents?: number;
  basis?: string;
  rejection_reason?: string | null;
  lines: JournalLine[];
  total_debits_inr: number;
  total_credits_inr: number;
}

export interface CashBucket {
  key: string;
  label: string;
  count: number;
  amount_cents: number;
  description?: string;
}

export interface CashPositionData {
  batch_id?: string;
  buckets: CashBucket[];
  journal?: Journal | null;
  notes?: string[];
}

export interface ComplianceFindingDetailData {
  rule_id?: string;
  title?: string;
  severity?: string;
  action?: string;
  basis?: string;
  authority?: string;
  citation?: string;
  reference_url?: string;
  why?: string;
  observed?: string;
  remediation?: string;
  threshold_applied?: string;
}

export interface ExceptionRecord {
  reason: string;
  candidate_txn_ids?: string[];
  diagnosis_note?: string;
  requires_human_approval?: boolean;
  // Plural: the engine attaches every rule that fired on the transaction,
  // not one. Writing this as `finding` was the first thing the new types
  // caught — a singular name that would have read as undefined forever.
  findings?: ComplianceFindingDetailData[];
  // Rupees waiting on this exception; the list arrives sorted by it.
  amount_at_stake_cents?: number;
  // False when a named transaction is not in the uploaded feeds, so the
  // figure above is a floor rather than the whole amount.
  amount_known?: boolean;
  rank?: number;
  // The finance category it is filed under, whose desk it goes to, and the
  // first step — exception_taxonomy on the engine side.
  category?: string;
  category_label?: string;
  owner?: string;
  next_action?: string;
}

export interface CategoryCount {
  category: string;
  label: string;
  owner: string;
  count: number;
  value_cents: number;
}

export interface ExceptionsSummary {
  count: number;
  total_at_stake_cents: number;
  unpriced_count: number;
  share_of_target: number | null;
  ordering: string;
  by_category?: CategoryCount[];
}

export interface FeeFinding {
  category: string;
  severity: string;
  txn_id: string;
  expected_cents: number;
  actual_cents: number;
  difference_cents: number;
  payment_method?: string;
  rule_basis?: string;
  citation?: string;
  description: string;
}

export interface FeeAudit {
  summary: {
    total_findings: number;
    high_severity: number;
    total_overcharge_cents: number;
    gst_issues: number;
    tds_compliance: string;
    tcs_compliance: string;
    settlement_integrity: string;
  };
  findings: FeeFinding[];
}

export interface CaseRecord {
  id: string;
  amount_cents: number;
  date: string;
  feed?: string;
  names_settlement: boolean;
  ref?: string;
  memo?: string;
}

/** A withheld settlement's investigation: what was proposed, and the check on it. */
export interface Investigation {
  case: {
    target_cents: number;
    tolerance_cents: number;
    residual_cents: number;
    withheld_reason: string;
    member_feed?: string;
    member_feed_declared?: boolean;
    engine_proposal: CaseRecord[];
    engine_proposal_sum_cents: number;
    alternatives: CaseRecord[][];
    alternative_sums_cents: number[];
    pool_size: number;
    next_working_day: string;
  };
  proposal: {
    action: string;
    txn_ids: string[];
    until_date?: string;
    request?: string;
    party?: string;
    amount_cents?: number;
    reason: string;
    proposer: string;
  };
  verification: { valid: boolean; failed: string[]; plain: string };
}

export interface SettlementAnswer {
  available: boolean;
  answer: string;
  // false when the engine has no recorded results for the batch: it
  // declines to answer from assumption rather than inventing one.
  grounded?: boolean;
}

/** The /reconcile/upload response, as the results panel consumes it. */
export interface ReconcileResult {
  summary: {
    batch_id: string;
    cleared: boolean;
    ambiguous?: boolean;
    withheld_reason?: string | null;
    method: string;
    match_rate: number;
    matched_count: number;
    total_candidates: number;
    confidence?: number;
    exception_count?: number;
    target_cents?: number;
    tie_out_residual_cents?: number;
    fee_basis?: string;
    false_positive_cost_estimate_cents?: number;
    requires_human_approval?: boolean;
    matched_gross_cents?: number;
    deductions_cents?: number;
    exceptions_by_reason?: Record<string, number>;
    // The raw score mapped through the isotonic fit to measured outcomes.
    // Shown beside the score; the clearing gate never reads it.
    calibrated_confidence?: number | null;
  };
  plain_summary?: string;
  reasoning?: string;
  matched_txn_ids?: string[];
  matched_transactions?: import("@/components/matched-payments").MatchedTxn[];
  interchangeable?: import("@/components/matched-payments").Interchangeable | null;
  compliance_review?: import("@/components/compliance-review").ComplianceTriage | null;
  already_settled_elsewhere?: { count: number; summary: string } | null;
  cash_position?: CashPositionData | null;
  exceptions: ExceptionRecord[];
  exceptions_summary?: ExceptionsSummary;
  audit_trail?: AuditEntry[];
  reviewer?: string;
  fee_audit?: FeeAudit | null;
  // The hash at the head of this batch's audit chain when the result was
  // produced — a receipt that later proves nothing before it was altered.
  audit_head?: string | null;
  investigation?: Investigation;
  open_items?: { opened: number; closed: number; not_tracked: number; error?: string };
}
