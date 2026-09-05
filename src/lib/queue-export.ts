/**
 * The triage run, as a file someone can send on.
 *
 * Lifted out of the queue route when that page was merged into the main
 * screen. The worklist is the product — "these five need you" — and it was
 * trapped behind a second page that never received any of the features the
 * main one grew. Keeping the export here means the merge loses nothing.
 */

export interface QueueRow {
  batch_id: string;
  status: string;
  plain?: string;
  reasoning?: string;
  error?: string;
  matched_count?: number;
  exception_count?: number;
  matched_transactions?: { txn_id: string }[];
  summary?: {
    matched_count?: number;
    total_candidates?: number;
    confidence?: number;
    tie_out_residual_cents?: number;
    exception_count?: number;
    target_cents?: number;
  };
}

export function exportQueueCsv(rows: QueueRow[], tally: Record<string, number>) {
  const esc = (v: unknown) => {
    const t = String(v ?? "");
    // A reasoning string contains commas and quotes, and a batch id could.
    return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
  };
  const header = [
    "batch_id",
    "status",
    "matched_count",
    "total_candidates",
    "confidence",
    "tie_out_residual_cents",
    "exception_count",
    // Plain first here too: whoever opens this file in a spreadsheet is the
    // same person the plain wording is for. The technical column stays, so
    // the file remains a complete record.
    "matched_payment_ids",
    "what_happened",
    "technical_reasoning",
  ];
  const lines = [header.join(",")];
  for (const r of rows) {
    lines.push(
      [
        r.batch_id,
        r.status,
        r.summary?.matched_count ?? "",
        r.summary?.total_candidates ?? "",
        r.summary?.confidence ?? "",
        r.summary?.tie_out_residual_cents ?? "",
        r.summary?.exception_count ?? r.exception_count ?? "",
        (r.matched_transactions ?? []).map((t) => t.txn_id).join(" "),
        r.plain ?? r.error ?? "",
        r.reasoning ?? "",
      ]
        .map(esc)
        .join(","),
    );
  }
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
  const summary = Object.entries(tally)
    .map(([k, v]) => `${k}=${v}`)
    .join(" ");
  const blob = new Blob([`# AmongResolver queue — ${stamp} — ${summary}\n${lines.join("\n")}\n`], {
    type: "text/csv",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `queue-${stamp}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}
