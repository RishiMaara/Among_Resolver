/**
 * Where "ready for human approval" actually gets approved.
 *
 * The cash panel printed "Proposed — awaiting approval" and the engine's own
 * note said the proposal was "balanced and ready for human approval". There
 * was no approval anywhere in the product. The sentence was decoration, and
 * a reviewer reading it had no way to do the thing it described.
 *
 * APPROVING POSTS NOTHING. This engine never writes to a ledger, and this
 * button does not change that — it records that a named person read a
 * balanced proposal and approved it for posting. The posting happens in the
 * accounting system, by whoever holds that authority. Recording an approval
 * and letting it read as a posting would be the same class of lie as
 * clearing a settlement nobody checked, so the wording here is deliberately
 * flat about what did and did not happen.
 */

import { useEffect, useState } from "react";
import { toast } from "sonner";
import { engineFetch, engineErrorMessage } from "@/lib/api";
import { currentSession } from "@/lib/session";
import { fetchDecisions, latestFor } from "@/lib/decisions";

export function JournalApproval({
  batchId,
  entryId,
  balanced,
  onDecided,
}: {
  batchId: string;
  entryId: string;
  balanced: boolean;
  /** Lifted so the status badge above stops saying "awaiting approval". */
  onDecided?: (decision: string) => void;
}) {
  const session = currentSession();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [done, setDone] = useState<{ decision: string; reviewer: string; note: string } | null>(
    null,
  );
  const [loading, setLoading] = useState(true);

  // Already approved or rejected? Then stop offering the buttons, including
  // after a reload.
  useEffect(() => {
    let live = true;
    (async () => {
      const entries = await fetchDecisions(batchId);
      if (!live) return;
      const prior = latestFor(entries, { kind: "journal" });
      if (prior) {
        setDone({
          decision: prior.verdict.toLowerCase(),
          reviewer: prior.reviewer,
          note: prior.note,
        });
        onDecided?.(prior.verdict.toLowerCase());
      }
      setLoading(false);
    })();
    return () => {
      live = false;
    };
    // onDecided is read inside the effect, so it belongs here. Leaving it out
    // is the same stale-closure shape that silently reverted a typed date in
    // index.tsx.
  }, [batchId, onDecided]);

  async function decide(decision: "approved" | "rejected") {
    if (!session) return;
    setBusy(decision);
    try {
      const res = await engineFetch(`settlement/${encodeURIComponent(batchId)}/journal/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          decision,
          reviewer: session.email,
          entry_id: entryId,
          note: note.trim(),
        }),
      });
      if (!res.ok) {
        throw new Error(engineErrorMessage(await res.json(), "Could not record the approval."));
      }
      const data = await res.json();
      setDone({ decision: data.decision, reviewer: data.reviewer, note: data.note });
      onDecided?.(data.decision);
      // Tell the audit trail panel to refetch — it holds a snapshot
      // taken before this decision existed.
      window.dispatchEvent(new CustomEvent("among:decision-recorded", { detail: { batchId } }));
      toast.success(
        decision === "approved"
          ? "Approved for posting — recorded. Nothing has been posted."
          : "Rejected — recorded against your name.",
      );
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : "Could not record the approval.");
    } finally {
      setBusy(null);
    }
  }

  if (loading) return null;

  if (done) {
    return (
      <div className="mt-4 border-t border-border pt-3">
        <p className="m-0 text-[12.5px]">
          <span
            style={{
              color: done.decision === "approved" ? "var(--s-done)" : "var(--destructive)",
            }}
          >
            {done.decision === "approved" ? "Approved for posting" : "Rejected"}
          </span>{" "}
          <span className="text-muted-foreground">
            by {done.reviewer}
            {done.note ? ` — "${done.note}"` : ""}
          </span>
        </p>
        <p className="m-0 mt-1 text-[11px] text-muted-foreground">
          Recorded in the audit trail. <strong>Nothing has been posted</strong> — this engine does
          not write to a ledger. Post it in your accounting system.
        </p>
      </div>
    );
  }

  return (
    <div className="mt-4 border-t border-border pt-3">
      {!session ? (
        <p className="m-0 text-[12px]" style={{ color: "var(--s-withheld)" }}>
          Sign in to approve this proposal. An approval nobody is named against is not an approval.
        </p>
      ) : (
        <>
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Note (optional)"
            className="w-full rounded-lg border border-border bg-transparent px-3 py-2 text-[12.5px] outline-none focus:border-accent"
          />
          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={busy !== null || !balanced}
              title={balanced ? undefined : "An unbalanced proposal cannot be approved."}
              onClick={() => decide("approved")}
              className="rounded-lg px-3.5 py-[7px] text-[12.5px] font-medium disabled:opacity-40"
              style={{
                color: "var(--s-done)",
                background: "color-mix(in oklab, var(--s-done) 14%, transparent)",
              }}
            >
              {busy === "approved" ? "Recording…" : "Approve for posting"}
            </button>
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => decide("rejected")}
              className="rounded-lg px-3.5 py-[7px] text-[12.5px] font-medium disabled:opacity-40"
              style={{
                color: "var(--destructive)",
                background: "color-mix(in oklab, var(--destructive) 14%, transparent)",
              }}
            >
              {busy === "rejected" ? "Recording…" : "Reject"}
            </button>
            <span className="text-[11px] text-muted-foreground">as {session.email}</span>
          </div>
          <p className="m-0 mt-2 text-[11px] text-muted-foreground">
            Approving records your sign-off. It does <strong>not</strong> post anything — this
            engine never writes to a ledger.
          </p>
        </>
      )}
    </div>
  );
}
