/**
 * The payouts page: Razorpay's payouts checked, and what is still waiting.
 *
 * What these pin: a payout the engine could not verify reads as not
 * verified, each check says what it found, an item's due date can be
 * explained day by day, and an engine failure reads as a failure.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route } from "@/routes/payouts";

vi.mock("@tanstack/react-router", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-router")>()),
  Link: ({ to, children, ...p }: { to: string; children: React.ReactNode }) => (
    <a href={to} {...p}>
      {children}
    </a>
  ),
}));

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const Payouts = (Route as any).options.component as React.ComponentType;

const RAZORPAY = {
  settlements: 2,
  tally: { verified: 1, verified_with_findings: 0, not_verified: 1 },
  results: [
    {
      settlement_id: "setl_A",
      utr: "UTR1",
      amount_cents: 6807991,
      settled_on: "2026-09-09",
      members: 11,
      status: "verified",
      checks: {
        tie_out: { verdict: "ties_out", plain: "11 line(s) sum to ₹68,079.91." },
        bank: { verdict: "arrived", plain: "Bank credit BNK9100 carries the UTR." },
        fees: { verdict: "clean" },
      },
    },
    {
      settlement_id: "setl_B",
      amount_cents: 100000,
      settled_on: "2026-09-10",
      members: 3,
      status: "not_verified",
      checks: {
        tie_out: { verdict: "does_not_tie_out", plain: "3 line(s) sum to ₹999.00 — off by ₹1.00." },
      },
    },
  ],
  unsettled_lines: { count: 2, value_cents: 179169, plain: "2 line(s) name no settlement yet." },
};

const OPEN = {
  as_of: "2026-09-21",
  calendar: "holidays 0.104, Maharashtra",
  settlement_cycle: "T+2 working days",
  summary: {
    open_count: 1,
    open_value_cents: 451665,
    overdue_count: 1,
    overdue_value_cents: 451665,
    buckets: [{ label: "6-10 working days", count: 1, value_cents: 451665 }],
  },
  items: [
    {
      item_id: "txn:pay_0020",
      kind: "unsettled_payment",
      ref: "pay_0020",
      amount_cents: 451665,
      occurred_on: "2026-09-11",
      due_on: "2026-09-16",
      status: "open",
      first_seen_batch: "SETTLE-001",
      age_working_days: 6,
      overdue_working_days: 3,
    },
  ],
  truncated: 0,
};

function engine(routes: Record<string, unknown>, failing: string[] = []) {
  return vi.fn(async (url: string) => {
    const u = String(url);
    if (u.includes("/sample-data/")) {
      return { ok: true, status: 200, blob: async () => new Blob(["{}"]) } as Response;
    }
    const hit = Object.keys(routes).find((k) => u.includes(k));
    if (failing.some((f) => u.includes(f))) {
      return {
        ok: false,
        status: 503,
        json: async () => ({ detail: { plain: "The engine is asleep." } }),
      } as Response;
    }
    return { ok: true, status: 200, json: async () => (hit ? routes[hit] : {}) } as Response;
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("Razorpay payouts", () => {
  it("checks the sample payouts and says which could not be verified", async () => {
    vi.stubGlobal("fetch", engine({ "razorpay/reconcile/upload": RAZORPAY, "open-items": OPEN }));
    render(<Payouts />);
    await userEvent.click(screen.getByRole("button", { name: /Sample: API responses/ }));
    expect(await screen.findByText(/2 payout\(s\): 1 verified/)).toBeInTheDocument();
    expect(screen.getByText("Not verified")).toBeInTheDocument();
    // A check's finding is readable, not only its colour.
    await userEvent.click(screen.getByRole("button", { name: /setl_B/ }));
    expect(screen.getByText(/off by ₹1\.00/)).toBeInTheDocument();
    expect(screen.getByText("2 line(s) name no settlement yet.")).toBeInTheDocument();
  });

  it("reconciles the dashboard report with no settlements list, and says how it read it", async () => {
    let sent: FormData | undefined;
    const run = {
      ...RAZORPAY,
      read: {
        recon: "dashboard report (CSV)",
        units: "Amounts read as rupees (they carry decimals) and converted to paise.",
        settlements: "derived from the report's own lines",
      },
    };
    const base = engine({ "open-items": OPEN });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (String(url).includes("razorpay/reconcile/upload")) {
          sent = init?.body as FormData;
          return { ok: true, status: 200, json: async () => run } as Response;
        }
        return base(url);
      }),
    );
    render(<Payouts />);
    await userEvent.click(screen.getByRole("button", { name: /Sample: dashboard report/ }));
    expect(await screen.findByText(/Read as a dashboard report \(CSV\)/)).toBeInTheDocument();
    expect(sent?.get("settlements_file")).toBeNull();
    expect((sent?.get("recon_file") as File).name).toBe("settlement_report.csv");
  });

  it("will not send a run without the recon report", async () => {
    const fetchMock = engine({ "open-items": OPEN });
    vi.stubGlobal("fetch", fetchMock);
    render(<Payouts />);
    await userEvent.click(screen.getByRole("button", { name: /Check these payouts/ }));
    expect(screen.getByText(/The recon report is needed/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("razorpay/"))).toBe(false);
  });

  it("shows an engine failure as a failure", async () => {
    vi.stubGlobal("fetch", engine({ "open-items": OPEN }, ["razorpay/reconcile/upload"]));
    render(<Payouts />);
    await userEvent.click(screen.getByRole("button", { name: /Sample: dashboard report/ }));
    expect(await screen.findByText("The engine is asleep.")).toBeInTheDocument();
  });
});

describe("what is still waiting", () => {
  it("shows the money open and overdue, and explains a due date", async () => {
    vi.stubGlobal(
      "fetch",
      engine({
        "open-items": OPEN,
        "calendar/due": {
          captured_on: "2026-09-11",
          t_plus_working_days: 2,
          due_on: "2026-09-16",
          calendar_days: 5,
          skipped: [
            { date: "2026-09-12", closed_because: "second Saturday" },
            { date: "2026-09-14", closed_because: "Ganesh Chaturthi" },
          ],
        },
      }),
    );
    render(<Payouts />);
    expect(await screen.findByText("pay_0020")).toBeInTheDocument();
    expect(screen.getByText(/3 working day\(s\) overdue/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Why this due date/ }));
    await waitFor(() =>
      expect(screen.getByText(/2026-09-14 does not count: Ganesh Chaturthi/)).toBeInTheDocument(),
    );
  });
});
