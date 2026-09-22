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

// OCR runs in a real browser (WebAssembly); here it is stood in for, so the
// test is about what the page does with a reading, not about Tesseract.
vi.mock("@/lib/scan-ocr", () => ({
  readScan: vi.fn(async () => "31/08/2026 NEFT DR VENDOR 85,000.00 11,69,320.00"),
  isScan: vi.fn(async () => true),
  BANK_FILE_TYPES: ".pdf",
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

  it("reconciles the payout, then shows the verifier stopping a proposal", async () => {
    let uploads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const u = String(url);
        if (u.includes("/sample-data/")) {
          return { ok: true, status: 200, blob: async () => new Blob(["x"]) } as Response;
        }
        let body: unknown = {};
        if (u.includes("/verify?receipt=")) {
          body = { intact: true, plain: "All 20 chained entries check out." };
        } else if (u.includes("reconcile/upload")) {
          uploads += 1;
          // The first run declares the member feed; the second must not.
          const declared = (init?.body as FormData).get("member_source");
          body =
            uploads === 1
              ? {
                  summary: { cleared: true, matched_count: 14, confidence: 0.95 },
                  audit_head: "fc172d071420abcdef",
                  plain_summary: "Matched.",
                  declared,
                }
              : {
                  summary: { cleared: false, matched_count: 13, confidence: 0.36 },
                  declared,
                  investigation: {
                    proposal: { proposer: "rules", action: "MATCH_PROPOSAL", reason: "sums" },
                    verification: {
                      valid: false,
                      plain: "REJECTED before reaching a reviewer: 4 payment(s) counted twice.",
                    },
                  },
                };
        }
        return { ok: true, status: 200, json: async () => body } as Response;
      }),
    );
    render(<Judge />);
    const c = card("Reconcile a payout; investigate one it will not clear");
    await userEvent.click(within(c).getByRole("button", { name: /run/i }));
    await waitFor(() => expect(within(c).getByText(/counted twice/)).toBeTruthy());
    const lines = within(c)
      .getAllByRole("listitem")
      .map((li) => li.textContent ?? "");
    expect(lines[0]).toMatch(/^Cleared: 14 payment/);
    expect(lines.some((l) => /Audit receipt fc172d071420…: All 20/.test(l))).toBe(true);
    expect(lines.some((l) => /^Member feed undeclared — Withheld: 13/.test(l))).toBe(true);
    expect(uploads).toBe(2);
  });

  it("reads a scan in the browser and shows what the engine made of it", async () => {
    let sent: FormData | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (String(url).includes("/sample-data/")) {
          return { ok: true, status: 200, blob: async () => new Blob(["%PDF"]) } as Response;
        }
        sent = init?.body as FormData;
        return {
          ok: true,
          status: 200,
          json: async () => ({
            read_by: "OCR in the browser (Tesseract.js)",
            lines: [{}, {}, {}, {}, {}, {}],
            check: { holds: true, plain: "Opening + credits − debits = closing — it balances." },
          }),
        } as Response;
      }),
    );
    render(<Judge />);
    const c = card("A scanned statement, read in your browser");
    await userEvent.click(within(c).getByRole("button", { name: /run/i }));
    await waitFor(() => expect(within(c).getByText(/Read by OCR in the browser/)).toBeTruthy());
    expect(sent?.get("scan_text")).toMatch(/85,000.00/);
    expect(within(c).getByText(/6 line\(s\)/)).toBeTruthy();
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
    expect(screen.getByText(/Not run on a live Razorpay account/)).toBeTruthy();
  });
});
