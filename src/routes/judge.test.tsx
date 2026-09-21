/**
 * The judge page: live checks show what the engine said, and an engine
 * failure is shown as a failure, never as a result.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route } from "@/routes/judge";
import { MEASURED } from "@/lib/measured";

vi.mock("@tanstack/react-router", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-router")>()),
  Link: ({ to, children, ...p }: { to: string; children: React.ReactNode }) => (
    <a href={to} {...p}>
      {children}
    </a>
  ),
}));

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const Judge = (Route as any).options.component as React.ComponentType;

afterEach(() => vi.unstubAllGlobals());

function card(title: string) {
  return screen.getByText(title).closest("article") as HTMLElement;
}

describe("the judge page", () => {
  it("shows every measured figure with its source file", () => {
    render(<Judge />);
    for (const m of MEASURED) {
      expect(screen.getByText(m.label)).toBeTruthy();
    }
    expect(screen.getAllByText(/\.json$/).length).toBe(MEASURED.length);
  });

  it("runs a live check and shows what the engine returned", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const body = String(url).includes("2026-03-31")
          ? {
              ecommerce_tds: { citation: "Section 194-O, Income-tax Act 1961", rate_percent: 0.1 },
              gst_tcs: {},
            }
          : {
              ecommerce_tds: {
                citation: "Section 393(1), Table Sl. 8(v), Income-tax Act 2025",
                rate_percent: 0.1,
              },
              gst_tcs: { citation: "Section 52, CGST Act 2017", rate_percent: 0.5 },
            };
        return { ok: true, status: 200, json: async () => body } as Response;
      }),
    );
    render(<Judge />);
    const c = card("Tax under the law on the payment's date");
    await userEvent.click(within(c).getByRole("button", { name: /run/i }));
    // The card's own description names both sections too, so read the result lines.
    await waitFor(() => expect(within(c).getAllByRole("listitem").length).toBe(3));
    const lines = within(c)
      .getAllByRole("listitem")
      .map((li) => li.textContent ?? "");
    expect(lines[0]).toMatch(/31 March 2026: Section 194-O/);
    expect(lines[1]).toMatch(/1 April 2026: Section 393\(1\)/);
  });

  it("shows an engine failure as a failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 503,
        json: async () => ({ detail: { plain: "The engine is asleep." } }),
      })) as unknown as typeof fetch,
    );
    render(<Judge />);
    const c = card("What is still waiting, across every run");
    await userEvent.click(within(c).getByRole("button", { name: /run/i }));
    await waitFor(() => expect(within(c).getByText("The engine is asleep.")).toBeTruthy());
  });

  it("names its limits", () => {
    render(<Judge />);
    expect(screen.getByText(/Not yet run against a live Razorpay account/)).toBeTruthy();
  });
});
