import { useEffect, useMemo, useState } from "react";
import type {
  AuditEntry,
  CashBucket,
  CashPositionData,
  ComplianceFindingDetailData,
  ExceptionRecord,
  JournalLine,
  ReconcileResult,
  SettlementAnswer,
} from "@/lib/engine-types";
import {
  AlertCircle,
  CheckCircle2,
  FileWarning,
  ExternalLink,
  ScrollText,
  ChevronDown,
  Wallet,
  MessageSquare,
  Download,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { LoadingMark } from "@/components/loading-mark";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { engineFetch } from "@/lib/api";
import { MatchedPayments } from "@/components/matched-payments";
import { ComplianceReview } from "@/components/compliance-review";
import { ReviewDecision } from "@/components/review-decision";
import { JournalApproval } from "@/components/journal-approval";
import { fetchDecisions, latestFor } from "@/lib/decisions";
import { toast } from "sonner";

/** Engine base URL — matches the constant in routes/index.tsx. */

// A raw ratio like 5/14,179 (0.035%) is completely normal for this system
// -- the candidate pool intentionally contains every uncleared ledger
// entry in the settlement window, most of which are unrelated noise by
// design. toFixed(1) alone rounds anything under 0.05% down to a flat
// "0.0%", which reads as "reconciliation failed" next to a green
// CLEARED badge even when the match was exact and correct. This picks
// enough decimal precision to show the real (small) number instead of
// a misleading zero.
function formatMatchRate(rate: number): string {
  const pct = rate * 100;
  if (pct === 0) return "0%";
  if (pct >= 1) return `${pct.toFixed(1)}%`;
  if (pct >= 0.01) return `${pct.toFixed(2)}%`;
  return `${pct.toFixed(4)}%`;
}

// A compliance stop must be explainable: which rule fired, what value tripped
// it, whether that rule is law or this firm's own risk appetite, and where to
// read the authority behind it. The basis badge is the important part — an
// internal ceiling shown as if it were statute would misrepresent the law to
// whoever reviews this.
// The same three tones the rulebook uses. A rule enforced because the law
// requires it and one enforced because we prefer it must not look alike here
// either — this is where a reviewer meets them.
const BASIS_LABELS: Record<string, { label: string; tone: string }> = {
  statutory: { label: "Statutory", tone: "var(--s-statutory)" },
  regulatory_guidance: { label: "Regulatory guidance", tone: "var(--s-guidance)" },
  internal_policy: { label: "Internal policy — not law", tone: "var(--s-internal)" },
};

function ComplianceFindingDetail({ finding }: { finding: ComplianceFindingDetailData }) {
  const basis = BASIS_LABELS[finding.basis ?? ""] ?? {
    label: finding.basis,
    tone: "var(--muted-foreground)",
  };

  return (
    <div className="mt-2 rounded-md border border-border/60 bg-background/40 p-3 flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold text-foreground">{finding.title}</span>
        <span
          className="whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-medium"
          style={{
            borderColor: `color-mix(in oklab, ${basis.tone} 34%, transparent)`,
            background: `color-mix(in oklab, ${basis.tone} 12%, transparent)`,
            color: basis.tone,
          }}
        >
          {basis.label}
        </span>
        <span className="text-[11px] text-muted-foreground font-mono">{finding.rule_id}</span>
      </div>

      {finding.observed && (
        <p className="text-xs">
          <span className="text-muted-foreground">What triggered it: </span>
          <span className="text-foreground">{finding.observed}</span>
        </p>
      )}

      {finding.authority && (
        <p className="text-xs">
          <span className="text-muted-foreground">Authority: </span>
          <span className="text-foreground">{finding.authority}</span>
        </p>
      )}

      {finding.why && <p className="text-xs text-muted-foreground">{finding.why}</p>}

      {finding.remediation && (
        <p className="text-xs">
          <span className="text-muted-foreground">Next step: </span>
          <span className="text-foreground">{finding.remediation}</span>
        </p>
      )}

      <div className="text-xs flex flex-col gap-1 pt-1 border-t border-border/60">
        <span className="text-muted-foreground">
          Authority: <span className="text-foreground">{finding.authority}</span>
        </span>
        {finding.citation && (
          <span className="text-muted-foreground">
            Reference: <span className="text-foreground">{finding.citation}</span>
          </span>
        )}
        {finding.reference_url && (
          <a
            href={finding.reference_url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-sky-500 hover:underline w-fit"
          >
            Read the official source
            <ExternalLink className="size-3" />
          </a>
        )}
      </div>
    </div>
  );
}

// Indian digit grouping (lakh/crore). A controller reading against crore
// thresholds should not have to count digits, and Intl's en-IN locale does
// this correctly where a plain toLocaleString does not.
function inr(cents: number): string {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
  }).format(cents / 100);
}

// Closing the finance-ops loop. A matched set is not what a finance function
// consumes — they need to know where the cash actually is, and what to post.
// Each bucket implies a different next action, which is why they are split
// this way rather than by source or status.
/**
 * The attestation download.
 *
 * GET /compliance/attestation/{batch_id} has existed since the rulebook was
 * built and nothing in the interface reached it. A reviewer could see that a
 * batch cleared but had no way to produce the document that says so — the
 * scope note, the rulebook as published at the time, the sanctions list
 * provenance, and every compliance event for the batch. So the last step of
 * the job, handing something to a controller or an auditor, was done by
 * reading the screen and retyping it.
 *
 * It downloads the engine's own record rather than anything assembled here.
 * A file this hands to an auditor must be what the engine attested, not the
 * frontend's summary of it.
 */
function AttestationButton({ batchId }: { batchId: string }) {
  const [busy, setBusy] = useState(false);

  const download = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const r = await engineFetch(`compliance/attestation/${encodeURIComponent(batchId)}`);
      if (!r.ok) {
        toast.error(`The engine could not produce an attestation (${r.status}).`);
        return;
      }
      const doc = await r.json();
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" }),
      );
      const a = document.createElement("a");
      a.href = url;
      a.download = `attestation-${batchId}.json`;
      a.click();
      URL.revokeObjectURL(url);
      toast.success(`Attestation for ${batchId} downloaded.`);
    } catch {
      toast.error("Could not reach the engine for the attestation.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      type="button"
      onClick={download}
      disabled={busy}
      className="flex items-center gap-1.5 rounded-full border border-border px-3 py-1 text-sm transition-colors hover:border-accent disabled:opacity-60"
      title="Download the engine's compliance attestation for this batch"
    >
      {busy ? <LoadingMark size={14} /> : <Download className="size-3.5" />}
      {busy ? "Preparing…" : "Attestation"}
    </button>
  );
}

function CashPosition({ cash, batchId }: { cash: CashPositionData; batchId: string }) {
  const j = cash.journal;

  // The journal status and the notes below both arrive inside the reconcile
  // response, which is built BEFORE anyone approves anything. After an
  // approval the badge still read "Proposed — awaiting approval" and the note
  // still read "ready for human approval", directly under a panel saying
  // "Approved for posting by ...". Three statements about one thing, two of
  // them stale.
  //
  // Same fault as the audit trail held: a snapshot rendered as if it were
  // current. This lifts the decision so every part of the card agrees.
  const [journalDecision, setJournalDecision] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    (async () => {
      const entries = await fetchDecisions(batchId);
      if (!live) return;
      const prior = latestFor(entries, { kind: "journal" });
      if (prior) setJournalDecision(prior.verdict.toLowerCase());
    })();
    return () => {
      live = false;
    };
  }, [batchId]);

  return (
    <div className="surface-card p-6 space-y-5">
      <div>
        <h3 className="font-semibold text-lg flex items-center gap-2">
          <Wallet className="size-5 text-muted-foreground" />
          Cash Position
        </h3>
        <p className="text-sm text-muted-foreground mt-1">
          Where the money sits after this reconciliation — not just what matched.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {cash.buckets.map((b: CashBucket) => (
          <div
            key={b.key}
            className={cn(
              "rounded-md border p-4",
              b.key === "reconciled_settled"
                ? "border-emerald-500/40 bg-emerald-500/5"
                : b.key === "compliance_hold"
                  ? "border-red-500/40 bg-red-500/5"
                  : "border-border bg-background/40",
            )}
          >
            <p className="text-xs text-muted-foreground">{b.label}</p>
            <p className="text-lg font-semibold tabular-nums mt-0.5">{inr(b.amount_cents)}</p>
            <p className="text-[11px] text-muted-foreground mt-1">
              {b.count.toLocaleString("en-IN")} record{b.count === 1 ? "" : "s"}
            </p>
            <p className="text-[11px] text-muted-foreground mt-2 leading-relaxed">
              {b.description}
            </p>
          </div>
        ))}
      </div>

      {j && (
        <div className="rounded-md border border-border p-4">
          <div className="flex flex-wrap items-center gap-2 mb-3">
            <span className="font-semibold text-sm">Posting proposal</span>
            <span className="text-[11px] font-mono text-muted-foreground">{j.entry_id}</span>
            <span
              className={cn(
                "px-2 py-0.5 rounded-full border text-[11px] font-medium",
                journalDecision === "approved"
                  ? "bg-emerald-500/15 text-emerald-500 border-emerald-500/30"
                  : journalDecision === "rejected" || j.status !== "proposed"
                    ? "bg-red-500/15 text-red-500 border-red-500/30"
                    : "bg-sky-500/15 text-sky-500 border-sky-500/30",
              )}
            >
              {journalDecision === "approved"
                ? "Approved for posting"
                : journalDecision === "rejected"
                  ? "Rejected by reviewer"
                  : j.status === "proposed"
                    ? "Proposed — awaiting approval"
                    : "Rejected"}
            </span>
            <span
              className={cn(
                "px-2 py-0.5 rounded-full border text-[11px] font-medium",
                j.balanced
                  ? "bg-emerald-500/15 text-emerald-500 border-emerald-500/30"
                  : "bg-red-500/15 text-red-500 border-red-500/30",
              )}
            >
              {j.balanced ? "Balanced" : `Out by ${inr(Math.abs(j.imbalance_cents ?? 0))}`}
            </span>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-muted-foreground border-b border-border">
                  <th className="text-left font-normal pb-2">Account</th>
                  <th className="text-right font-normal pb-2">Debit</th>
                  <th className="text-right font-normal pb-2">Credit</th>
                </tr>
              </thead>
              <tbody>
                {j.lines.map((l: JournalLine, i: number) => (
                  <tr key={i} className="border-b border-border/50">
                    <td className="py-2 pr-4">
                      <span className="font-mono text-xs">{l.account}</span>
                      <span className="block text-[11px] text-muted-foreground">{l.memo}</span>
                    </td>
                    <td className="text-right tabular-nums py-2 whitespace-nowrap">
                      {l.debit_inr ? `₹${l.debit_inr.toLocaleString("en-IN")}` : ""}
                    </td>
                    <td className="text-right tabular-nums py-2 whitespace-nowrap">
                      {l.credit_inr ? `₹${l.credit_inr.toLocaleString("en-IN")}` : ""}
                    </td>
                  </tr>
                ))}
                <tr className="font-semibold">
                  <td className="py-2">Total</td>
                  <td className="text-right tabular-nums py-2 whitespace-nowrap">
                    ₹{j.total_debits_inr.toLocaleString("en-IN")}
                  </td>
                  <td className="text-right tabular-nums py-2 whitespace-nowrap">
                    ₹{j.total_credits_inr.toLocaleString("en-IN")}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          {j.rejection_reason && <p className="text-xs text-red-500 mt-3">{j.rejection_reason}</p>}
          <p className="text-[11px] text-muted-foreground mt-3">{j.basis}</p>

          {/* "Proposed — awaiting approval" was a status with nowhere to
              approve it. Same fault as the batch decision: the app asserted a
              human step and gave no way to take it. */}
          {j.status === "proposed" && (
            <JournalApproval
              onDecided={setJournalDecision}
              batchId={batchId}
              entryId={j.entry_id ?? ""}
              balanced={!!j.balanced}
            />
          )}
        </div>
      )}

      {/* Once a decision exists, the engine's "ready for human approval" note
          is describing a step that has already happened. Suppressed rather
          than rewritten — the note is the engine's, and the decision panel
          above states the current position. */}
      {cash.notes
        ?.filter((n: string) => !(journalDecision && /ready for human approval/i.test(n)))
        .map((n: string, i: number) => (
          <p key={i} className="text-xs text-muted-foreground">
            {n}
          </p>
        ))}
    </div>
  );
}

// Plain-language Q&A over a completed reconciliation.
//
// Every figure in an answer comes from what the engine recorded — the audit
// trail, match result, cash position and compliance findings. The model
// explains those facts; it does not compute, match or decide anything. The
// suggested questions are the ones a controller actually asks first, and they
// double as a demonstration that the answers are grounded rather than generic.
const SUGGESTED_QUESTIONS = [
  "Why did this settlement clear?",
  "What is my cash position?",
  "Which transactions were blocked, and on what authority?",
  "What still needs a human decision?",
];

function SettlementQA({ batchId }: { batchId: string }) {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<SettlementAnswer | null>(null);
  const [loading, setLoading] = useState(false);

  const ask = async (q: string) => {
    const text = q.trim();
    if (!text || loading) return;
    setLoading(true);
    setAnswer(null);
    setQuestion(text);
    try {
      const res = await engineFetch(`settlement/${encodeURIComponent(batchId)}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: text }),
      });
      setAnswer(await res.json());
    } catch (e: unknown) {
      setAnswer({
        available: false,
        answer: `Could not reach the engine (${
          e instanceof Error ? e.message : String(e)
        }). The reconciliation results above are unaffected.`,
      });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="surface-card p-6 space-y-4">
      <div>
        <h3 className="font-semibold text-lg flex items-center gap-2">
          <MessageSquare className="size-5 text-muted-foreground" />
          Ask about this settlement
        </h3>
        <p className="text-sm text-muted-foreground mt-1">
          Answers are grounded in the engine's recorded results — the audit trail, match result,
          cash position and compliance findings. Nothing is computed at question time.
        </p>
      </div>

      <div className="flex flex-wrap gap-2">
        {SUGGESTED_QUESTIONS.map((q) => (
          <button
            key={q}
            onClick={() => ask(q)}
            disabled={loading}
            className="rounded-full border border-border px-3 py-1 text-xs hover:bg-muted disabled:opacity-50"
          >
            {q}
          </button>
        ))}
      </div>

      <div className="flex gap-2">
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && ask(question)}
          placeholder="Ask anything about this reconciliation…"
          className="h-9 flex-1 rounded-md border border-border bg-background px-3 text-sm"
        />
        <Button size="sm" onClick={() => ask(question)} disabled={loading || !question.trim()}>
          {loading ? (
            <>
              <LoadingMark size={15} onSolid className="mr-1.5" />
              Thinking…
            </>
          ) : (
            "Ask"
          )}
        </Button>
      </div>

      {answer && (
        <div
          className={cn(
            "rounded-md border p-4 text-sm whitespace-pre-wrap",
            answer.available === false
              ? "border-amber-500/40 bg-amber-500/5"
              : "border-border bg-background/40",
          )}
        >
          {answer.answer}
          {answer.grounded === false && (
            <p className="text-xs text-muted-foreground mt-2">
              No recorded results for this batch — the engine will not answer from assumption.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// The audit trail is the engine's answer to "why did you decide that?". It was
// already returned by the API and rendered nowhere, so the one artifact that
// makes the pipeline explainable was effectively invisible. Collapsed by
// default because it runs to tens of thousands of entries at scale.
function AuditTrail({ trail, batchId }: { trail: AuditEntry[]; batchId: string }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  // The trail arrives in the reconcile response, which is built BEFORE any
  // human decision exists — so a reviewer who confirmed a batch, approved a
  // posting or cleared a finding then looked at the audit trail found no
  // trace of what they had just done. It was in the store; this screen was
  // holding a snapshot taken a moment too early.
  //
  // Decision controls announce themselves, and this refetches. A trail that
  // omits the human half of the process is the specific thing this panel
  // exists to disprove.
  const [live, setLive] = useState<AuditEntry[] | null>(null);
  useEffect(() => {
    if (!batchId) return;
    let alive = true;
    const refetch = async () => {
      try {
        const r = await engineFetch(`audit/${encodeURIComponent(batchId)}`);
        if (!r.ok || !alive) return;
        const j = await r.json();
        if (Array.isArray(j.trail)) setLive(j.trail);
      } catch {
        /* Keep the snapshot we already have rather than blanking the panel. */
      }
    };
    const onDecision = (e: Event) => {
      const d = (e as CustomEvent).detail;
      if (!d?.batchId || d.batchId === batchId) void refetch();
    };
    window.addEventListener("among:decision-recorded", onDecision);
    return () => {
      alive = false;
      window.removeEventListener("among:decision-recorded", onDecision);
    };
  }, [batchId]);

  const entries = live ?? trail;
  const humanCount = useMemo(
    () => entries.filter((e: AuditEntry) => e.agent === "human_reviewer").length,
    [entries],
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return entries;
    return entries.filter((e: AuditEntry) => `${e.agent} ${e.detail}`.toLowerCase().includes(q));
  }, [entries, query]);

  const agents = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const e of entries) counts[e.agent] = (counts[e.agent] ?? 0) + 1;
    return Object.entries(counts).sort((a, b) => b[1] - a[1]);
  }, [entries]);

  if (entries.length === 0) return null;

  return (
    <div className="surface-card p-6">
      <button onClick={() => setOpen(!open)} className="w-full flex items-center gap-2 text-left">
        <ScrollText className="size-5 text-muted-foreground" />
        <h3 className="font-semibold text-lg">Audit Trail ({entries.length.toLocaleString()})</h3>
        {humanCount > 0 && (
          <span
            className="rounded-full px-2 py-[3px] text-[10px] uppercase"
            style={{
              color: "var(--s-done)",
              background: "color-mix(in oklab, var(--s-done) 14%, transparent)",
            }}
          >
            {humanCount} human decision{humanCount === 1 ? "" : "s"}
          </span>
        )}
        <ChevronDown
          className={cn(
            "size-4 ml-auto text-muted-foreground transition-transform",
            open && "rotate-180",
          )}
        />
      </button>

      <p className="text-sm text-muted-foreground mt-1">
        Every decision the engine made for batch{" "}
        <span className="font-mono text-foreground">{batchId}</span> — which agent acted, what it
        decided, and why.
      </p>

      <div className="flex flex-wrap gap-2 mt-3">
        {agents.map(([agent, count]) => (
          <span
            key={agent}
            className="px-2 py-0.5 rounded-full border border-border bg-muted/50 text-[11px]"
          >
            {agent.replace(/_/g, " ")} · {count.toLocaleString()}
          </span>
        ))}
      </div>

      {open && (
        <div className="mt-4 space-y-3">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by agent or detail…"
            className="h-9 w-full rounded-md border border-border bg-background px-3 text-sm"
          />
          <p className="text-xs text-muted-foreground">
            Showing {Math.min(filtered.length, 200).toLocaleString()} of{" "}
            {filtered.length.toLocaleString()} matching entries
            {filtered.length > 200 && " (first 200)"}.
          </p>
          <div className="max-h-96 overflow-y-auto rounded-md border border-border divide-y divide-border">
            {filtered.slice(0, 200).map((e: AuditEntry, i: number) => (
              <div key={i} className="p-3 text-xs flex flex-col gap-1">
                <div className="flex items-center gap-2">
                  <span className="font-mono font-semibold text-foreground">{e.agent}</span>
                  <span className="text-muted-foreground">{e.timestamp_utc}</span>
                </div>
                <span className="text-muted-foreground break-words">{e.detail}</span>
              </div>
            ))}
            {filtered.length === 0 && (
              <p className="p-3 text-xs text-muted-foreground">No entries match that filter.</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export function ResultsSkeleton({ progress }: { progress: number }) {
  return (
    <div className="space-y-4">
      <div className="surface-card p-5">
        <div className="flex items-center justify-between text-sm">
          <span className="font-medium">Linking & Reconciling…</span>
          <span className="tabular-nums text-muted-foreground">{Math.round(progress)}%</span>
        </div>
        <div className="mt-4 h-1.5 overflow-hidden rounded-full bg-secondary">
          <div
            className="h-full rounded-full bg-accent transition-all duration-300 ease-out"
            style={{ width: `${progress}%` }}
          />
        </div>
      </div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="surface-card p-5">
            <Skeleton className="h-3 w-20" />
            <Skeleton className="mt-4 h-8 w-24" />
          </div>
        ))}
      </div>
      <div className="surface-card p-5">
        <Skeleton className="h-3 w-40" />
        <Skeleton className="mt-6 h-64 w-full" />
      </div>
    </div>
  );
}

export function ResultsPanel({ results }: { results: ReconcileResult }) {
  const s = results.summary;

  // Plain-language headline, independent of pool size, so a tiny
  // percentage next to a success badge doesn't read as a contradiction.
  const headline = s.cleared
    ? `Found an exact match: ${s.matched_count} transaction${s.matched_count === 1 ? "" : "s"} reconciled to this settlement.`
    : s.ambiguous
      ? `Found a candidate match, but it wasn't unique — flagged for human review before clearing.`
      : `No confident match found for this settlement — see exceptions below.`;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-display text-2xl">Reconciliation Report</h2>
        <AttestationButton batchId={s.batch_id} />
        <div
          className={cn(
            "px-3 py-1 rounded-full text-sm font-semibold border flex items-center gap-2",
            s.cleared
              ? "bg-emerald-500/10 text-emerald-600 border-emerald-500/20"
              : s.ambiguous
                ? "bg-amber-500/10 text-amber-600 border-amber-500/20"
                : "bg-red-500/10 text-red-600 border-red-500/20",
          )}
        >
          {s.cleared ? <CheckCircle2 className="size-4" /> : <AlertCircle className="size-4" />}
          {s.cleared ? "CLEARED" : s.ambiguous ? "AMBIGUOUS (Needs Review)" : "UNMATCHED"}
        </div>
      </div>

      {/* The plain statement leads. `headline` is a short label; this is the
          paragraph a reviewer acts on, written in rupees and without the
          engine's vocabulary. Generated by the engine so this card, the queue
          and the CSV cannot describe the same outcome differently. */}
      {results.plain_summary ? (
        <p className="narrative-copy -mt-2 text-[14px] leading-[1.6] text-foreground">
          {results.plain_summary}
        </p>
      ) : (
        <p className="text-sm text-muted-foreground -mt-2">{headline}</p>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className="surface-card p-5">
          <p className="text-xs text-muted-foreground mb-1">Pool Coverage</p>
          <p className="text-2xl font-semibold">{formatMatchRate(s.match_rate)}</p>
          <p className="text-[11px] text-muted-foreground mt-1">
            of scanned ledger entries in window
          </p>
        </div>
        <div className="surface-card p-5">
          <p className="text-xs text-muted-foreground mb-1">Method Used</p>
          <p className="text-xl font-semibold capitalize">{s.method.replace("_", " ")}</p>
        </div>
        <div className="surface-card p-5">
          <p className="text-xs text-muted-foreground mb-1">Transactions Matched</p>
          <p className="text-xl font-semibold">
            {s.matched_count} / {s.total_candidates}
          </p>
        </div>
        <div className="surface-card p-5">
          <p className="text-xs text-muted-foreground mb-1">False Positive Risk</p>
          <p className="text-xl font-semibold">
            {s.false_positive_cost_estimate_cents
              ? inr(s.false_positive_cost_estimate_cents)
              : "₹0.00"}
          </p>
        </div>
      </div>

      {/* Which payments, not just how many. This was the number "6 / 45" and
          nothing else — a reviewer asked to confirm a set they could not see. */}
      {(results.matched_transactions?.length ?? 0) > 0 && (
        <MatchedPayments
          rows={results.matched_transactions ?? []}
          batchId={results.summary.batch_id}
          targetCents={s.target_cents}
          interchangeable={results.interchangeable}
        />
      )}

      {/* The decision goes directly under the payments it is about, so a
          reviewer confirms what they are looking at rather than scrolling
          back to check what they just agreed to. */}
      <ReviewDecision
        batchId={s.batch_id}
        txnIds={results.matched_txn_ids ?? []}
        cleared={!!s.cleared}
      />

      {/* Compliance sits above the cash position: what a reviewer may not
          touch has to be settled before where the money is means anything. */}
      <ComplianceReview review={results.compliance_review} batchId={s.batch_id} />

      {results.cash_position && <CashPosition cash={results.cash_position} batchId={s.batch_id} />}

      <SettlementQA batchId={s.batch_id} />

      <AuditTrail trail={results.audit_trail ?? []} batchId={s.batch_id} />

      <div className="surface-card p-6">
        <h3 className="mb-4 flex items-center gap-2 text-lg font-semibold">
          <FileWarning className="size-5 text-muted-foreground" />
          Exceptions ({results.exceptions.length})
        </h3>
        {results.exceptions.length === 0 ? (
          <p className="text-sm text-muted-foreground">No exceptions found. Perfect match.</p>
        ) : (
          <div className="space-y-2">
            {results.exceptions.slice(0, 10).map((ex: ExceptionRecord, i: number) => (
              // Amber, not red. An exception is the engine routing work to a
              // human, which is the behaviour that produces zero false
              // clears. Painting it as an error argues against the product.
              <div
                key={i}
                className="flex flex-col gap-1 rounded-r-[10px] border-l-2 px-3 py-3 text-sm"
                style={{
                  borderColor: "var(--s-withheld)",
                  background: "color-mix(in oklab, var(--s-withheld) 6%, transparent)",
                }}
              >
                <span className="font-semibold capitalize" style={{ color: "var(--s-withheld)" }}>
                  {ex.reason.replace(/_/g, " ")}
                </span>
                <span className="text-muted-foreground">{ex.diagnosis_note}</span>
                {(ex.findings ?? []).map((f: ComplianceFindingDetailData, j: number) => (
                  <ComplianceFindingDetail key={j} finding={f} />
                ))}
              </div>
            ))}
            {results.exceptions.length > 10 && (
              <p className="mt-4 text-xs italic text-muted-foreground">
                …and {results.exceptions.length - 10} more. Click any node on the pipeline above to
                read that agent's lines.
              </p>
            )}
          </div>
        )}
      </div>

      <p className="font-mono text-[11px] text-muted-foreground">
        Audit trail · {results.audit_trail?.length ?? 0} entries · click any node above to read its
        lines
      </p>
    </div>
  );
}
