/**
 * Recording that a reviewer acted on a compliance finding.
 *
 * Each card told the reviewer what to do next and gave them no way to say
 * they had done it, so the panel's advice went nowhere and the trail never
 * showed that a human had looked.
 *
 * THE VERBS ARE NOT APPROVE / REJECT. Someone reading a flagged pattern
 * either judges it benign — the reseller really is restocking — or escalates
 * it to a person who can act. Nobody "approves" a suspected structuring
 * pattern, and offering that word would frame the reviewer as endorsing the
 * behaviour rather than assessing it.
 *
 * THE STATUTORY CARVE-OUT. Clearing a review is not discharging a legal duty,
 * and on a statutory finding those are dangerously easy to confuse. A CTR
 * obligation is a duty to REPORT: deciding a transaction is legitimate does
 * not remove it. So on a statutory card the button says so before it is
 * pressed, and the audit entry repeats it afterwards. A reviewer who clicks
 * something and believes they have filed a report is worse off than one with
 * no button at all.
 */

import { useEffect, useState } from "react";
import { toast } from "sonner";
import { engineFetch, engineErrorMessage } from "@/lib/api";
import { currentSession } from "@/lib/session";
import { fetchDecisions, latestFor, type RecordedDecision } from "@/lib/decisions";

export function ComplianceDecision({
  batchId,
  ruleId,
  basis,
  txnIds,
}: {
  batchId: string;
  ruleId: string;
  basis: string;
  txnIds: string[];
}) {
  const session = currentSession();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [decided, setDecided] = useState<RecordedDecision | null>(null);
  const [loading, setLoading] = useState(true);
  const statutory = basis === "statutory";

  // Already answered? Then do not ask again — including after a reload.
  useEffect(() => {
    let live = true;
    (async () => {
      const entries = await fetchDecisions(batchId);
      if (!live) return;
      setDecided(latestFor(entries, { kind: "compliance", ruleId }));
      setLoading(false);
    })();
    return () => {
      live = false;
    };
  }, [batchId, ruleId]);

  async function decide(decision: "cleared" | "escalated") {
    if (!session) return;
    setBusy(decision);
    try {
      const res = await engineFetch(
        `settlement/${encodeURIComponent(batchId)}/compliance/decision`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            rule_id: ruleId,
            decision,
            reviewer: session.email,
            basis,
            note: note.trim(),
            txn_ids: txnIds,
          }),
        },
      );
      if (!res.ok) {
        throw new Error(engineErrorMessage(await res.json(), "Could not record the decision."));
      }
      const data = await res.json();
      setDecided({
        verdict: decision.toUpperCase(),
        reviewer: data.reviewer,
        note: data.note ?? "",
        detail: "",
        ts: "",
      });
      // Tell the audit trail panel to refetch — it holds a snapshot
      // taken before this decision existed.
      window.dispatchEvent(new CustomEvent("among:decision-recorded", { detail: { batchId } }));
      toast.success(
        decision === "cleared"
          ? `${ruleId} — reviewed and cleared, recorded against your name.`
          : `${ruleId} — escalated, recorded against your name.`,
      );
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : "Could not record the decision.");
    } finally {
      setBusy(null);
    }
  }

  if (loading) return null;

  if (decided) {
    const cleared = decided.verdict === "CLEARED";
    return (
      <div className="border-t border-border px-4 py-2.5">
        <p className="m-0 text-[12px]">
          <span style={{ color: cleared ? "var(--s-done)" : "var(--s-withheld)" }}>
            {cleared ? "Reviewed — cleared" : "Escalated"}
          </span>{" "}
          <span className="text-muted-foreground">
            by {decided.reviewer}
            {decided.note ? ` — "${decided.note}"` : ""}
          </span>
        </p>
        {cleared && statutory && (
          <p className="m-0 mt-1 text-[11px]" style={{ color: "var(--s-withheld)" }}>
            This rests on a statutory obligation. The review is closed; the reporting duty is not
            discharged by it.
          </p>
        )}
      </div>
    );
  }

  if (!session) {
    return (
      <div className="border-t border-border px-4 py-2.5">
        <p className="m-0 text-[11.5px] text-muted-foreground">
          Sign in to record that you reviewed this.
        </p>
      </div>
    );
  }

  return (
    <div className="border-t border-border px-4 py-2.5">
      <input
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="What did you find? (optional)"
        className="w-full rounded-lg border border-border bg-transparent px-3 py-1.5 text-[12px] outline-none focus:border-accent"
      />
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={busy !== null}
          onClick={() => decide("cleared")}
          className="rounded-lg px-3 py-[6px] text-[12px] font-medium disabled:opacity-40"
          style={{
            color: "var(--s-done)",
            background: "color-mix(in oklab, var(--s-done) 14%, transparent)",
          }}
        >
          {busy === "cleared" ? "Recording…" : "Reviewed — no concern"}
        </button>
        <button
          type="button"
          disabled={busy !== null}
          onClick={() => decide("escalated")}
          className="rounded-lg px-3 py-[6px] text-[12px] font-medium disabled:opacity-40"
          style={{
            color: "var(--s-withheld)",
            background: "color-mix(in oklab, var(--s-withheld) 14%, transparent)",
          }}
        >
          {busy === "escalated" ? "Recording…" : "Escalate"}
        </button>
      </div>
      {statutory && (
        <p className="m-0 mt-2 text-[11px]" style={{ color: "var(--s-withheld)" }}>
          Statutory: clearing this closes your review. It does not discharge the reporting
          obligation.
        </p>
      )}
    </div>
  );
}
