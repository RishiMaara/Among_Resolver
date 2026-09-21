/**
 * The receipt for a reconciliation's audit trail, and the check against it.
 *
 * The engine chains every audit entry to the one before it by hash, and
 * returns the hash at the head of the chain with each result. Whoever keeps
 * that value can later prove nothing up to it was altered or removed — a
 * chain alone cannot see its own tail being cut off; a head held elsewhere
 * can. This puts the value where a reviewer can copy it and the check where
 * they can run it.
 */

import { useState } from "react";
import { Fingerprint } from "lucide-react";
import { engineFetch, engineErrorMessage } from "@/lib/api";
import { LoadingMark } from "@/components/loading-mark";
import { cn } from "@/lib/utils";

export function AuditReceipt({ batchId, head }: { batchId: string; head: string }) {
  const [busy, setBusy] = useState(false);
  const [verdict, setVerdict] = useState<{ intact: boolean; plain: string } | null>(null);

  const check = async () => {
    setBusy(true);
    try {
      const r = await engineFetch(
        `audit/${encodeURIComponent(batchId)}/verify?receipt=${encodeURIComponent(head)}`,
      );
      const body = await r.json().catch(() => ({}));
      setVerdict(
        r.ok
          ? { intact: !!body.intact, plain: String(body.plain ?? "") }
          : { intact: false, plain: engineErrorMessage(body, `The check failed (${r.status}).`) },
      );
    } catch {
      setVerdict({ intact: false, plain: "Could not reach the engine to run the check." });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-3 rounded-md border border-border px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <Fingerprint className="size-3.5 text-muted-foreground" />
        <span className="text-muted-foreground">Receipt</span>
        <code className="font-mono" title={head}>
          {head.slice(0, 16)}…
        </code>
        <button
          type="button"
          onClick={() => void navigator.clipboard?.writeText(head)}
          className="rounded border border-border px-2 py-0.5 hover:bg-muted"
        >
          Copy
        </button>
        <button
          type="button"
          onClick={check}
          disabled={busy}
          className="flex items-center gap-1 rounded border border-border px-2 py-0.5 hover:bg-muted disabled:opacity-60"
        >
          {busy && <LoadingMark size={12} />}
          Verify the trail
        </button>
      </div>
      {verdict && (
        <p
          className={cn(
            "mt-2",
            verdict.intact ? "text-emerald-700 dark:text-emerald-400" : "text-red-600",
          )}
        >
          {verdict.plain}
        </p>
      )}
    </div>
  );
}
