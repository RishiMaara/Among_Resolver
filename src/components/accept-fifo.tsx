/**
 * Accepting the oldest-first convention on a settlement nobody can identify.
 *
 * A merchant selling one item at one price produces payments identical in
 * every recorded field. Any N of them make the total, so naming N specific
 * ids is an arbitrary pick wearing the costume of an identification — and the
 * engine correctly refuses to make it. That left a whole class of merchant
 * with a settlement they could never close.
 *
 * This is the way out, and it is deliberately a HUMAN action rather than
 * something the engine does. Accounting has handled fungible units this way
 * for a century: you do not say which unit left, you consume in a stated
 * order and track the balance. The reviewer accepts that convention; the
 * payments are recorded as consumed so nothing else can claim them; and the
 * trail says it rests on a convention, not on evidence.
 *
 * It does NOT clear the batch, and the button never implies it does. The
 * engine's verdict is a separate fact from a person accepting a convention,
 * and the moment those two blur, "zero false clears" stops meaning anything.
 */

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { engineFetch, engineErrorMessage } from "@/lib/api";
import { currentSession } from "@/lib/session";

export interface FifoProposal {
  basis: string;
  count: number;
  txn_ids: string[];
  earliest: string;
  latest: string;
  pool_size: number;
}

export function AcceptFifo({ batchId, proposal }: { batchId: string; proposal: FifoProposal }) {
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [note, setNote] = useState("");

  async function accept() {
    const reviewer = currentSession()?.email ?? "";
    if (!reviewer) {
      toast.error("Sign in first — accepting a convention has to be attributed.");
      return;
    }
    setBusy(true);
    try {
      const res = await engineFetch(`settlement/${encodeURIComponent(batchId)}/accept-fifo`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reviewer, note }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(engineErrorMessage(body, `The engine returned ${res.status}.`));
      setDone(`${body.consumed_count} payments recorded as consumed. The batch is still withheld.`);
      toast.success("Convention accepted and recorded");
    } catch (e) {
      toast.error(e instanceof Error ? e.message.split("\n")[0] : "Could not record it.");
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <div
        className="mt-3 rounded-[7px] px-3 py-2.5 text-[12.5px] leading-[1.5]"
        style={{
          color: "var(--s-done)",
          background: "color-mix(in oklab, var(--s-done) 10%, transparent)",
        }}
      >
        {done} The audit trail records that this rests on a stated convention, not on evidence that
        these specific payments are the members.
      </div>
    );
  }

  return (
    <div className="mt-3 rounded-[7px] border border-border p-3">
      <p className="m-0 text-[12.5px] font-medium">Close this on the oldest-first convention</p>
      <p className="m-0 mt-1 text-[12px] leading-[1.55] text-muted-foreground">
        Take the {proposal.count} earliest of the {proposal.pool_size.toLocaleString("en-IN")}{" "}
        indistinguishable payments — {proposal.earliest.slice(0, 16)} to{" "}
        {proposal.latest.slice(0, 16)} — and record them as consumed so no other settlement can
        claim them. <b>This does not clear the batch.</b> Any set of the same size would have been
        equally correct, and the record will say so.
      </p>
      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Why this is the right call (optional)"
          className="min-w-[200px] flex-1 rounded-[6px] border border-border bg-background px-2.5 py-1.5 text-[12px]"
        />
        <Button size="sm" variant="outline" onClick={accept} disabled={busy}>
          {busy ? "Recording…" : "Accept convention"}
        </Button>
      </div>
    </div>
  );
}
