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
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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
vi.mock("@tanstack/react-router", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-router")>()),
  Link: ({ to, children, ...p }: { to: string; children: React.ReactNode }) => (
    <a href={to} {...p}>
      {children}
    </a>
  ),
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

describe("the exception queue", () => {
  it("says how much money is waiting and prices each exception", () => {
    render(
      <ResultsPanel
        results={base({
          exceptions: [
            {
              reason: "unmatched_in_pool",
              candidate_txn_ids: ["A"],
              diagnosis_note: "largest first",
              amount_at_stake_cents: 40000000,
              amount_known: true,
              rank: 1,
            },
            {
              reason: "unmatched_in_pool",
              candidate_txn_ids: ["GHOST"],
              diagnosis_note: "not in the feeds",
              amount_at_stake_cents: 0,
              amount_known: false,
              rank: 2,
            },
          ],
          exceptions_summary: {
            count: 2,
            total_at_stake_cents: 40000000,
            unpriced_count: 1,
            share_of_target: 0.25,
            ordering: "amount at stake, largest first",
          },
        })}
      />,
    );
    expect(screen.getByText(/waiting on review/)).toHaveTextContent("25.0% of the settlement");
    expect(screen.getAllByText(/at stake/).length).toBeGreaterThanOrEqual(2);
    // An exception whose transactions are not in the feeds must not read as
    // "nothing at stake" — the figure is a floor, and says so.
    expect(screen.getByText(/\+ at stake/)).toBeInTheDocument();
  });
});

describe("the calibrated confidence", () => {
  it("shows both figures and says which one decides", () => {
    render(
      <ResultsPanel
        results={base({ summary: { ...base().summary, calibrated_confidence: 0.9848 } })}
      />,
    );
    expect(screen.getByText(/right about 98% of the time/)).toBeInTheDocument();
    expect(
      screen.getByText(/gate reads the engine's score, never the calibrated one/),
    ).toBeTruthy();
  });
});

describe("the investigation", () => {
  const investigated = (valid: boolean) =>
    base({
      summary: { ...base().summary, cleared: false, ambiguous: true },
      investigation: {
        case: {
          target_cents: 6852202,
          tolerance_cents: 5,
          residual_cents: -3,
          withheld_reason: "alternate_subset",
          member_feed: "gateway",
          member_feed_declared: false,
          engine_proposal: [
            {
              id: "pay_0000",
              amount_cents: 451665,
              date: "2026-08-31",
              feed: "gateway",
              names_settlement: true,
            },
            {
              id: "JV00000",
              amount_cents: 451665,
              date: "2026-08-31",
              feed: "erp",
              names_settlement: true,
            },
          ],
          engine_proposal_sum_cents: 903330,
          alternatives: [[], []],
          alternative_sums_cents: [6852202, 6852205],
          pool_size: 45,
          next_working_day: "2026-09-03",
        },
        proposal: {
          action: "MATCH_PROPOSAL",
          txn_ids: ["pay_0000", "JV00000"],
          reason: "The engine's own set sums to the target.",
          proposer: "rules",
        },
        verification: valid
          ? {
              valid: true,
              failed: [],
              plain: "Checked: this proposal is consistent with the data.",
            }
          : {
              valid: false,
              failed: ["1 payment(s) counted twice"],
              plain: "REJECTED before reaching a reviewer: 1 payment(s) counted twice.",
            },
      },
    });

  it("shows a rejected proposal as rejected, with the reason", () => {
    render(<ResultsPanel results={investigated(false)} />);
    expect(screen.getByText(/REJECTED before reaching a reviewer/)).toBeInTheDocument();
    expect(screen.getByText("Propose a set of payments")).toHaveClass("line-through");
    // The case says the feed was assumed, not declared.
    expect(screen.getByText(/assumed — not declared/)).toBeInTheDocument();
  });

  it("shows a verified proposal as a proposal still", () => {
    render(<ResultsPanel results={investigated(true)} />);
    expect(screen.getByText("Propose a set of payments")).not.toHaveClass("line-through");
    expect(screen.getByText(/proposed by fixed rules/)).toBeInTheDocument();
  });
});

describe("exceptions filed by category", () => {
  it("names the category, whose desk it goes to and the first step", () => {
    render(
      <ResultsPanel
        results={base({
          exceptions: [
            {
              reason: "missing_entry",
              candidate_txn_ids: ["BNK1"],
              diagnosis_note: "no counterpart",
              amount_at_stake_cents: 6646636,
              category: "unidentified_receipt",
              category_label: "Unidentified receipt",
              owner: "treasury",
              next_action: "Identify the payer from the narration or UTR.",
            },
          ],
          exceptions_summary: {
            count: 1,
            total_at_stake_cents: 6646636,
            unpriced_count: 0,
            share_of_target: 0.97,
            ordering: "amount at stake, largest first",
            by_category: [
              {
                category: "unidentified_receipt",
                label: "Unidentified receipt",
                owner: "treasury",
                count: 1,
                value_cents: 6646636,
              },
            ],
          },
        })}
      />,
    );
    expect(screen.getByText(/Unidentified receipt · 1 ·/)).toBeInTheDocument();
    expect(screen.getByText(/For treasury:/)).toBeInTheDocument();
    expect(screen.getByText(/Identify the payer from the narration/)).toBeInTheDocument();
  });

  it("points at what is still waiting when a run opened items", () => {
    render(
      <ResultsPanel results={base({ open_items: { opened: 31, closed: 0, not_tracked: 0 } })} />,
    );
    expect(screen.getByRole("link", { name: /See what is waiting/ })).toHaveAttribute(
      "href",
      "/payouts",
    );
  });
});

describe("the fee audit", () => {
  const audit = {
    summary: {
      total_findings: 0,
      high_severity: 0,
      total_overcharge_cents: 0,
      gst_issues: 0,
      tds_compliance: "ok",
      tcs_compliance: "ok",
      settlement_integrity: "ok",
    },
    findings: [],
  };

  it("states a clean result as clean on a clear", () => {
    render(<ResultsPanel results={base({ fee_audit: audit })} />);
    expect(screen.getByText("Fees and tax")).toBeInTheDocument();
    expect(screen.getByText("TDS: ok")).toBeInTheDocument();
  });

  it("is not shown for a withheld proposal", () => {
    render(
      <ResultsPanel
        results={base({ summary: { ...base().summary, cleared: false }, fee_audit: audit })}
      />,
    );
    expect(screen.queryByText("Fees and tax")).not.toBeInTheDocument();
  });
});

describe("the audit receipt", () => {
  it("verifies the trail against the receipt and shows the engine's verdict", async () => {
    const head = "b3edf42203be6ff36e21462d942e5fc926fcf4961d924218285f60ab30cdcffe";
    const fetchMock = vi.fn(async (url: string) => {
      const body = String(url).includes("/verify?receipt=")
        ? { intact: true, plain: "All 40 chained entries check out." }
        : { trail: [] };
      return { ok: true, status: 200, json: async () => body } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ResultsPanel results={base({ audit_head: head })} />);
    fireEvent.click(screen.getByRole("button", { name: /Verify the trail/ }));
    await waitFor(() =>
      expect(screen.getByText("All 40 chained entries check out.")).toBeInTheDocument(),
    );
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes(`receipt=${head}`))).toBe(true);
  });
});

describe("the Tally export", () => {
  const journal = base({
    cash_position: {
      buckets: [],
      notes: [],
      journal: {
        entry_id: "JE-1",
        status: "proposed",
        balanced: true,
        lines: [],
        total_debits_inr: 0,
        total_credits_inr: 0,
      },
    },
  });

  it("is not offered before the posting is approved", async () => {
    render(<ResultsPanel results={journal} />);
    await waitFor(() => expect(screen.getByTestId("journal-approval")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Export for Tally/ })).not.toBeInTheDocument();
  });

  it("is offered once someone approved it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          ({
            ok: true,
            status: 200,
            // The decisions endpoint, as the approval control reads it.
            json: async () => ({
              decisions: [
                {
                  ts: "2026-09-21T10:00:00Z",
                  detail: "bob@x APPROVED the posting proposal JE-1.",
                },
              ],
            }),
          }) as Response,
      ),
    );
    render(<ResultsPanel results={journal} />);
    expect(await screen.findByRole("button", { name: /Export for Tally/ })).toBeInTheDocument();
  });
});
