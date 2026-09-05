/**
 * The main screen.
 *
 * This route holds the run logic, the detected-settlements panel, the triage
 * list and the error rendering — the largest untested surface in the project
 * and the one where several real defects have lived: a rejection shown as
 * "[object Object]", a typed settlement date silently reverted by a stale
 * closure, and a statement of 15,000 settlements that could only be worked
 * one at a time.
 *
 * The component is reached through Route.options rather than by exporting it,
 * so nothing in production changes shape to make it testable.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route } from "@/routes/index";

// Heavy children with their own concerns. Stubbing them keeps these
// assertions about the screen's own logic.
vi.mock("@/components/agent-flow", () => ({ AgentFlow: () => <div /> }));
vi.mock("@/components/flow-narrative", () => ({ FlowNarrative: () => <div /> }));
vi.mock("@/components/results-panel", () => ({
  ResultsPanel: ({ results }: { results: { summary: { batch_id: string } } }) => (
    <div data-testid="results">{results.summary.batch_id}</div>
  ),
  ResultsSkeleton: () => <div data-testid="skeleton" />,
}));
vi.mock("@tanstack/react-router", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-router")>()),
  Link: ({ children, ...p }: { children: React.ReactNode }) => <a {...p}>{children}</a>,
}));

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const Index = (Route as any).options.component as React.ComponentType;

const SESSION = { email: "tester@amongresolver.app", signedInAt: "2026-09-02T00:00:00Z" };

/** Two settlements, as /settlements/detect returns them. */
const DETECTED = {
  detected: 2,
  returned: 2,
  truncated: false,
  unreadable_amounts: 0,
  date_order: "day",
  date_order_proven: true,
  settlements: [
    {
      batch_id: "STL-A",
      net_amount: 1000,
      settled_at: "2026-08-19T00:00:00Z",
      currency: "INR",
      declared_deductions: 10,
    },
    {
      batch_id: "STL-B",
      net_amount: 2000,
      settled_at: "2026-08-20T00:00:00Z",
      currency: "INR",
      declared_deductions: 20,
    },
  ],
};

const QUEUE_RESULT = {
  results: [
    { batch_id: "STL-A", status: "cleared", plain: "Matched. Nothing left over." },
    { batch_id: "STL-B", status: "withheld", plain: "Needs your decision." },
  ],
};

let fetchMock: ReturnType<typeof vi.fn>;

/** Route each call by URL so tests declare only what they care about. */
function stubEngine(handlers: Record<string, () => unknown>, ok = true) {
  fetchMock = vi.fn(async (url: string) => {
    const key = Object.keys(handlers).find((k) => String(url).includes(k));
    const body = key ? handlers[key]!() : {};
    return {
      ok,
      status: ok ? 200 : 422,
      json: async () => body,
    } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
}

async function attach(file: File, index = 0) {
  const inputs = document.querySelectorAll<HTMLInputElement>('input[type="file"]');
  await userEvent.upload(inputs[index]!, file);
}

const csv = (name: string) => new File(["a,b\n1,2\n"], name, { type: "text/csv" });

beforeEach(() => {
  sessionStorage.setItem("among.session", JSON.stringify(SESSION));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("detected settlements", () => {
  it("offers to reconcile every settlement it found, not just one", async () => {
    stubEngine({ "settlements/detect": () => DETECTED });
    render(<Index />);
    await attach(csv("bank.csv"), 1);

    // The panel used to say "pick one to fill the form" over a statement
    // holding thousands. Finding them and then refusing to act on more than
    // one is a triage list with the triage removed.
    expect(await screen.findByText(/Reconcile all 2/i)).toBeInTheDocument();
  });

  it("reports what the FILE held, not what fits on screen", async () => {
    stubEngine({
      "settlements/detect": () => ({ ...DETECTED, detected: 60, truncated: true }),
    });
    render(<Index />);
    await attach(csv("bank.csv"), 1);

    // A wrong count of the user's own settlements is the product failing at
    // the one thing it claims: never assert what it has not established.
    expect(await screen.findByText(/60 settlements/i)).toBeInTheDocument();
    expect(screen.getByText(/showing the first 2/i)).toBeInTheDocument();
  });

  it("warns when the date order was assumed rather than proven", async () => {
    stubEngine({
      "settlements/detect": () => ({ ...DETECTED, date_order_proven: false }),
    });
    render(<Index />);
    await attach(csv("bank.csv"), 1);
    expect(await screen.findByText(/Dates read as day\/month/i)).toBeInTheDocument();
  });
});

describe("the triage list", () => {
  async function runAll() {
    stubEngine({
      "settlements/detect": () => DETECTED,
      "reconcile/queue": () => QUEUE_RESULT,
    });
    render(<Index />);
    await attach(csv("gateway.csv"), 0);
    await attach(csv("bank.csv"), 1);
    await userEvent.click(await screen.findByText(/Reconcile all 2/i));
    return await screen.findByText(/1 cleared · 1 need you/i);
  }

  it("summarises how much of the run is the reviewer's problem", async () => {
    expect(await runAll()).toBeInTheDocument();
  });

  it("sorts the ones needing attention above the ones that cleared", async () => {
    await runAll();
    const rows = screen
      .getAllByRole("button")
      .filter((b) => /^(cleared|needs you)/i.test(b.textContent ?? ""));
    expect(rows[0]).toHaveTextContent(/needs you/i);
    expect(rows[0]).toHaveTextContent("STL-B");
  });

  it("loads a settlement into the form when its row is clicked", async () => {
    await runAll();
    const row = screen.getAllByRole("button").find((b) => b.textContent?.includes("STL-B"))!;
    await userEvent.click(row);

    // "Pick one" is what this always did; the row just adds a verdict to it.
    await waitFor(() => {
      const batchInput = document.querySelectorAll<HTMLInputElement>(
        'input:not([type="file"])',
      )[0]!;
      expect(batchInput.value).toBe("STL-B");
    });
  });
});

describe("errors reaching the person", () => {
  it("shows the engine's plain rejection, never [object Object]", async () => {
    stubEngine(
      {
        "reconcile/upload": () => ({
          detail: {
            plain: "We could not read this file.\n\nWHAT IS MISSING\n  - the payment amount",
            message: "Cannot process 'x.csv'.",
            rejected: true,
          },
        }),
      },
      false,
    );
    render(<Index />);
    await attach(csv("gateway.csv"), 0);
    await userEvent.click(screen.getByRole("button", { name: /run engine/i }));

    const panel = await screen.findByText(/We could not read this file/i);
    expect(panel).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("[object Object]");
  });

  it("keeps the line breaks the explanation depends on", async () => {
    stubEngine(
      {
        "reconcile/upload": () => ({
          detail: { plain: "Line one.\nLine two.\nLine three." },
        }),
      },
      false,
    );
    render(<Index />);
    await attach(csv("gateway.csv"), 0);
    await userEvent.click(screen.getByRole("button", { name: /run engine/i }));

    const el = await screen.findByText(/Line one\./);
    // A structured block rendered in a plain <p> collapses into one
    // unreadable paragraph.
    expect(el).toHaveClass("whitespace-pre-line");
  });
});

describe("running a reconciliation", () => {
  it("refuses to run with no feed attached", async () => {
    stubEngine({});
    render(<Index />);
    await userEvent.click(screen.getByRole("button", { name: /run engine/i }));
    // Any ONE feed is enough, but none is not.
    expect(fetchMock).not.toHaveBeenCalledWith(
      expect.stringContaining("reconcile/upload"),
      expect.anything(),
    );
  });

  it("renders the report once the engine answers", async () => {
    stubEngine({
      "reconcile/upload": () => ({
        summary: { batch_id: "SETTLE-001", cleared: true },
        exceptions: [],
      }),
      "audit/": () => ({ trail: [] }),
    });
    render(<Index />);
    await attach(csv("gateway.csv"), 0);
    await userEvent.click(screen.getByRole("button", { name: /run engine/i }));

    const results = await screen.findByTestId("results", {}, { timeout: 5000 });
    expect(within(results).getByText("SETTLE-001")).toBeInTheDocument();
  });
});
