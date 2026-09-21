/**
 * The approved posting as a Tally import file.
 *
 * Offered only once the posting is approved, and the engine checks that
 * again: it refuses an unbalanced entry, one nobody approved, or one whose
 * latest decision was a rejection. The export is recorded in the audit trail
 * against the name of whoever asked for it. Nothing is posted — the entry
 * exists in Tally only once someone imports the file.
 */

import { useState } from "react";
import { FileDown } from "lucide-react";
import { toast } from "sonner";
import { engineFetch, engineErrorMessage } from "@/lib/api";
import { currentSession } from "@/lib/session";
import { LoadingMark } from "@/components/loading-mark";

export function TallyExport({ batchId }: { batchId: string }) {
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);

  const download = async () => {
    const who = currentSession()?.email;
    if (!who) {
      setRefused("An export is recorded against a name. Sign in first.");
      return;
    }
    setBusy(true);
    setRefused(null);
    try {
      const r = await engineFetch(`settlement/${encodeURIComponent(batchId)}/export/tally`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reviewer: who }),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        setRefused(engineErrorMessage(body, `The engine refused the export (${r.status}).`));
        return;
      }
      const url = URL.createObjectURL(await r.blob());
      const a = document.createElement("a");
      a.href = url;
      a.download = `tally_${batchId.replace(/[^A-Za-z0-9._-]/g, "_")}.xml`;
      a.click();
      URL.revokeObjectURL(url);
      toast.success("Tally import file downloaded. Nothing has been posted.");
    } catch {
      setRefused("Could not reach the engine for the export.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-3">
      <button
        type="button"
        onClick={download}
        disabled={busy}
        className="flex items-center gap-1.5 rounded-full border border-border px-3 py-1 text-sm transition-colors hover:border-accent disabled:opacity-60"
      >
        {busy ? <LoadingMark size={14} /> : <FileDown className="size-3.5" />}
        Export for Tally (XML)
      </button>
      {refused && <p className="mt-2 text-xs text-red-600">{refused}</p>}
    </div>
  );
}
