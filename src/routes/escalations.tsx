/**
 * Where escalations go.
 *
 * "Escalate" names a destination, and there was not one. The button wrote a
 * line into the batch's own audit trail and stopped — so a reviewer who
 * escalated a finding could not see it again, and nobody downstream had any
 * way to find it. The word promised a handoff the system never performed.
 *
 * This is the list. It reads the audit trail rather than a second store, so
 * an escalation cannot exist in one place and not the other, and it is keyed
 * by batch so each row leads back to the reconciliation it came from.
 *
 * Nothing here auto-resolves. Clearing an escalation is a decision and goes
 * through the same control that raised it; an item that vanished on its own
 * would be worse than one that never moved.
 */

import { useEffect, useState } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import { Flag, ScrollText } from "lucide-react";
import { HistoryLink } from "@/components/history-link";
import { engineFetch } from "@/lib/api";
import { Wordmark } from "@/components/wordmark";
import { SessionMenu } from "@/components/session-menu";
import { ThemeToggle } from "@/components/theme-toggle";

export const Route = createFileRoute("/escalations")({ component: Escalations });

interface Escalation {
  batch_id: string;
  escalated_by: string;
  rule_id: string;
  payment_count: number;
  note: string;
  raised_at: string;
}

function Escalations() {
  const [rows, setRows] = useState<Escalation[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const r = await engineFetch("escalations?limit=200");
        if (!r.ok) throw new Error(`The engine returned ${r.status}.`);
        const d = await r.json();
        if (live) setRows(d.escalations ?? []);
      } catch (e) {
        if (live) setError(e instanceof Error ? e.message : "Could not load escalations.");
      }
    })();
    return () => {
      live = false;
    };
  }, []);

  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* This screen had no header at all — its only link home sat inside a
          table row. A page a reviewer is sent TO, with no way back, is a dead
          end, and the other screens all carry this bar. */}
      <header className="sticky top-0 z-40 border-b border-border bg-background/90 backdrop-blur-md">
        <div className="mx-auto flex max-w-[1120px] items-center justify-between gap-4 px-4 py-2.5 sm:px-6">
          <Wordmark pill="Escalations" />
          <div className="flex items-center gap-3.5">
            <Link
              to="/"
              className="whitespace-nowrap text-[14px] text-muted-foreground hover:text-foreground"
            >
              ← Agent Flow
            </Link>
            <HistoryLink className="flex items-center gap-1.5 whitespace-nowrap text-[14px] text-muted-foreground hover:text-foreground" />
            <Link
              to="/rulebook"
              className="nav-scroll flex items-center gap-1.5 whitespace-nowrap text-[14px] text-muted-foreground hover:text-foreground"
            >
              <ScrollText className="size-4" />
              <span className="hidden sm:inline">Rulebook</span>
            </Link>
            <SessionMenu />
            <ThemeToggle />
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-[900px] px-6 py-10">
        <p className="label-ui m-0 flex items-center gap-1.5 text-[10px] uppercase text-muted-foreground">
          <Flag className="size-3.5" style={{ color: "var(--s-withheld)" }} />
          Compliance · follow-up queue
        </p>
        <h1 className="mt-2 text-[30px] font-semibold leading-tight">What a reviewer sent on</h1>
        <p className="mt-2 max-w-[640px] text-[13.5px] leading-[1.6] text-muted-foreground">
          Every compliance finding escalated for follow-up, newest first. These are judgements a
          reviewer declined to close on their own — they stay here until somebody acts on them,
          because an item that cleared itself would be worse than one that never moved.
        </p>

        {error && <p className="mt-6 text-[13px] text-destructive">{error}</p>}

        {!rows && !error && <p className="mt-6 text-[13px] text-muted-foreground">Loading…</p>}

        {rows && rows.length === 0 && (
          <div className="surface-card mt-6 p-6">
            <p className="m-0 text-[13px]" style={{ color: "var(--s-done)" }}>
              Nothing has been escalated.
            </p>
            <p className="m-0 mt-1 text-[12px] text-muted-foreground">
              Findings a reviewer escalates from a reconciliation appear here.
            </p>
          </div>
        )}

        {rows && rows.length > 0 && (
          <div className="mt-6 overflow-hidden rounded-[12px] border border-border">
            <div className="border-b border-border bg-muted/40 px-4 py-2.5">
              <span className="label-ui text-[10px] uppercase text-muted-foreground">
                {rows.length} awaiting follow-up
              </span>
            </div>
            {rows.map((e, i) => (
              <article
                key={`${e.batch_id}-${e.rule_id}-${i}`}
                className="border-b border-border px-4 py-3 last:border-0"
                style={{ borderLeft: "3px solid var(--s-withheld)" }}
              >
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <span className="font-mono text-[12.5px] font-medium">{e.rule_id}</span>
                  <Link
                    to="/"
                    className="font-mono text-[11.5px] text-accent underline decoration-dotted underline-offset-2"
                  >
                    {e.batch_id}
                  </Link>
                  <span className="text-[11.5px] text-muted-foreground">
                    {e.payment_count > 0 &&
                      `${e.payment_count} payment${e.payment_count === 1 ? "" : "s"} · `}
                    raised by {e.escalated_by || "unknown"}
                  </span>
                </div>
                {e.note && <p className="m-0 mt-1 text-[12.5px] leading-[1.5]">“{e.note}”</p>}
              </article>
            ))}
          </div>
        )}

        <p className="mt-5 text-[11.5px] leading-[1.5] text-muted-foreground">
          Read from the audit trail, not a separate list — an escalation cannot be recorded in one
          place and missing from the other. This engine does not file reports; acting on these is a
          person's job.
        </p>
      </div>
    </div>
  );
}
