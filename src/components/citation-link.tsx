/**
 * A citation that stays useful when the browser refuses to open it.
 *
 * THE ACTUAL PROBLEM
 * ------------------
 * The markup was never wrong. `target="_blank" rel="noopener noreferrer"` is
 * the correct way to link out, and it works in a real browser tab. What fails
 * is the environment: measured in the preview pane, `window.open` returns
 * null — new tabs are blocked — so the click does nothing and the citation
 * looks broken.
 *
 * Worth separating two things that get conflated here. `X-Frame-Options:
 * DENY`, which FIU-IND and FATF do send, prevents a site being EMBEDDED in an
 * iframe. It has no bearing on navigating to it in a new tab. So the header is
 * real but it is not what blocks these links; popup suppression is.
 *
 * WHAT THIS DOES ABOUT IT
 * -----------------------
 * A compliance claim is only worth as much as a reader's ability to check it,
 * so the citation must survive the click being swallowed. Three ways out,
 * in order:
 *
 *   1. A real anchor, so a normal browser — and middle-click, and "open in
 *      new tab" — behaves exactly as expected.
 *   2. If the click is swallowed, the URL goes to the clipboard and the
 *      reader is told what happened, rather than being left with a dead link.
 *   3. The host is always shown as text, so the source is identifiable even
 *      when nothing can be opened and nothing can be copied.
 */

import { useState } from "react";

function hostOf(url: string): string {
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return url.slice(0, 40);
  }
}

export function CitationLink({
  url,
  label,
  className = "",
}: {
  url: string;
  label?: string;
  className?: string;
}) {
  const [note, setNote] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  async function copy(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2500);
    } catch {
      // Clipboard refused too. Fall back to showing the address in full so
      // it can at least be selected by hand.
      setNote("Copy this address:");
      window.setTimeout(() => setNote(null), 12000);
    }
  }

  if (!url) return null;
  const host = hostOf(url);

  async function onClick(e: React.MouseEvent<HTMLAnchorElement>) {
    // Let the browser do it the normal way when the reader asked for that
    // explicitly — new tab, new window, download, or a middle click.
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) {
      return;
    }
    // preventDefault FIRST, and unconditionally.
    //
    // This used to open the link and then return without preventing the
    // default, so the anchor's own target="_blank" fired as well and one
    // click opened the citation TWICE. And the third argument — any window
    // features string at all — makes browsers open a popup WINDOW rather
    // than a tab, which is intrusive, harder to close, and in an automated
    // context blocks scripting on every other tab.
    //
    // So: stop the anchor, then ask for a plain tab with no features. A
    // modifier click has already returned above, so this only governs an
    // ordinary left click.
    e.preventDefault();
    const opened = window.open(url, "_blank");
    if (opened) {
      // rel="noopener" covers the anchor path; this covers the scripted one.
      opened.opener = null;
      return;
    }

    // Blocked. Say so, and leave the reader something they can act on.
    try {
      await navigator.clipboard.writeText(url);
      setNote("This preview blocks new tabs — link copied, paste it in a browser tab.");
    } catch {
      setNote("This preview blocks new tabs. Open this address in a browser tab:");
    }
    window.setTimeout(() => setNote(null), 9000);
  }

  return (
    <span className={className}>
      <a
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        onClick={onClick}
        className="underline decoration-dotted underline-offset-2 hover:decoration-solid"
        title={url}
      >
        {label ?? "Official source"}
      </a>{" "}
      <span className="text-muted-foreground">({host})</span>{" "}
      {/* The address, always obtainable.
       *
       * The link itself is correct — all ten cited sources return 200 to an
       * ordinary browser. But some of them sit behind firewalls that reject
       * anything they do not recognise as one: FIU-IND answers this app's
       * embedded preview with "The requested URL was rejected", which looks
       * exactly like a broken citation and is not one.
       *
       * A compliance claim is worth what a reader's ability to check it is
       * worth, so there has to be a path that does not depend on the click
       * succeeding. This is it: one press puts the full address on the
       * clipboard, to be opened in whatever browser does work. */}
      <button
        type="button"
        onClick={copy}
        title={`Copy ${url}`}
        className="rounded-[4px] border border-border px-1.5 py-[1px] align-middle text-[10px] uppercase tracking-[0.06em] text-muted-foreground hover:text-foreground"
      >
        {copied ? "copied" : "copy link"}
      </button>
      {note && (
        <span className="mt-1 block text-[11px] leading-snug text-muted-foreground">
          {note} <code className="break-all font-mono text-[10.5px]">{url}</code>
        </span>
      )}
    </span>
  );
}
