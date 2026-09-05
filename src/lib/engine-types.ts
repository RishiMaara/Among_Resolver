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
  audit_trail?: AuditEntry[];
  reviewer?: string;
}
