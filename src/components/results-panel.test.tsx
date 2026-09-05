/**
 * The report a reviewer reads.
 *
 * This panel had the most defects of anything in the project and every one of
 * them was a screen contradicting itself: a cash bucket labelled "Reconciled &
 * settled — confirmed against the bank credit" on a settlement the engine had
 * just refused, an audit trail that omitted the human half of the process, a
 * posting proposal that still read "awaiting approval" after being approved.
 *
 * So these tests are mostly about agreement. The verdict at the top, the
 * numbers in the middle and the labels at the bottom have to be saying the
 * same thing about the same batch.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { ResultsPanel } from "@/components/results-panel";
import type { ReconcileResult } from "@/lib/engine-types";

// The decision controls read the audit trail on mount and have their own
// tests; stubbing them keeps these assertions about the report.
vi.mock("@/components/review-decision", () => ({
  ReviewDecision: () => <div data-testid="review-decision" />,
}));
vi.mock("@/components/journal-approval", () => ({
  JournalApproval: () => <div data-testid="journal-approval" />,
}));
vi.mock("@/components/compliance-decision", () => ({
  ComplianceDecision: () => <div data-testid="compliance-decision" />,
}));

const base = (over: Partial<ReconcileResult> = {}): ReconcileResult =>
  ({
    summary: {
      batch_id: "STL-1",
      cleared: true,
      method: "exact_subset_sum",
      match_rate: 1,
      matched_count: 3,
      total_candidates: 41,
      confidence: 0.97,
      exception_count: 0,
      target_cents: 5000000,
      tie_out_residual_cents: 0,
    },
    plain_summary: "Matched. We identified the 3 payments that make up this settlement.",
    matched_txn_ids: ["T1", "T2", "T3"],
    matched_transactions: [
      {
        txn_id: "T1",
        source: "gateway",
        amount_cents: 5000000,
        currency: "INR",
        timestamp_utc: "2026-08-17T10:00:00Z",
        reference: "STL-1",
        memo: "",
      },
    ],
    exceptions: [],
    audit_trail: [
      { agent: "subset_sum", detail: "Exact subset-sum match." },
      { agent: "human_reviewer", detail: "alice CONFIRMED this batch." },
    ],
    ...over,
  }) as ReconcileResult;

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ trail: [] }) }) as Response),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("the verdict", () => {
  it("says CLEARED when the engine cleared it", () => {
    render(<ResultsPanel results={base()} />);
    expect(screen.getByText(/^CLEARED$/)).toBeInTheDocument();
  });

  it("does not say CLEARED when the engine withheld it", () => {
    render(
      <ResultsPanel
        results={base({
          summary: { ...base().summary, cleared: false, ambiguous: true },
        })}
      />,
    );
    expect(screen.queryByText(/^CLEARED$/)).not.toBeInTheDocument();
    expect(screen.getByText(/Needs Review/i)).toBeInTheDocument();
  });

  it("leads with the plain statement rather than a status label alone", () => {
    render(<ResultsPanel results={base()} />);
    expect(
      screen.getByText(/We identified the 3 payments that make up this settlement/),
    ).toBeInTheDocument();
  });
});

describe("the cash position", () => {
  const withCash = (cleared: boolean, label: string) =>
    base({
      summary: { ...base().summary, cleared },
      cash_position: {
        buckets: [
          {
            key: "reconciled_settled",
            label,
            count: 3,
            amount_cents: 5000000,
            description: "test",
          },
        ],
        notes: [],
        journal: null,
      },
    });

  it("renders the engine's own bucket label without reinterpreting it", () => {
    // The engine decides what the bucket may CLAIM; the panel must not
    // upgrade a refusal into a confirmation on the way to the screen.
    render(<ResultsPanel results={withCash(false, "Proposed set — NOT confirmed")} />);
    expect(screen.getByText(/Proposed set — NOT confirmed/)).toBeInTheDocument();
    expect(screen.queryByText(/^Reconciled & settled$/)).not.toBeInTheDocument();
  });

  it("shows the cleared label when the settlement actually cleared", () => {
    render(<ResultsPanel results={withCash(true, "Reconciled & settled")} />);
    expect(screen.getByText(/Reconciled & settled/)).toBeInTheDocument();
  });
});

describe("the posting proposal", () => {
  const withJournal = (status: string) =>
    base({
      cash_position: {
        buckets: [],
        notes: [],
        journal: {
          entry_id: "JE-1",
          status,
          balanced: true,
          lines: [{ account: "1010 · Bank", debit_inr: 1000, credit_inr: null, memo: "x" }],
          total_debits_inr: 1000,
          total_credits_inr: 1000,
        },
      },
    });

  it("offers the approval control while the proposal is open", () => {
    render(<ResultsPanel results={withJournal("proposed")} />);
    expect(screen.getByTestId("journal-approval")).toBeInTheDocument();
  });

  it("does not offer it once the engine itself rejected the entry", () => {
    render(<ResultsPanel results={withJournal("rejected")} />);
    expect(screen.queryByTestId("journal-approval")).not.toBeInTheDocument();
  });
});

describe("the audit trail", () => {
  it("counts the human decisions rather than burying them in the list", () => {
    render(<ResultsPanel results={base()} />);
    // "who signed this off" should be answerable from the header.
    expect(screen.getByText(/1 human decision/i)).toBeInTheDocument();
  });

  it("renders no trail panel when there are no entries", () => {
    render(<ResultsPanel results={base({ audit_trail: [] })} />);
    // The phrase appears in other copy on this screen, so match the heading,
    // which always carries its entry count.
    expect(screen.queryByText(/Audit Trail \(/)).not.toBeInTheDocument();
  });
});

describe("compliance", () => {
  it("says a clean run was screened rather than showing nothing", () => {
    render(
      <ResultsPanel
        results={base({
          compliance_review: { findings: [], auto_closed: 0, needs_human: 0, summary: "" },
        })}
      />,
    );
    // Silence on a compliance screen reads as "not checked".
    expect(screen.getByText(/Screened, nothing flagged/i)).toBeInTheDocument();
  });
});

describe("the matched payments", () => {
  it("names them rather than only counting them", () => {
    render(<ResultsPanel results={base()} />);
    expect(screen.getByText("T1")).toBeInTheDocument();
  });

  it("survives a response that carries none", () => {
    render(<ResultsPanel results={base({ matched_transactions: [] })} />);
    expect(screen.getByText(/^CLEARED$/)).toBeInTheDocument();
  });
});
