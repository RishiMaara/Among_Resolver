/**
 * The record of what the engine said, and when.
 *
 * The screen's own claim is that "a batch appearing twice is not an error —
 * reconciling again is normal — but both verdicts stay on the record, and the
 * second never quietly replaces the first". That is the property worth
 * pinning: a repeat run must be visible AS a repeat, because a batch
 * reconciled more than once is exactly what an auditor goes looking for and
 * the screen offers to have counted it for them.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route } from "@/routes/history";

vi.mock("@tanstack/react-router", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-router")>()),
  Link: ({ to, children, ...p }: { to: string; children: React.ReactNode }) => (
    <a href={to} {...p}>
      {children}
    </a>
  ),
}));

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const History = (Route as any).options.component as React.ComponentType;

const run = (over: Record<string, unknown> = {}) => ({
  record: "rec-1",
  timestamp_utc: "2026-08-19T10:00:00Z",
  batch_id: "STL-A",
  reviewer: "alice@example.com",
  status: "cleared",
  confidence: 0.97,
  matched_count: 41,
  total_candidates: 41,
  tie_out_residual_cents: 0,
  audit_trail_entry_count: 14,
  ...over,
});

function stub(body: unknown, ok = true) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok, status: ok ? 200 : 500, json: async () => body }) as Response),
  );
}

beforeEach(() => {
  sessionStorage.setItem(
    "among.session",
    JSON.stringify({ email: "tester@amongresolver.app", signedInAt: "2026-09-02T00:00:00Z" }),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("the run list", () => {
  it("shows a recorded run with its verdict and reviewer", async () => {
    stub({ runs: [run()] });
    render(<History />);
    expect(await screen.findByText("STL-A")).toBeInTheDocument();
    expect(screen.getByText(/alice@example\.com/)).toBeInTheDocument();
    expect(screen.getByText(/cleared/i)).toBeInTheDocument();
  });

  it("shows the audit entry count, which is what an auditor asks for", async () => {
    stub({ runs: [run({ audit_trail_entry_count: 301 })] });
    render(<History />);
    expect(await screen.findByText(/301/)).toBeInTheDocument();
  });
});

describe("a batch reconciled more than once", () => {
  it("is marked as a repeat rather than left to be spotted by scanning", async () => {
    stub({
      runs: [
        run({ record: "r1", batch_id: "STL-DUP", status: "withheld" }),
        run({ record: "r2", batch_id: "STL-DUP", status: "cleared" }),
      ],
    });
    render(<History />);
    // Both verdicts stay on the record; the second never replaces the first,
    // so BOTH rows carry the marker.
    expect((await screen.findAllByText(/×2/)).length).toBe(2);
    expect(screen.getByText(/cleared/i)).toBeInTheDocument();
    expect(screen.getByText(/withheld/i)).toBeInTheDocument();
  });

  it("does not mark a batch that ran once", async () => {
    stub({ runs: [run({ batch_id: "STL-ONCE" })] });
    render(<History />);
    await screen.findByText("STL-ONCE");
    expect(screen.queryByText(/×\d/)).not.toBeInTheDocument();
  });
});

describe("filtering", () => {
  it("narrows by batch id", async () => {
    stub({
      runs: [run({ record: "a", batch_id: "ALPHA" }), run({ record: "b", batch_id: "BETA" })],
    });
    render(<History />);
    await screen.findByText("ALPHA");

    await userEvent.type(screen.getByRole("textbox"), "beta");
    await waitFor(() => expect(screen.queryByText("ALPHA")).not.toBeInTheDocument());
    expect(screen.getByText("BETA")).toBeInTheDocument();
  });

  it("narrows by reviewer, so 'what did alice sign off' is answerable", async () => {
    stub({
      runs: [
        run({ record: "a", batch_id: "ALPHA", reviewer: "alice@example.com" }),
        run({ record: "b", batch_id: "BETA", reviewer: "bob@example.com" }),
      ],
    });
    render(<History />);
    await screen.findByText("ALPHA");

    await userEvent.type(screen.getByRole("textbox"), "bob@");
    await waitFor(() => expect(screen.queryByText("ALPHA")).not.toBeInTheDocument());
    expect(screen.getByText("BETA")).toBeInTheDocument();
  });
});

describe("when there is nothing to show", () => {
  it("says so rather than rendering an empty table", async () => {
    stub({ runs: [] });
    render(<History />);
    expect(await screen.findByText(/No runs recorded yet/i)).toBeInTheDocument();
  });

  it("names the engine as the likely cause when the fetch fails", async () => {
    // A blank screen would leave the reader guessing whether there is no
    // history or no engine.
    stub({}, false);
    render(<History />);
    expect(await screen.findByText(/Is the engine running\?/i)).toBeInTheDocument();
  });
});
