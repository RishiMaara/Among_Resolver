/**
 * The compliance panel's two load-bearing promises.
 *
 * One: an auto-closed finding must not print an instruction. A DUPLICATE_TX
 * card once said "What to do next: Confirm whether this is a genuine second
 * payment" and, directly beneath it, "Closed automatically — no person needed
 * to look at this". Instructions to do a thing and to not do it, on one card.
 *
 * Two: basis must never be blurred. A rule enforced because the law requires
 * it and one enforced because this firm prefers it are different claims, and
 * an internal rule that reads as statutory is the specific failure this panel
 * exists to prevent.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ComplianceReview, type ComplianceTriage } from "@/components/compliance-review";

// ComplianceDecision fetches prior decisions on mount. It has its own tests;
// stubbing it here keeps these assertions about the PANEL and avoids an
// unawaited state update warning that has nothing to do with what is being
// tested.
vi.mock("@/components/compliance-decision", () => ({
  ComplianceDecision: () => <div data-testid="decision-control" />,
}));

const finding = (over: Partial<ComplianceTriage["findings"][0]> = {}) =>
  ({
    rule_id: "DUPLICATE_TX",
    title: "Identical transaction recorded twice",
    severity: "LOW",
    action: "FLAGGED",
    basis: "internal_policy",
    authority: "AmongResolver internal data-quality control",
    source_name: "Internal data-quality standard",
    citation: "Internal duplicate detection",
    reference_url: "https://bsaaml.ffiec.gov/manual",
    rule_text: "",
    threshold_applied: "Identical amount, timestamp, payer and payee",
    why: "Usually double ingestion of the same record rather than financial crime.",
    observed: "",
    remediation: "Confirm whether this is a genuine second payment or a duplicated record.",
    transaction_count: 2,
    total_amount_cents: 299800,
    transactions: [
      {
        txn_id: "pdup1",
        amount_cents: 149900,
        currency: "INR",
        timestamp_utc: "2026-08-17T13:42:00Z",
        payer_id: "CUST1042",
        memo: "",
      },
    ],
    ...over,
  }) as ComplianceTriage["findings"][0];

const triage = (f: ComplianceTriage["findings"]): ComplianceTriage => ({
  findings: f,
  auto_closed: f.filter((x) => x.auto_disposition?.disposition === "auto").length,
  needs_human: f.filter((x) => x.auto_disposition?.disposition !== "auto").length,
  summary: "test summary",
});

describe("an auto-closed finding", () => {
  const autoClosed = finding({
    auto_disposition: {
      disposition: "auto",
      reason: "Two records identical in amount, timestamp, payer and payee.",
      residual: "If the customer was charged twice it is still a billing issue.",
    },
  });

  it("does not ask the reviewer to do anything", () => {
    render(<ComplianceReview review={triage([autoClosed])} batchId="B1" />);
    expect(screen.queryByText(/What to do next/i)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/Confirm whether this is a genuine second payment/i),
    ).not.toBeInTheDocument();
  });

  it("says it was closed, and states what it did not settle", () => {
    render(<ComplianceReview review={triage([autoClosed])} batchId="B1" />);
    expect(screen.getByText(/Closed automatically/i)).toBeInTheDocument();
    expect(screen.getByText(/still a billing issue/i)).toBeInTheDocument();
  });
});

describe("a finding that needs a person", () => {
  const needsHuman = finding({
    rule_id: "STRUCTURING_PATTERN",
    basis: "regulatory_guidance",
    severity: "MEDIUM",
    auto_disposition: {
      disposition: "human",
      reason: "Supervisory guidance, and the pattern is ambiguous by nature.",
    },
  });

  it("does print what to do next", () => {
    render(<ComplianceReview review={triage([needsHuman])} batchId="B1" />);
    expect(screen.getByText(/What to do next/i)).toBeInTheDocument();
  });

  it("explains why it is the reviewer's call", () => {
    render(<ComplianceReview review={triage([needsHuman])} batchId="B1" />);
    expect(screen.getByText(/Why this one is yours/i)).toBeInTheDocument();
  });
});

describe("basis is never blurred", () => {
  it("marks an internal rule as not law", () => {
    render(<ComplianceReview review={triage([finding()])} batchId="B1" />);
    expect(screen.getByText(/INTERNAL POLICY — NOT LAW/i)).toBeInTheDocument();
    expect(screen.getByText(/No statutory force/i)).toBeInTheDocument();
  });

  it("does not offer an internal rule's link as an official source", () => {
    // The link is real background reading, but calling it "the official
    // source" lends a rule with no statutory force the weight of one.
    render(<ComplianceReview review={triage([finding()])} batchId="B1" />);
    expect(screen.queryByText(/Read the official source/i)).not.toBeInTheDocument();
    expect(screen.getByText(/not the basis for this rule/i)).toBeInTheDocument();
  });

  it("does call a statutory rule's link the official source", () => {
    const statutory = finding({
      rule_id: "CTR_THRESHOLD",
      basis: "statutory",
      reference_url: "https://fiuindia.gov.in/content/ctr.html",
    });
    render(<ComplianceReview review={triage([statutory])} batchId="B1" />);
    expect(screen.getByText(/Read the official source/i)).toBeInTheDocument();
    expect(screen.getByText(/^STATUTORY$/i)).toBeInTheDocument();
  });
});

describe("a clean run", () => {
  it("says so out loud rather than rendering nothing", () => {
    // Silence on a compliance screen reads as "not checked".
    render(
      <ComplianceReview
        review={{ findings: [], auto_closed: 0, needs_human: 0, summary: "" }}
        batchId="B1"
      />,
    );
    expect(screen.getByText(/Screened, nothing flagged/i)).toBeInTheDocument();
  });
});
