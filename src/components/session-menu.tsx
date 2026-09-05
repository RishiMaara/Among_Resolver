/**
 * Who is signed in, and the way back out.
 *
 * The gate is only half of a sign-in: without this, a session that lasts the
 * whole browser tab has no exit, and a demo cannot be shown twice without
 * clearing storage by hand.
 */

import { useEffect, useState } from "react";
import { currentSession, signOut } from "@/lib/session";

export function SessionMenu() {
  const [email, setEmail] = useState<string | null>(null);

  // sessionStorage does not exist during the server render, so this reads
  // after mount and the server emits nothing.
  useEffect(() => setEmail(currentSession()?.email ?? null), []);
  if (!email) return null;

  const who = email.split("@")[0];

  return (
    <button
      type="button"
      onClick={() => {
        signOut();
        window.location.reload();
      }}
      title={`Signed in as ${email} — click to sign out`}
      className="hidden items-center gap-1.5 whitespace-nowrap rounded-full border border-border px-2.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:border-accent hover:text-foreground sm:flex"
    >
      <span
        className="size-1.5 rounded-full"
        style={{ background: "var(--s-done)" }}
        aria-hidden="true"
      />
      {who}
    </button>
  );
}
