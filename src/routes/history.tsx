/**
 * Previously recorded runs.
 *
 * The engine has written a record on every reconciliation since history.py
 * was built, and nothing could read them back — the files accumulated in
 * data/history with no endpoint and no screen. A record nobody can retrieve
 * is not a record, and for a finance control the retrieval IS the feature:
 * the question an auditor asks is "what did this say last time", which the
 * system could answer and could not be asked.
 */

import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { Flag, ScrollText } from "lucide-react";
import { ThemeToggle } from "@/components/theme-toggle";
import { SessionMenu } from "@/components/session-menu";
import { Wordmark } from "@/components/wordmark";
import { LoadingMark } from "@/components/loading-mark";
import { engineFetch, ENGINE_HOST } from "@/lib/api";

export const Route = createFileRoute("/history")({ component: HistoryScreen });

interface Run {
  record: string;
  timestamp_utc: string | null;
  batch_id: string | null;
  reviewer: string | null;
  status: "cleared" | "withheld" | "unmatched" | null;
  confidence: number | null;
  matched_count: number | null;
  total_candidates: number | null;
  tie_out_residual_cents: number | null;
  audit_trail_entry_count: number | null;
}

const TONE: Record<string, string> = {
  cleared: "var(--s-done)",
  withheld: "var(--s-withheld)",
  unmatched: "var(--s-blocked)",
};

function HistoryScreen() {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const r = await engineFetch("history?limit=200");
        if (!r.ok) throw new Error(`engine returned ${r.status}`);
        const data = await r.json();
        if (live) setRuns(data.runs ?? []);
      } catch (e) {
        if (live) {
          setError(e instanceof Error ? e.message : "could not reach the engine");
        }
      }
    })();
    return () => {
      live = false;
    };
  }, []);

  const q = filter.trim().toLowerCase();
  const shown = (runs ?? []).filter(
    (r) =>
      !q ||
      (r.batch_id ?? "").toLowerCase().includes(q) ||
      (r.reviewer ?? "").toLowerCase().includes(q),
  );

  // How many times each batch appears. A batch reconciled more than once is
  // exactly what an auditor looks for, so it is counted here rather than left
  // for the reader to spot by scanning.
  const seen = new Map<string, number>();
  for (const r of runs ?? []) {
    const k = r.batch_id ?? "";
    seen.set(k, (seen.get(k) ?? 0) + 1);
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-40 border-b border-border bg-background/90 backdrop-blur-md">
        <div className="mx-auto flex max-w-[1120px] items-center justify-between gap-4 px-4 py-2.5 sm:px-6">
          <Wordmark pill="History" />
          <div className="flex items-center gap-3.5">
            <Link
              to="/"
              className="whitespace-nowrap text-[14px] text-muted-foreground hover:text-foreground"
            >
              ← Agent Flow
            </Link>
            <Link
              to="/escalations"
              className="nav-flag flex items-center gap-1.5 whitespace-nowrap text-[14px] text-muted-foreground hover:text-foreground"
            >
              <Flag className="size-4" />
              <span className="hidden sm:inline">Escalations</span>
            </Link>
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

      <div className="mx-auto max-w-[1120px] px-4 pb-24 sm:px-6">
        <section className="py-12">
          <p className="eyebrow-display m-0 uppercase text-muted-foreground">Recorded runs</p>
          <h1 className="m-0 mt-4 max-w-[680px] font-[family-name:var(--font-display)] text-[38px] font-normal leading-[1.05] tracking-[-0.025em]">
            What this engine said, and when it said it
          </h1>
          <p className="narrative-copy m-0 mt-4 max-w-[620px] text-[15.5px] leading-[1.65] text-muted-foreground">
            Every reconciliation writes a record. A batch appearing twice is not an error —
            reconciling again is normal — but both verdicts stay on the record, and the second never
            quietly replaces the first. Served from{" "}
            <span className="font-mono text-[13.5px] text-foreground">
              GET {ENGINE_HOST}/history
            </span>
            .
          </p>
        </section>

        {error && (
          <div className="rounded-[14px] border border-dashed border-border p-10 text-center">
            <p className="m-0 text-[13.5px] text-muted-foreground">
              Could not load history — {error}. Is the engine running?
            </p>
          </div>
        )}

        {!runs && !error && (
          <div className="flex flex-col items-center gap-4 rounded-[14px] border border-dashed border-border p-14 text-center">
            <LoadingMark size={40} />
            <p className="m-0 text-[13.5px] text-muted-foreground">
              Reading recorded runs from the engine…
            </p>
          </div>
        )}

        {runs && runs.length === 0 && (
          <div className="rounded-[14px] border border-dashed border-border p-12 text-center">
            <p className="m-0 text-[13.5px] text-muted-foreground">
              No runs recorded yet. Reconcile a settlement and it will appear here.
            </p>
          </div>
        )}

        {runs && runs.length > 0 && (
          <>
            <div className="mb-3 flex flex-wrap items-center gap-2.5">
              <input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Filter by batch or reviewer…"
                className="h-9 min-w-[220px] flex-1 rounded-[10px] border border-border bg-background px-3 text-[13px]"
              />
              <span className="label-ui text-[10px] uppercase text-muted-foreground">
                {shown.length} of {runs.length}
              </span>
            </div>

            <div className="overflow-x-auto rounded-[14px] border border-border">
              <table className="w-full min-w-[720px] border-collapse text-left">
                <thead>
                  <tr className="border-b border-border">
                    {["Batch", "Verdict", "Matched", "Confidence", "Audit", "Reviewer", "When"].map(
                      (h) => (
                        <th
                          key={h}
                          className="label-ui px-3 py-2.5 text-[10px] uppercase text-muted-foreground"
                        >
                          {h}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody>
                  {shown.map((r) => (
                    <tr key={r.record} className="border-b border-border last:border-0">
                      <td className="px-3 py-2.5 font-mono text-[12px]">
                        {r.batch_id ?? "—"}
                        {(seen.get(r.batch_id ?? "") ?? 0) > 1 && (
                          <span
                            className="ml-2 rounded-full px-1.5 py-0.5 text-[9.5px] uppercase"
                            style={{
                              color: "var(--s-withheld)",
                              background: "color-mix(in oklab, var(--s-withheld) 12%, transparent)",
                            }}
                            title="This batch was reconciled more than once"
                          >
                            ×{seen.get(r.batch_id ?? "")}
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2.5">
                        <span
                          className="label-ui text-[10px] uppercase"
                          style={{
                            color: TONE[r.status ?? ""] ?? "var(--muted-foreground)",
                          }}
                        >
                          {r.status ?? "—"}
                        </span>
                      </td>
                      <td className="px-3 py-2.5 font-mono text-[12px] tabular-nums text-muted-foreground">
                        {r.matched_count ?? "—"}/{r.total_candidates ?? "—"}
                      </td>
                      <td className="px-3 py-2.5 font-mono text-[12px] tabular-nums text-muted-foreground">
                        {r.confidence == null ? "—" : r.confidence.toFixed(2)}
                      </td>
                      <td
                        className="px-3 py-2.5 font-mono text-[12px] tabular-nums text-muted-foreground"
                        title="Audit entries recorded for this run. The trail itself is at GET /audit/{batch_id}."
                      >
                        {r.audit_trail_entry_count ?? "—"}
                      </td>
                      <td className="px-3 py-2.5 text-[12px] text-muted-foreground">
                        {r.reviewer ?? (
                          <span title="Recorded before runs carried attribution">not recorded</span>
                        )}
                      </td>
                      <td className="px-3 py-2.5 font-mono text-[11.5px] text-muted-foreground">
                        {r.timestamp_utc ? r.timestamp_utc.slice(0, 19).replace("T", " ") : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
