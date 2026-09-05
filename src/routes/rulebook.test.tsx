/**
 * The published control set.
 *
 * This screen exists to keep one distinction visible: a rule enforced because
 * the law requires it and a rule enforced because this firm prefers it are
 * different claims. Everything else here is presentation; that is the
 * substance, and it is the thing that has actually gone wrong before — every
 * internal rule once carried a link labelled "Read the official source"
 * pointing at FIU-IND, FATF or the US BSA/AML manual, on a card that also
 * said NOT LAW.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route } from "@/routes/rulebook";

vi.mock("@tanstack/react-router", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-router")>()),
  Link: ({ to, children, ...p }: { to: string; children: React.ReactNode }) => (
    <a href={to} {...p}>
      {children}
    </a>
  ),
}));

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const Rulebook = (Route as any).options.component as React.ComponentType;

const rule = (over: Record<string, unknown> = {}) => ({
  rule_id: "CTR_THRESHOLD",
  title: "Cash transaction at the reporting threshold",
  severity: "MEDIUM",
  action: "FLAGGED",
  basis: "statutory",
  authority: "FIU-IND / PMLA 2002",
  source_name: "PML Rules 2005",
  citation: "PML Rules 2005 r.3",
  reference_url: "https://fiuindia.gov.in/content/ctr.html",
  rule_text: "Cash transactions above the threshold must be reported.",
  threshold_applied: "Single cash transaction >= Rs 10,00,000",
  why: "A reporting duty, not a prohibition.",
  observed: "",
  remediation: "Include in the CTR filing.",
  ...over,
});

const INTERNAL = rule({
  rule_id: "DUPLICATE_TX",
  title: "Identical transaction recorded twice",
  basis: "internal_policy",
  severity: "LOW",
  authority: "AmongResolver internal data-quality control",
  reference_url: "https://bsaaml.ffiec.gov/manual",
  threshold_applied: "Identical amount, timestamp, payer and payee",
  why: "Usually double ingestion rather than financial crime.",
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

describe("basis is the loudest thing on a card", () => {
  it("says NOT LAW on an internal rule", async () => {
    stub({ rules: [INTERNAL] });
    render(<Rulebook />);
    expect(await screen.findByText(/INTERNAL POLICY — NOT LAW/i)).toBeInTheDocument();
  });

  it("marks a statutory rule as statutory", async () => {
    stub({ rules: [rule()] });
    render(<Rulebook />);
    expect(await screen.findByText(/^STATUTORY$/i)).toBeInTheDocument();
  });

  it("says a supervisory threshold is this engine's calibration", async () => {
    stub({ rules: [rule({ rule_id: "STRUCTURING_PATTERN", basis: "regulatory_guidance" })] });
    render(<Rulebook />);
    expect(await screen.findByText(/REGULATORY GUIDANCE/i)).toBeInTheDocument();
    // This screen's own wording. The compliance panel says the same thing
    // differently ("this engine's calibration, not a legal line"); what both
    // must do is separate the obligation from the parameters this system
    // chose to detect it with.
    expect(screen.getByText(/the detection parameters are ours/i)).toBeInTheDocument();
  });
});

describe("what each rule has to state", () => {
  it("shows the threshold this engine actually applied", async () => {
    stub({ rules: [rule()] });
    render(<Rulebook />);
    // The rule text is what the SOURCE requires; the threshold is what this
    // system chose. Showing one without the other hides the choice.
    expect(await screen.findByText(/Single cash transaction >= Rs 10,00,000/)).toBeInTheDocument();
  });

  it("shows why the rule exists and what to do about it", async () => {
    stub({ rules: [rule()] });
    render(<Rulebook />);
    expect(await screen.findByText(/A reporting duty, not a prohibition/)).toBeInTheDocument();
    expect(screen.getByText(/Include in the CTR filing/)).toBeInTheDocument();
  });

  it("names the authority behind it", async () => {
    stub({ rules: [rule()] });
    render(<Rulebook />);
    expect(await screen.findByText(/FIU-IND \/ PMLA 2002/)).toBeInTheDocument();
  });
});

describe("filtering", () => {
  it("narrows by rule id or title", async () => {
    stub({ rules: [rule(), INTERNAL] });
    render(<Rulebook />);
    await screen.findByText(/CTR_THRESHOLD/);

    await userEvent.type(screen.getByRole("textbox"), "duplicate");
    await waitFor(() => expect(screen.queryByText(/CTR_THRESHOLD/)).not.toBeInTheDocument());
    expect(screen.getByText(/DUPLICATE_TX/)).toBeInTheDocument();
  });

  it("says so when a search matches nothing", async () => {
    stub({ rules: [rule()] });
    render(<Rulebook />);
    await screen.findByText(/CTR_THRESHOLD/);

    await userEvent.type(screen.getByRole("textbox"), "zzzz-no-such-rule");
    expect(await screen.findByText(/No rules found/i)).toBeInTheDocument();
  });
});

describe("when the engine cannot be reached", () => {
  it("reports it rather than showing an empty rulebook", async () => {
    // An empty compliance rulebook reads as "no rules apply", which is the
    // most dangerous thing this screen could imply.
    stub({}, false);
    render(<Rulebook />);
    await waitFor(() => expect(screen.queryByText(/CTR_THRESHOLD/)).not.toBeInTheDocument());
    expect(document.body.textContent).not.toMatch(/^\s*$/);
  });
});
