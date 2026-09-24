/**
 * The demo gate: every screen asks for a name, except one that records none.
 *
 * The judge page was behind it too, so the README's link to it — and the
 * "Judges' brief" link on the sign-in screen itself — led to a login form.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { AuthGate } from "@/components/auth-gate";

vi.mock("@tanstack/react-router", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-router")>()),
  Link: ({ to, children, ...p }: { to: string; children: React.ReactNode }) => (
    <a href={to} {...p}>
      {children}
    </a>
  ),
}));

beforeEach(() => sessionStorage.clear());

describe("the demo sign-in gate", () => {
  it("asks for a name before a screen that records decisions", async () => {
    render(
      <AuthGate>
        <p>the reconciliation screen</p>
      </AuthGate>,
    );
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByText("the reconciliation screen")).not.toBeInTheDocument();
  });

  it("lets anyone read a page that records nothing", () => {
    render(
      <AuthGate open>
        <p>the judge page</p>
      </AuthGate>,
    );
    expect(screen.getByText("the judge page")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Sign in" })).not.toBeInTheDocument();
  });
});
