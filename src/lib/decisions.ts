/**
 * What has already been decided on a batch.
 *
 * WHY THIS EXISTS
 * ---------------
 * Once a reviewer has confirmed or rejected something, offering them the
 * buttons again is asking a question that has been answered. Worse, the first
 * version kept the controls after a decision and reset entirely on reload —
 * so a refreshed page presented a settled batch as though nobody had looked
 * at it, which is exactly the state the audit trail exists to prevent.
 *
 * Decisions live in the audit trail, so they survive a reload and a different
 * browser. This reads them back and tells each control whether its question
 * is already settled.
 *
 * ON THE PARSING
 * --------------
 * The audit trail stores a decision as one prose line, because that is what
 * makes it readable to a human opening the trail six months later — which is
 * the trail's whole job. Recovering structure from it means matching the
 * markers this app itself writes ("CONFIRMED this batch", "the posting
 * proposal", "compliance finding X"). That coupling is real: change the
 * wording in main.py and this stops recognising older entries. It is the
 * right trade for now — the alternative is a second decisions table
 * duplicating the trail — but it is a coupling, not an accident, and the
 * markers should move together.
 */

import { engineFetch } from "@/lib/api";

export type DecisionScope =
  { kind: "batch" } | { kind: "journal" } | { kind: "compliance"; ruleId: string };

export interface RecordedDecision {
  verdict: string; // CONFIRMED | REJECTED | APPROVED | CLEARED | ESCALATED
  reviewer: string;
  note: string;
  detail: string;
  ts: string;
}

interface TrailEntry {
  ts?: string;
  timestamp?: string;
  detail?: string;
}

/** "alice@x.com CONFIRMED this batch covering 41 payment(s). Note: ..." */
function parse(detail: string, ts: string): RecordedDecision | null {
  const m = detail.match(/^(\S+)\s+(CONFIRMED|REJECTED|APPROVED|CLEARED|ESCALATED)\b/);
  if (!m) return null;
  const noteMatch = detail.match(
    /Note:\s*(.*?)(?:\s+(?:This is the reviewer|Nothing has been posted|This finding rests)|$)/,
  );
  return {
    reviewer: m[1] ?? "",
    verdict: m[2] ?? "",
    note: (noteMatch?.[1] ?? "").trim(),
    detail,
    ts,
  };
}

function matches(detail: string, scope: DecisionScope): boolean {
  if (scope.kind === "journal") return /posting proposal/i.test(detail);
  if (scope.kind === "compliance") {
    return new RegExp(`compliance finding\\s+${scope.ruleId}\\b`).test(detail);
  }
  // A batch decision is the one that is neither of the others.
  return (
    /\b(CONFIRMED|REJECTED)\s+this batch\b/.test(detail) &&
    !/posting proposal/i.test(detail) &&
    !/compliance finding/i.test(detail)
  );
}

/**
 * Every human decision recorded against this batch, newest first per scope.
 * Returns null on any failure — a control that cannot read the history should
 * offer its buttons rather than wrongly claim the question is settled.
 */
export async function fetchDecisions(batchId: string): Promise<TrailEntry[] | null> {
  if (!batchId) return null;
  try {
    const res = await engineFetch(`settlement/${encodeURIComponent(batchId)}/decisions`);
    if (!res.ok) return null;
    const data = await res.json();
    return Array.isArray(data?.decisions) ? data.decisions : [];
  } catch {
    return null;
  }
}

/** The most recent decision for one scope, or null if it is still open. */
export function latestFor(
  entries: TrailEntry[] | null,
  scope: DecisionScope,
): RecordedDecision | null {
  if (!entries) return null;
  for (let i = entries.length - 1; i >= 0; i--) {
    const detail = entries[i]?.detail ?? "";
    if (!matches(detail, scope)) continue;
    const parsed = parse(detail, entries[i]?.ts ?? entries[i]?.timestamp ?? "");
    if (parsed) return parsed;
  }
  return null;
}
