/**
 * Every screen can reach every other one.
 *
 * The escalations page had no header at all. Its only link home sat inside a
 * table row, so a reviewer sent there — which is the entire point of the
 * page — arrived at a dead end. The rulebook could reach home and nowhere
 * else.
 *
 * A missing link is invisible in every other kind of test: the page renders,
 * its content is correct, and the person looking at it is stuck. So this
 * asserts the graph rather than any one screen.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@tanstack/react-router", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-router")>()),
  Link: ({ to, children, ...p }: { to: string; children: React.ReactNode }) => (
    <a href={to} {...p}>
      {children}
    </a>
  ),
}));

import { Route as IndexRoute } from "@/routes/index";
import { Route as HistoryRoute } from "@/routes/history";
import { Route as RulebookRoute } from "@/routes/rulebook";
import { Route as EscalationsRoute } from "@/routes/escalations";

/* eslint-disable @typescript-eslint/no-explicit-any */
const SCREENS: Record<string, { route: any; self: string }> = {
  "Agent Flow": { route: IndexRoute, self: "/" },
  History: { route: HistoryRoute, self: "/history" },
  Rulebook: { route: RulebookRoute, self: "/rulebook" },
  Escalations: { route: EscalationsRoute, self: "/escalations" },
};
/* eslint-enable @typescript-eslint/no-explicit-any */

const ALL = ["/", "/history", "/rulebook", "/escalations"];

beforeEach(() => {
  sessionStorage.setItem(
    "among.session",
    JSON.stringify({ email: "tester@amongresolver.app", signedInAt: "2026-09-02T00:00:00Z" }),
  );
  // Screens fetch on mount; nothing here should reach a network.
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }) as Response),
  );
});

afterEach(() => vi.unstubAllGlobals());

describe.each(Object.entries(SCREENS))("%s", (name, { route, self }) => {
  const Screen = route.options.component as React.ComponentType;

  it("links to every other screen", () => {
    render(<Screen />);
    const hrefs = new Set(screen.getAllByRole("link").map((a) => a.getAttribute("href")));
    for (const target of ALL.filter((t) => t !== self)) {
      expect(hrefs, `${name} cannot reach ${target}`).toContain(target);
    }
  });
});

describe("getting back to the main screen", () => {
  it("is possible from every secondary screen", () => {
    // The specific failure: a page you are sent to, that you cannot leave.
    for (const [name, { route, self }] of Object.entries(SCREENS)) {
      if (self === "/") continue;
      const Screen = route.options.component as React.ComponentType;
      const { unmount } = render(<Screen />);
      const home = screen.getAllByRole("link").filter((a) => a.getAttribute("href") === "/");
      expect(home.length, `${name} has no way home`).toBeGreaterThan(0);
      unmount();
    }
  });
});
