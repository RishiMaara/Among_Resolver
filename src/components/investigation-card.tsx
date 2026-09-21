/**
 * What the investigator proposes for a settlement the engine withheld, and
 * the check that decided whether a reviewer should act on it.
 *
 * A rejected proposal is shown as rejected, with the reasons, rather than
 * hidden. "The obvious set counts four payments twice" tells a reviewer more
 * than silence would, and a verifier nobody can see reject anything is a
 * verifier nobody has reason to trust.
 */

import { ShieldCheck, ShieldX, Search } from "lucide-react";
import type { Investigation } from "@/lib/engine-types";
import { cn, inr } from "@/lib/utils";

const ACTION: Record<string, string> = {
  MATCH_PROPOSAL: "Propose a set of payments",
  WAIT_FOR_DATA: "Wait for data, then re-run",
  REQUEST_SOURCE: "Ask for a missing source",
  WRITE_OFF_ROUNDING: "Write off a rounding difference",
  ESCALATE: "Escalate to a person",
};

function detail(p: Investigation["proposal"]): string | null {
  switch (p.action) {
    case "MATCH_PROPOSAL": {
      const shown = p.txn_ids.slice(0, 8).join(", ");
      const more = p.txn_ids.length > 8 ? ` and ${p.txn_ids.length - 8} more` : "";
      return `${p.txn_ids.length} payment(s): ${shown}${more}.`;
    }
    case "WAIT_FOR_DATA":
      return p.until_date ? `Re-run on or after ${p.until_date}.` : null;
    case "REQUEST_SOURCE":
      return p.party ? `Ask the ${p.party} for ${p.request || "the missing record"}.` : null;
    case "WRITE_OFF_ROUNDING":
      return typeof p.amount_cents === "number" ? `Write off ${inr(p.amount_cents)}.` : null;
    default:
      return null;
  }
}

export function InvestigationCard({ found }: { found: Investigation }) {
  const { proposal: p, verification: v, case: c } = found;
  const line = detail(p);
  const feeds = new Set(c.engine_proposal.map((r) => r.feed).filter(Boolean));

  return (
    <div className="surface-card p-6">
      <h3 className="flex items-center gap-2 text-lg font-semibold">
        <Search className="size-5 text-muted-foreground" />
        What to do about it
      </h3>
      <p className="mt-1 text-sm text-muted-foreground">
        The investigator reads the withheld settlement and proposes one next step. Code checks the
        proposal against the data before anyone is asked to act on it.
      </p>

      <div
        className={cn(
          "mt-4 rounded-md border p-4",
          v.valid ? "border-emerald-500/40 bg-emerald-500/5" : "border-red-500/40 bg-red-500/5",
        )}
      >
        <div className="flex flex-wrap items-center gap-2">
          {v.valid ? (
            <ShieldCheck className="size-4 text-emerald-600" />
          ) : (
            <ShieldX className="size-4 text-red-600" />
          )}
          <span className={cn("font-semibold text-sm", !v.valid && "line-through opacity-80")}>
            {ACTION[p.action] ?? p.action}
          </span>
          <span className="rounded-full border border-border px-2 py-0.5 text-[11px] text-muted-foreground">
            proposed by {p.proposer === "model" ? "the model" : "fixed rules"}
          </span>
        </div>
        {line && <p className="mt-2 text-sm">{line}</p>}
        {p.reason && <p className="mt-1 text-sm text-muted-foreground">{p.reason}</p>}
        <p
          className={cn(
            "mt-3 text-[13px] font-medium",
            v.valid ? "text-emerald-700 dark:text-emerald-400" : "text-red-600 dark:text-red-400",
          )}
        >
          {v.plain}
        </p>
      </div>

      <dl className="mt-4 grid gap-x-6 gap-y-2 text-[12.5px] sm:grid-cols-2">
        <div className="flex justify-between gap-3">
          <dt className="text-muted-foreground">Target, before fees</dt>
          <dd className="tabular-nums">{inr(c.target_cents)}</dd>
        </div>
        <div className="flex justify-between gap-3">
          <dt className="text-muted-foreground">Engine's own set</dt>
          <dd className="tabular-nums">
            {c.engine_proposal.length} record(s), {inr(c.engine_proposal_sum_cents)}
            {feeds.size > 1 && ` · ${[...feeds].join(" + ")}`}
          </dd>
        </div>
        <div className="flex justify-between gap-3">
          <dt className="text-muted-foreground">Other sets that also add up</dt>
          <dd className="tabular-nums">{c.alternatives.length}</dd>
        </div>
        {c.member_feed && (
          <div className="flex justify-between gap-3">
            <dt className="text-muted-foreground">Member feed</dt>
            <dd>
              {c.member_feed}
              {c.member_feed_declared === false && " (assumed — not declared)"}
            </dd>
          </div>
        )}
      </dl>
    </div>
  );
}
