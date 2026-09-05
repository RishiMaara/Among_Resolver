/**
 * Naming the payments, and admitting when the naming is arbitrary.
 *
 * This table is the project's central claim made visible: anyone can total a
 * column, and saying WHICH rows compose a settlement is the part worth
 * building. Two things therefore have to hold — the total must be checkable
 * against the target on sight, and where payments are interchangeable the
 * table must say so rather than implying three specific ids were identified
 * when any three of two thousand would have done.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MatchedPayments, type MatchedTxn } from "@/components/matched-payments";

const txn = (id: string, cents: number): MatchedTxn => ({
  txn_id: id,
  source: "gateway",
  amount_cents: cents,
  currency: "INR",
  timestamp_utc: "2026-08-17T10:00:00Z",
  reference: "STL-1",
  memo: "",
});

const rows = [txn("T1", 1000000), txn("T2", 1500000), txn("T3", 2500000)];

describe("the total against the target", () => {
  it("says '= target' when the set ties out exactly", () => {
    render(<MatchedPayments rows={rows} targetCents={5000000} />);
    expect(screen.getByText(/= target/)).toBeInTheDocument();
  });

  it("says how far short it falls", () => {
    render(<MatchedPayments rows={rows} targetCents={9999900} />);
    expect(screen.getByText(/short by ₹49,999\.00/)).toBeInTheDocument();
  });

  it("says how far over it runs", () => {
    render(<MatchedPayments rows={rows} targetCents={4000000} />);
    expect(screen.getByText(/over by ₹10,000\.00/)).toBeInTheDocument();
  });

  it("renders without a target rather than crashing", () => {
    render(<MatchedPayments rows={rows} />);
    expect(screen.getByText(/₹50,000\.00/)).toBeInTheDocument();
  });
});

describe("naming the payments", () => {
  it("lists every matched payment by id", () => {
    render(<MatchedPayments rows={rows} targetCents={5000000} />);
    for (const id of ["T1", "T2", "T3"]) {
      expect(screen.getByText(id)).toBeInTheDocument();
    }
  });

  it("orders by amount, largest first, because that is where the risk is", () => {
    const { container } = render(<MatchedPayments rows={rows} targetCents={5000000} />);
    // Scope to the table body: the header carries the total plus its
    // "= target" marker in the same node.
    const amounts = [...container.querySelectorAll("tbody tr")].map(
      (tr) => tr.querySelectorAll("td")[1]?.textContent,
    );
    expect(amounts).toEqual(["₹25,000.00", "₹15,000.00", "₹10,000.00"]);
  });

  it("renders nothing at all for an empty set", () => {
    const { container } = render(<MatchedPayments rows={[]} targetCents={100} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("interchangeable payments", () => {
  const interchangeable = {
    groups: [
      {
        amount_cents: 1000000,
        currency: "INR",
        picked: 1,
        identical_available: 2000,
        txn_ids: ["T1"],
      },
    ],
    wholly_interchangeable: false,
  };

  it("marks a row that could have been any of many", () => {
    render(<MatchedPayments rows={rows} targetCents={5000000} interchangeable={interchangeable} />);
    expect(screen.getByText(/any of many/i)).toBeInTheDocument();
  });

  it("points at double-claiming, which is the risk that actually matters", () => {
    render(<MatchedPayments rows={rows} targetCents={5000000} interchangeable={interchangeable} />);
    expect(screen.getByText(/not claimed by another settlement/i)).toBeInTheDocument();
  });

  it("stays silent when nothing is interchangeable", () => {
    render(<MatchedPayments rows={rows} targetCents={5000000} />);
    expect(screen.queryByText(/any of many/i)).not.toBeInTheDocument();
  });
});
