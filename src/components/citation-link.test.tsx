/**
 * A citation opens once, in a tab.
 *
 * It used to open twice. The handler called `window.open` and then returned
 * without preventing the default, so the anchor's own `target="_blank"` fired
 * as well — one click, two tabs. And passing a window-features string made
 * browsers open a popup WINDOW rather than a tab, which is intrusive to close
 * and, in an automated browser, blocks scripting on every other tab.
 *
 * Both are the kind of fault nobody reports as a bug. They read as the app
 * being slightly rude.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CitationLink } from "@/components/citation-link";

const URL_UNDER_TEST = "https://fiuindia.gov.in/content/ctr.html";

let openSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  openSpy = vi.fn(() => ({ opener: {} }) as unknown as Window);
  vi.stubGlobal("open", openSpy);
});
afterEach(() => vi.unstubAllGlobals());

describe("an ordinary left click", () => {
  it("opens the citation exactly once", async () => {
    render(<CitationLink url={URL_UNDER_TEST} />);
    await userEvent.click(screen.getByRole("link"));
    expect(openSpy).toHaveBeenCalledTimes(1);
  });

  it("asks for a tab, not a popup window", async () => {
    render(<CitationLink url={URL_UNDER_TEST} />);
    await userEvent.click(screen.getByRole("link"));
    // Any third argument at all turns a tab into a popup.
    expect(openSpy).toHaveBeenCalledWith(URL_UNDER_TEST, "_blank");
  });

  it("prevents the anchor firing as well", async () => {
    render(<CitationLink url={URL_UNDER_TEST} />);
    const link = screen.getByRole("link");
    const evt = new MouseEvent("click", { bubbles: true, cancelable: true });
    link.dispatchEvent(evt);
    // Without this the browser follows target="_blank" on top of window.open.
    expect(evt.defaultPrevented).toBe(true);
  });
});

describe("when the browser refuses to open anything", () => {
  it("says so rather than leaving a dead link", async () => {
    vi.stubGlobal(
      "open",
      vi.fn(() => null),
    );
    render(<CitationLink url={URL_UNDER_TEST} />);
    await userEvent.click(screen.getByRole("link"));
    expect(await screen.findByText(/blocks new tabs/i)).toBeInTheDocument();
    // The address itself has to be visible, because a citation nobody can
    // reach is a claim nobody can check.
    expect(screen.getByText(URL_UNDER_TEST)).toBeInTheDocument();
  });
});

describe("the reader's own intent always wins", () => {
  it("leaves a ctrl-click to the browser", async () => {
    render(<CitationLink url={URL_UNDER_TEST} />);
    const link = screen.getByRole("link");
    link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, ctrlKey: true }));
    expect(openSpy).not.toHaveBeenCalled();
  });

  it("leaves a middle click to the browser", async () => {
    render(<CitationLink url={URL_UNDER_TEST} />);
    screen
      .getByRole("link")
      .dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 1 }));
    expect(openSpy).not.toHaveBeenCalled();
  });
});

describe("the address is obtainable however the click goes", () => {
  it("offers a copy control carrying the full url", async () => {
    render(<CitationLink url={URL_UNDER_TEST} />);
    const copy = screen.getByRole("button", { name: /copy link/i });
    expect(copy).toHaveAttribute("title", `Copy ${URL_UNDER_TEST}`);
  });

  it("shows the address in full when the clipboard refuses", async () => {
    // The environment this most matters in — an embedded preview — is also
    // the one most likely to refuse clipboard access.
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
      configurable: true,
    });
    render(<CitationLink url={URL_UNDER_TEST} />);
    await userEvent.click(screen.getByRole("button", { name: /copy link/i }));
    expect(await screen.findByText(/Copy this address/i)).toBeInTheDocument();
    expect(screen.getByText(URL_UNDER_TEST)).toBeInTheDocument();
  });

  it("does not navigate when the copy control is pressed", async () => {
    render(<CitationLink url={URL_UNDER_TEST} />);
    await userEvent.click(screen.getByRole("button", { name: /copy link/i }));
    // Copying an address must never take the reader out of the app.
    expect(openSpy).not.toHaveBeenCalled();
  });
});

describe("the source stays identifiable", () => {
  it("shows the host as text, openable or not", () => {
    render(<CitationLink url={URL_UNDER_TEST} />);
    expect(screen.getByText(/fiuindia\.gov\.in/)).toBeInTheDocument();
  });

  it("renders nothing when there is no url", () => {
    const { container } = render(<CitationLink url="" />);
    expect(container).toBeEmptyDOMElement();
  });
});
