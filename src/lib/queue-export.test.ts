/**
 * The worklist as a file someone can send on.
 *
 * The escaping matters more than it looks. This engine's own plain-English
 * text is full of commas, quotes and rupee amounts — "We found 3 payments
 * that add up to Rs 50,000.00 exactly, but nothing ties them to this
 * settlement" — so an export that split on commas naively would corrupt
 * exactly the files this app produces, and it would do it silently.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { exportQueueCsv, type QueueRow } from "@/lib/queue-export";

let captured = "";

beforeEach(() => {
  captured = "";
  // Spy on the two methods rather than replacing the whole URL global —
  // stubbing `URL` wholesale broke `new URL(...)` everywhere else and threw
  // "URL is not a constructor" inside the code under test, which the export
  // swallowed. The tests still passed, which is exactly why it was worth
  // fixing: a stub that breaks the subject silently is worse than no stub.
  vi.spyOn(URL, "createObjectURL").mockImplementation((obj: Blob | MediaSource) => {
    const b = obj as Blob;
    void b.text().then((t) => {
      captured = t;
    });
    return "blob:mock";
  });
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
});

const row = (over: Partial<QueueRow> = {}): QueueRow => ({
  batch_id: "STL-1",
  status: "withheld",
  plain: "We found 3 payments that add up to Rs 50,000.00 exactly, but nothing ties them.",
  reasoning: 'Withheld: linkage found no reference, so the match rests on "arithmetic alone".',
  matched_transactions: [{ txn_id: "T1" }, { txn_id: "T2" }],
  summary: {
    matched_count: 3,
    total_candidates: 41,
    confidence: 0.22,
    tie_out_residual_cents: 0,
    exception_count: 2,
  },
  ...over,
});

async function runExport(rows: QueueRow[]) {
  exportQueueCsv(rows, { cleared: 0, withheld: rows.length });
  // let the async blob read settle
  await new Promise((r) => setTimeout(r, 0));
  return captured;
}

describe("exportQueueCsv", () => {
  it("quotes a field containing commas so the columns survive", async () => {
    const csv = await runExport([row()]);
    expect(csv).toContain(
      '"We found 3 payments that add up to Rs 50,000.00 exactly, but nothing ties them."',
    );
  });

  it("doubles embedded quotes rather than truncating the field", async () => {
    const csv = await runExport([row()]);
    expect(csv).toContain('""arithmetic alone""');
  });

  it("carries the plain explanation and the technical one as separate columns", async () => {
    const csv = await runExport([row()]);
    const header = csv.split("\n").find((l) => l.includes("batch_id")) ?? "";
    expect(header).toContain("what_happened");
    expect(header).toContain("technical_reasoning");
  });

  it("names the matched payments so the file is actionable", async () => {
    const csv = await runExport([row()]);
    expect(csv).toContain("matched_payment_ids");
    expect(csv).toContain("T1 T2");
  });

  it("handles a row with an error and no summary without throwing", async () => {
    const csv = await runExport([
      { batch_id: "BAD", status: "error", error: "Could not read this file." } as QueueRow,
    ]);
    expect(csv).toContain("BAD");
    expect(csv).toContain("Could not read this file.");
  });
});
