/**
 * Reading decisions back out of the audit trail.
 *
 * A decision already made must not be asked again — including after a reload,
 * which once reset every control and presented a settled batch as though
 * nobody had looked at it. That recovery works by matching markers this app
 * itself writes into the trail's prose, which is a real coupling: change the
 * wording in main.py and these stop matching. These tests are where that
 * breakage shows up as a failure rather than as a silently re-opened
 * question.
 */

import { describe, it, expect } from "vitest";
import { latestFor } from "@/lib/decisions";

const entry = (detail: string, ts = "2026-09-02T10:00:00Z") => ({ ts, detail });

// The exact strings main.py writes.
const BATCH_CONFIRM =
  "alice@x.com CONFIRMED this batch covering 41 payment(s). Note: Checked the dashboard. This is the reviewer's decision and does not alter the engine's own verdict.";
const BATCH_REJECT =
  "bob@x.com REJECTED this batch covering 41 payment(s). This is the reviewer's decision and does not alter the engine's own verdict.";
const JOURNAL_APPROVE =
  "alice@x.com APPROVED the posting proposal (entry JE-1). Note: Fees agree. Nothing has been posted to any ledger — this records the approval, not the posting.";
const COMPLIANCE_ESCALATE =
  "alice@x.com ESCALATED compliance finding STRUCTURING_PATTERN covering 4 payment(s).";
const COMPLIANCE_CLEAR_STATUTORY =
  "alice@x.com CLEARED compliance finding CTR_THRESHOLD. Note: Verified. This finding rests on a STATUTORY obligation: clearing the review records a judgement about the transaction and does NOT discharge any reporting duty.";

describe("latestFor — scope separation", () => {
  const all = [entry(BATCH_CONFIRM), entry(JOURNAL_APPROVE), entry(COMPLIANCE_ESCALATE)];

  it("finds the batch decision without matching the journal or a finding", () => {
    const d = latestFor(all, { kind: "batch" });
    expect(d?.verdict).toBe("CONFIRMED");
    expect(d?.reviewer).toBe("alice@x.com");
  });

  it("finds the journal approval and does not mistake it for a batch decision", () => {
    expect(latestFor(all, { kind: "journal" })?.verdict).toBe("APPROVED");
  });

  it("matches a compliance finding only by its own rule id", () => {
    expect(latestFor(all, { kind: "compliance", ruleId: "STRUCTURING_PATTERN" })?.verdict).toBe(
      "ESCALATED",
    );
    // A different rule on the same batch is still an open question.
    expect(latestFor(all, { kind: "compliance", ruleId: "DUPLICATE_TX" })).toBeNull();
  });
});

describe("latestFor — the most recent decision wins", () => {
  it("returns the last decision, not the first", () => {
    const d = latestFor([entry(BATCH_CONFIRM), entry(BATCH_REJECT)], { kind: "batch" });
    expect(d?.verdict).toBe("REJECTED");
    expect(d?.reviewer).toBe("bob@x.com");
  });
});

describe("latestFor — note extraction", () => {
  it("keeps the reviewer's note and drops the engine's trailing boilerplate", () => {
    const d = latestFor([entry(BATCH_CONFIRM)], { kind: "batch" });
    expect(d?.note).toBe("Checked the dashboard.");
    expect(d?.note).not.toContain("This is the reviewer");
  });

  it("stops a statutory caveat leaking into the note", () => {
    const d = latestFor([entry(COMPLIANCE_CLEAR_STATUTORY)], {
      kind: "compliance",
      ruleId: "CTR_THRESHOLD",
    });
    expect(d?.note).toBe("Verified.");
    expect(d?.note).not.toContain("STATUTORY");
  });
});

describe("latestFor — open questions stay open", () => {
  it("returns null when nothing has been decided", () => {
    expect(latestFor([], { kind: "batch" })).toBeNull();
  });

  it("returns null rather than guessing when the trail could not be read", () => {
    // A control that cannot read history must offer its buttons, not claim
    // the question is settled.
    expect(latestFor(null, { kind: "batch" })).toBeNull();
  });

  it("ignores entries that are not decisions", () => {
    const noise = [entry("linkage: Cleared on the 'all_linked' tier: 3 of 3")];
    expect(latestFor(noise, { kind: "batch" })).toBeNull();
  });
});
