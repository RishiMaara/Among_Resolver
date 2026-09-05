/**
 * Where a reviewer's decision goes.
 *
 * Every other control in this app reads or re-runs. The product's whole claim
 * is that the engine declines when it cannot be certain and a person decides
 * — and there was nowhere for that decision to go. It happened in someone's
 * head and evaporated. The audit trail recorded what the engine did and never
 * what the human did, which made it a record of half the process.
 *
 * It also left the sign-in screen making a promise the app did not keep:
 * "reconciliation decisions are attributed to whoever is signed in". They
 * were attributed to nobody, because they were never written down.
 *
 * TWO THINGS THIS DELIBERATELY DOES NOT DO
 *
 * It does not change the verdict. A withheld batch stays withheld after a
 * reviewer confirms it; their decision is recorded ALONGSIDE the engine's,
 * never over it. An audit trail where a person can retroactively make the
 * machine look right is worth nothing, and the moment confirming flips the
 * status is the moment the 0-false-clears number stops meaning anything.
 *
 * It does not let you decide anonymously. Signed out, the buttons are gone
 * and the reason is stated — an unattributed decision is precisely the thing
 * this exists to stop.
 */

import { useEffect, useState } from "react";
import { toast } from "sonner";
import { engineFetch, engineErrorMessage } from "@/lib/api";
import { currentSession } from "@/lib/session";
import { fetchDecisions, latestFor, type RecordedDecision } from "@/lib/decisions";

export function ReviewDecision({
  batchId,
  txnIds,
  cleared,
}: {
  batchId: string;
  txnIds: string[];
  /** Only to word the prompt; it does not gate the control. */
  cleared: boolean;
}) {
  const session = currentSession();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [decided, setDecided] = useState<RecordedDecision | null>(null);
  const [loading, setLoading] = useState(true);

  // A question already answered must not be asked again — including after a
  // reload, which previously reset the control and presented a settled batch
  // as though nobody had looked at it.
  useEffect(() => {
    let live = true;
    (async () => {
      const entries = await fetchDecisions(batchId);
      if (!live) return;
      setDecided(latestFor(entries, { kind: "batch" }));
      setLoading(false);
    })();
    return () => {
      live = false;
    };
  }, [batchId]);

  async function decide(decision: "confirmed" | "rejected") {
    if (!session) return;
    setBusy(decision);
    try {
      const res = await engineFetch(`settlement/${encodeURIComponent(batchId)}/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          decision,
          reviewer: session.email,
          note: note.trim(),
          txn_ids: txnIds,
        }),
      });
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
      setNote("");
      // Tell the audit trail panel to refetch — it holds a snapshot
      // taken before this decision existed.
      window.dispatchEvent(new CustomEvent("among:decision-recorded", { detail: { batchId } }));
      toast.success(
        decision === "confirmed"
          ? "Confirmed — recorded against your name in the audit trail."
          : "Rejected — recorded against your name in the audit trail.",
      );
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : "Could not record the decision.");
    } finally {
      setBusy(null);
    }
  }

  if (loading) return null;

  if (decided) {
    const confirmed = decided.verdict === "CONFIRMED";
    return (
      <div className="surface-card p-5">
        <p className="label-ui m-0 text-[10px] uppercase text-muted-foreground">Your decision</p>
        <p className="m-0 mt-2 text-[13px]">
          <span style={{ color: confirmed ? "var(--s-done)" : "var(--destructive)" }}>
            {confirmed ? "Confirmed" : "Rejected"}
          </span>{" "}
          <span className="text-muted-foreground">
            by {decided.reviewer}
            {decided.note ? ` — "${decided.note}"` : ""}
          </span>
        </p>
        <p className="m-0 mt-1 text-[11px] text-muted-foreground">
          Recorded in the audit trail. The engine's own verdict is unchanged — this sits beside it,
          not over it.
        </p>
      </div>
    );
  }

  return (
    <div className="surface-card p-5">
      <p className="label-ui m-0 text-[10px] uppercase text-muted-foreground">Your decision</p>

      <p className="m-0 mt-2 text-[12.5px] leading-[1.55] text-muted-foreground">
        {cleared
          ? "The engine cleared this batch. Recording your review keeps a named human decision next to the machine's, which is what an auditor asks for."
          : "The engine would not clear this on its own. Confirming does not change that — it records that you looked, and what you concluded."}
      </p>

      {!session ? (
        <p className="m-0 mt-3 text-[12.5px]" style={{ color: "var(--s-withheld)" }}>
          Sign in to record a decision. A decision nobody is named against is the thing this exists
          to prevent.
        </p>
      ) : (
        <>
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Why? (optional, but it is what the next reader will want)"
            className="mt-3 w-full rounded-lg border border-border bg-transparent px-3 py-2 text-[12.5px] outline-none focus:border-accent"
          />
          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => decide("confirmed")}
              className="rounded-lg px-3.5 py-[7px] text-[12.5px] font-medium disabled:opacity-50"
              style={{
                color: "var(--s-done)",
                background: "color-mix(in oklab, var(--s-done) 14%, transparent)",
              }}
            >
              {busy === "confirmed" ? "Recording…" : "Confirm these payments"}
            </button>
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => decide("rejected")}
              className="rounded-lg px-3.5 py-[7px] text-[12.5px] font-medium disabled:opacity-50"
              style={{
                color: "var(--destructive)",
                background: "color-mix(in oklab, var(--destructive) 14%, transparent)",
              }}
            >
              {busy === "rejected" ? "Recording…" : "Reject"}
            </button>
            <span className="text-[11px] text-muted-foreground">
              as {session.email}
              {txnIds.length > 0 &&
                ` · covering ${txnIds.length} payment${txnIds.length === 1 ? "" : "s"}`}
            </span>
          </div>
        </>
      )}
    </div>
  );
}
