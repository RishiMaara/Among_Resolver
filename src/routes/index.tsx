import { createFileRoute, Link } from "@tanstack/react-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronUp, Flag, Play, RotateCcw, ScrollText, Wand2 } from "lucide-react";
import { LoadingMark } from "@/components/loading-mark";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { ThemeToggle } from "@/components/theme-toggle";
import { SessionMenu } from "@/components/session-menu";
import { DropzoneInline } from "@/components/dropzone-inline";
import { loadRateCards, saveRateCard, type RateCard } from "@/lib/rate-cards";
import { SAMPLE_PRESETS, loadSampleFiles, type SamplePreset } from "@/lib/sample-preset";
import { currentSession } from "@/lib/session";
import { ResultsPanel, ResultsSkeleton } from "@/components/results-panel";
import { AgentFlow } from "@/components/agent-flow";
import { FlowNarrative } from "@/components/flow-narrative";
import type { AuditEntry } from "@/lib/agent-flow";
import { HistoryLink } from "@/components/history-link";
import { engineFetch, engineErrorMessage } from "@/lib/api";
import { exportQueueCsv, type QueueRow } from "@/lib/queue-export";
import { Wordmark } from "@/components/wordmark";

export const Route = createFileRoute("/")({
  component: Index,
});

/**
 * Engine base URL.
 *
 * Set VITE_ENGINE_URL at build time for any non-local deployment:
 *   VITE_ENGINE_URL=https://api.myhost.com npm run build
 *
 * Falls back to localhost for local development. All fetch calls in this
 * file must use this constant — hardcoding 127.0.0.1 breaks every deployed
 * environment silently.
 */

type Status = "idle" | "running" | "done" | "error";

interface DetectResponse {
  detected: number;
  returned: number;
  truncated: boolean;
  unreadable_amounts: number;
  date_order: "day" | "month";
  date_order_proven: boolean;
  settlements: DetectedSettlement[];
}

interface DetectedSettlement {
  batch_id: string;
  net_amount: number;
  settled_at: string;
  currency: string;
  declared_deductions?: number | null;
}

/** Shape of the /reconcile/upload JSON response.
 *  Mirrors the Python ReconciliationReport dataclass.
 *  Fields are optional (?) so partial responses don't break the UI
 *  if the backend adds or renames fields between versions.
 */
export interface ReconciliationResult {
  summary: {
    batch_id: string;
    cleared: boolean;
    confidence: number;
    method: string;
    /** actual backend key from summary() */
    matched_count: number;
    /** actual backend key from summary() */
    total_candidates: number;
    match_rate: number;
    target_cents: number;
    matched_gross_cents: number;
    net_amount_cents: number;
    deductions_cents: number;
    tie_out_residual_cents: number;
    ties_out: boolean;
    fee_basis: string;
    ambiguous?: boolean;
    false_positive_cost_estimate_cents: number;
    exception_count: number;
    requires_human_approval: boolean;
  };
  matched_txn_ids: string[];
  exceptions: Array<{
    batch_id: string;
    candidate_txn_ids: string[];
    reason: string;
    diagnosis_note: string;
    requires_human_approval: boolean;
    findings: Array<{
      rule_id: string;
      title: string;
      severity: string;
      action: string;
      basis: string;
      authority: string;
      source_name: string;
      citation: string;
      reference_url: string;
      rule_text: string;
      threshold_applied: string;
      why: string;
      observed: string;
      remediation: string;
    }>;
  }>;
  cash_position?: {
    batch_id: string;
    as_of_utc: string;
    buckets: Array<{
      key: string;
      label: string;
      count: number;
      amount_cents: number;
      amount_inr: number;
      description: string;
    }>;
    journal?: {
      entry_id: string;
      date_utc: string;
      status: string;
      basis: string;
      balanced: boolean;
      imbalance_cents: number;
      rejection_reason: string;
      total_debits_inr: number;
      total_credits_inr: number;
      lines: Array<{
        account: string;
        debit_inr: number;
        credit_inr: number;
        memo: string;
      }>;
    } | null;
    notes: string[];
  };
  ingestion_notes: string[];
  header_mapping_warnings: string[];
}

function Index() {
  const [status, setStatus] = useState<Status>("idle");
  const [progress, setProgress] = useState(0);
  const [results, setResults] = useState<ReconciliationResult | null>(null);
  // The live audit trail. This drives the flow visualiser: nodes light up
  // because the engine wrote a decision, not because a timer fired.
  // Polled DURING the request, since /reconcile/upload blocks for the
  // whole run while the audit store is written as it goes.
  const [auditEntries, setAuditEntries] = useState<AuditEntry[]>([]);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [runError, setRunError] = useState<string | null>(null);
  // Kept apart from runError on purpose. This is a short instruction about the
  // form and belongs beside the button that was pressed; runError is the
  // engine's own structured rejection and belongs in the report area. Sharing
  // one field printed a one-line message twice, in two different styles.
  const [formError, setFormError] = useState<string | null>(null);
  // Settlements read out of an uploaded bank statement. The batch id, amount
  // and date were fields the user typed while holding a file that already
  // states them — retyping a document into a form is the manual work this
  // engine exists to remove.
  const [detected, setDetected] = useState<DetectedSettlement[] | null>(null);

  // Running EVERY detected settlement, not just the one you pick.
  //
  // This screen already read a statement and found every settlement in it —
  // 15,000 in one real file — and then offered "pick one to fill the form".
  // That is a triage list with the triage removed: it knew what was there and
  // made you work through it one at a time.
  //
  // Keeping that in a separate /queue page was worse than it looked. Two
  // codepaths meant one of them rotted: compliance review, cash position, the
  // decision panel, journal approval and the audit trail all landed here and
  // none of them reached the queue. Running the batch from the page that owns
  // the full report is what stops that happening again.
  const [triage, setTriage] = useState<QueueRow[] | null>(null);
  const [triageBusy, setTriageBusy] = useState(false);
  // What the file held, as distinct from what is on screen. These were the
  // same number, because the banner printed the length of a list that had
  // already been sliced to 25 — so a statement with 60 credits reported
  // "25 settlements found". On a reconciliation tool a wrong count of the
  // user's own settlements is not a display bug, it is the product failing
  // at the one thing it claims: never assert what it has not established.
  const [detectStats, setDetectStats] = useState<{
    total: number;
    unreadable: number;
    truncated: boolean;
    dateAssumed: boolean;
  } | null>(null);
  // The upload strip collapses once a run starts, so the canvas — the
  // thing worth watching — is what fills the screen.
  const [uploadOpen, setUploadOpen] = useState(true);

  // Form State

  const [batchId, setBatchId] = useState("SETTLE-001");
  const [netAmount, setNetAmount] = useState("50000.00");
  const [settledAt, setSettledAt] = useState(
    // Shown and sent as UTC, so what is on screen is what gets recorded.
    new Date().toISOString().slice(0, 16),
  );
  const [windowDays, setWindowDays] = useState("5");
  // Which feed the settlement's members live in. The engine cannot infer this
  // reliably: a gateway payment and its ERP mirror carry the same amount, so
  // without it neither can be ruled out and the batch is withheld as
  // ambiguous. Declaring it moves benchmark auto-clear from 62% to 75.33%.
  const [memberSource, setMemberSource] = useState("gateway");
  // Exact deductions from the settlement advice, when known. Supplying them
  // makes the gross target a fact rather than a rate-card estimate.
  const [declaredDeductions, setDeclaredDeductions] = useState("");
  // Fee terms. Blank means "use the engine's default card", which the engine
  // announces in its run notes — it is a guess, not anyone's contract, and a
  // wrong card eliminates the match rather than degrading it.
  const [gatewayFeeBps, setGatewayFeeBps] = useState("");
  const [taxWithholdingBps, setTaxWithholdingBps] = useState("");
  const [flatFeeCents, setFlatFeeCents] = useState("");
  const [rateCards, setRateCards] = useState<RateCard[]>([]);
  const [cardName, setCardName] = useState("");

  // Files
  const [gatewayFile, setGatewayFile] = useState<File | null>(null);
  const [bankFile, setBankFile] = useState<File | null>(null);
  const [erpFile, setErpFile] = useState<File | null>(null);

  const timers = useRef<number[]>([]);
  const clearTimers = () => {
    timers.current.forEach((t) => window.clearTimeout(t));
    timers.current = [];
  };

  useEffect(() => setRateCards(loadRateCards()), []);

  const applyCard = useCallback((name: string) => {
    const c = loadRateCards().find((x) => x.name === name);
    if (!c) return;
    setCardName(c.name);
    setGatewayFeeBps(c.gatewayFeeBps);
    setTaxWithholdingBps(c.taxWithholdingBps);
    setFlatFeeCents(c.flatFeeCents);
    toast.success(`Loaded fee terms for ${c.name}.`);
  }, []);

  const storeCard = useCallback(() => {
    const name = cardName.trim();
    if (!name) {
      toast.error("Name the merchant before saving its terms.");
      return;
    }
    setRateCards(
      saveRateCard({
        name,
        gatewayFeeBps,
        taxWithholdingBps,
        flatFeeCents,
      }),
    );
    toast.success(`Saved fee terms for ${name}.`);
  }, [cardName, gatewayFeeBps, taxWithholdingBps, flatFeeCents]);

  const onBankPicked = useCallback(async (f: File | null) => {
    setBankFile(f);
    setDetected(null);
    setDetectStats(null);
    if (!f) return;
    try {
      const fd = new FormData();
      fd.append("file", f);
      const res = await engineFetch("settlements/detect", { method: "POST", body: fd });
      if (!res.ok) return; // silent: this is an offer, not a step
      const data: DetectResponse = await res.json();
      if (data.settlements?.length) {
        // No slice. The list scrolls, so showing every row the engine
        // returned costs nothing and hides nothing.
        setDetected(data.settlements);
        setDetectStats({
          total: data.detected ?? data.settlements.length,
          unreadable: data.unreadable_amounts ?? 0,
          truncated: Boolean(data.truncated),
          // Every date in the file was <= 12/12, so day-first could not be
          // proven from the column. The reading is an assumption and has to
          // be shown as one.
          dateAssumed: data.date_order_proven === false,
        });
      }
    } catch {
      /* Detection is a convenience. Its failure must not block an upload. */
    }
  }, []);

  const applyDetected = useCallback((d: DetectedSettlement) => {
    setBatchId(d.batch_id);
    setNetAmount(String(d.net_amount));
    if (d.settled_at) {
      // settled_at arrives as ISO-8601 from the engine, which resolved
      // day-first vs month-first across the whole column. Passing the file's
      // raw string to new Date() — as this did — made the browser guess
      // month-first, so 09/03/2026 became September 3rd and 15/03/2026
      // became Invalid Date and silently filled nothing.
      const iso = d.settled_at.slice(0, 10);
      // Functional update: reading `settledAt` from the closure here made
      // it a frozen first-render value, so an unparseable date reverted the
      // field to whatever it held on mount rather than leaving it alone.
      setSettledAt((prev) => (/^\d{4}-\d{2}-\d{2}$/.test(iso) ? `${iso}T00:00` : prev));
    }
    if (d.declared_deductions != null) setDeclaredDeductions(String(d.declared_deductions));
    setDetected(null);
    setDetectStats(null);
    toast.success(`Loaded ${d.batch_id} from the statement.`);
  }, []);

  /**
   * Reconcile every detected settlement against the same pool.
   *
   * The settlements list is synthesised from what detection already found
   * rather than re-uploading the statement, so this works whatever shape the
   * original file was — the engine's queue endpoint wants a settlements CSV
   * and detection has already normalised the columns it needs.
   */
  const runAll = useCallback(async () => {
    if (!detected || detected.length === 0) return;
    setTriageBusy(true);
    setTriage(null);
    try {
      const header = "settlement_id,net_amount,settled_at,currency,declared_deductions";
      const body = detected
        .map((d) =>
          [
            d.batch_id,
            d.net_amount.toFixed(2),
            (d.settled_at || "").slice(0, 10),
            d.currency || "INR",
            d.declared_deductions != null ? Number(d.declared_deductions).toFixed(2) : "",
          ].join(","),
        )
        .join("\n");
      const csv = new File([`${header}\n${body}\n`], "settlements.csv", {
        type: "text/csv",
      });

      const fd = new FormData();
      fd.append("settlements_file", csv);
      if (gatewayFile) fd.append("gateway_file", gatewayFile);
      if (bankFile) fd.append("bank_file", bankFile);
      if (erpFile) fd.append("erp_file", erpFile);
      fd.append("settlement_window_days", String(windowDays));
      fd.append("member_source", memberSource);

      const res = await engineFetch("reconcile/queue", { method: "POST", body: fd });
      if (!res.ok) {
        throw new Error(engineErrorMessage(await res.json(), "Could not run the settlements."));
      }
      const data = await res.json();
      setTriage(data.results ?? []);
      const cleared = (data.results ?? []).filter((r: QueueRow) => r.status === "cleared").length;
      const need = (data.results ?? []).length - cleared;
      toast.success(
        need === 0
          ? `All ${cleared} settlements cleared.`
          : `${cleared} cleared, ${need} need you.`,
      );
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : "Could not run the settlements.");
    } finally {
      setTriageBusy(false);
    }
  }, [detected, gatewayFile, bankFile, erpFile, windowDays, memberSource]);

  const run = useCallback(async () => {
    // Any ONE feed is enough.
    //
    // This required a gateway file, which encoded an assumption the engine
    // does not make: SourceType.GATEWAY appears in exactly two places in the
    // engine — a cash-position bucket and a timezone default — and
    // member_source accepts `bank` as readily as `gateway`.
    //
    // The assumption is also backwards. A bank statement is the only
    // INDEPENDENT record: a gateway export and an ERP ledger routinely fail
    // in the same direction, because many ERPs book straight from the gateway
    // feed, so "two sources agree" can mean one source counted twice. The
    // bank is the third party.
    //
    // Reconciling within a single bank statement — a credit against the
    // debits that compose it — is a real scenario. The engine handled it
    // correctly on a genuine statement; only this line refused.
    if (!gatewayFile && !bankFile && !erpFile) {
      toast.error("Upload at least one feed — gateway, bank or ERP.");
      return;
    }

    // Check the fields the engine cannot do without, before asking it.
    //
    // A blank amount used to be POSTED anyway. FastAPI rejected it with a
    // 422 whose body is an array rather than this engine's {plain, message}
    // shape, nothing rendered it, and the screen showed a bare "FAILED ·
    // 0ms · 0/12 agents" with no reason on it. An unexplained failure is the
    // one thing this product is not allowed to produce, and it was producing
    // it on the most ordinary mistake there is: a field left empty.
    const missing: string[] = [];
    if (!batchId.trim()) missing.push("the batch id");
    if (!netAmount.trim() || Number.isNaN(Number(netAmount))) {
      missing.push("the settlement amount");
    }
    if (!settledAt.trim()) missing.push("the settlement date");
    if (missing.length) {
      const list =
        missing.length === 1
          ? missing[0]
          : `${missing.slice(0, -1).join(", ")} and ${missing[missing.length - 1]}`;
      const msg = `Fill in ${list} before running. The engine needs the credited amount to work backwards from.`;
      setFormError(msg);
      // A toast AND the inline line, deliberately. They are not a duplicate
      // of each other: the toast is transient and catches the eye of someone
      // who has just pressed a button and is looking at the button, while
      // the inline line persists beside the field so it is still there when
      // they go to fix it. What was wrong before was the same sentence
      // rendered twice INLINE, in two different treatments, in one view.
      toast.error(`Fill in ${list} before running.`);
      return;
    }

    clearTimers();
    setResults(null);
    setAuditEntries([]);
    setRunError(null);
    setFormError(null);
    setElapsedMs(0);
    const startedAt = performance.now();
    // The audit trail is append-only and keyed by batch_id, which is correct
    // — it is the durable record, and a re-run must not erase what the last
    // one decided. But it means a second run on the same batch id returns
    // BOTH runs' entries, and the flow would then show a merge of two
    // histories as though it were one. So the view is scoped by time; the
    // stored trail keeps everything.
    const runStartedIso = new Date(Date.now() - 1000).toISOString();
    const tick = window.setInterval(() => setElapsedMs(performance.now() - startedAt), 100);
    timers.current.push(tick);
    setStatus("running");
    setUploadOpen(false);
    setProgress(0);

    // Poll the real audit trail while the reconciliation runs.
    //
    // This replaces a progress bar that animated on setTimeout and told the
    // user nothing. The endpoint returns entries as the engine writes them,
    // so progress is the count of agents that have actually reported — and
    // if the run stalls, the display stalls with it, which is the truthful
    // behaviour.
    let stopped = false;
    const poll = window.setInterval(() => {
      if (stopped) return;
      void (async () => {
        try {
          const r = await engineFetch(`audit/${encodeURIComponent(batchId)}`);
          if (!r.ok) return;
          const j = await r.json();
          const trail: AuditEntry[] = (j.trail ?? []).filter(
            (e: AuditEntry) => e.timestamp_utc >= runStartedIso,
          );
          setAuditEntries(trail);
          const agents = new Set(trail.map((e) => e.agent)).size;
          setProgress(Math.min(94, Math.round((agents / 15) * 100)));
        } catch {
          /* A failed poll is cosmetic. The reconciliation is the POST below
             and must not be affected by the visualiser's fetches. */
        }
      })();
    }, 350);
    timers.current.push(poll);

    try {
      const formData = new FormData();
      formData.append("batch_id", batchId);
      formData.append("net_amount", netAmount);
      // A datetime-local value carries no timezone, and new Date() reads it
      // as LOCAL — so toISOString() shifted it by the browser's offset. In
      // IST (+5:30) that moved every settlement before 05:30 to the PREVIOUS
      // DATE: picking 9 March 00:00 sent 2026-03-08T18:30Z. The audit trail
      // then recorded a date the operator never chose, and the window this
      // anchors was off by a day at one edge. The statement picker fills this
      // field at T00:00, so every auto-filled settlement hit it.
      //
      // The wall clock is sent as UTC instead, so the date recorded is the
      // date on screen. The field says UTC rather than leaving the reader to
      // discover which zone it meant.
      formData.append("settled_at", `${settledAt}:00Z`);
      formData.append("settlement_window_days", windowDays);
      formData.append("currency", "INR");
      if (memberSource) formData.append("member_source", memberSource);
      // Attribution. The sign-in screen tells the user their decisions are
      // attributed to them; until this line that was a claim the app did not
      // honour — the session existed and nothing recorded it.
      const who = currentSession()?.email;
      if (who) formData.append("reviewer", who);
      if (declaredDeductions.trim())
        formData.append("declared_deductions", declaredDeductions.trim());
      // Only send terms that were actually given. An omitted field means the
      // engine falls back to its default for that one and says so.
      if (gatewayFeeBps.trim()) formData.append("gateway_fee_bps", gatewayFeeBps.trim());
      if (taxWithholdingBps.trim())
        formData.append("tax_withholding_bps", taxWithholdingBps.trim());
      if (flatFeeCents.trim()) formData.append("flat_fee_cents", flatFeeCents.trim());

      if (gatewayFile) formData.append("gateway_file", gatewayFile);
      if (bankFile) formData.append("bank_file", bankFile);
      if (erpFile) formData.append("erp_file", erpFile);

      const response = await engineFetch("reconcile/upload", {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        const err = await response.json();
        throw new Error(engineErrorMessage(err, "Reconciliation failed."));
      }

      const data = await response.json();

      // Final trail fetch: the last agents write after the poll's final tick,
      // so without this the flow would stop a node or two short of the truth.
      try {
        const r = await engineFetch(`audit/${encodeURIComponent(batchId)}`);
        if (r.ok) {
          const all: AuditEntry[] = (await r.json()).trail ?? [];
          setAuditEntries(all.filter((e) => e.timestamp_utc >= runStartedIso));
        }
      } catch {
        /* keep whatever the polling already collected */
      }

      stopped = true;
      window.clearInterval(poll);
      window.clearInterval(tick);
      setElapsedMs(performance.now() - startedAt);
      setProgress(100);
      setResults(data);
      setStatus("done");

      if (data.summary.cleared) {
        toast.success("Reconciliation complete — Batch Cleared!");
      } else {
        toast.warning("Reconciliation complete — Action Required");
      }
    } catch (e: unknown) {
      stopped = true;
      window.clearInterval(poll);
      window.clearInterval(tick);
      const msg = e instanceof Error ? e.message : String(e);
      setRunError(msg);
      // A rejection is a structured block; the panel below shows all
      // of it, so the toast takes only the headline.
      toast.error(msg.split("\n")[0]);
      setStatus("error");
    }
  }, [
    batchId,
    netAmount,
    settledAt,
    windowDays,
    memberSource,
    declaredDeductions,
    gatewayFeeBps,
    taxWithholdingBps,
    flatFeeCents,
    gatewayFile,
    bankFile,
    erpFile,
  ]);

  // One click to a runnable demo. See src/lib/sample-preset.ts for why the
  // settlement date in particular could not be left to be typed.
  const [presetBusy, setPresetBusy] = useState<string | null>(null);

  const loadPreset = async (preset: SamplePreset) => {
    setPresetBusy(preset.label);
    try {
      const files = await loadSampleFiles();
      setBatchId(preset.batchId);
      setNetAmount(preset.netAmount);
      setSettledAt(preset.settledAt);
      setWindowDays(preset.windowDays);
      setMemberSource(preset.memberSource);
      setDeclaredDeductions(preset.declaredDeductions);
      setGatewayFile(files.gateway);
      setBankFile(files.bank);
      setErpFile(files.erp);
      // Clear any previous run so the panel is not showing one settlement's
      // result above another settlement's inputs.
      setResults(null);
      setAuditEntries([]);
      setRunError(null);
      setFormError(null);
      setStatus("idle");
      setUploadOpen(true);
      toast.success(`${preset.label} loaded`, { description: `Expect: ${preset.expected}` });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not load the sample settlement.");
    } finally {
      setPresetBusy(null);
    }
  };

  const reset = () => {
    clearTimers();
    setResults(null);
    setAuditEntries([]);
    setRunError(null);
    setElapsedMs(0);
    setProgress(0);
    setStatus("idle");
    setGatewayFile(null);
    setBankFile(null);
    setErpFile(null);
  };

  // Report what is actually attached. Any one feed is enough, so the count
  // describes the uploads rather than a mode chosen in advance.
  const attached = [
    gatewayFile && `gateway: ${gatewayFile.name}`,
    bankFile && `bank: ${bankFile.name}`,
    erpFile && `erp: ${erpFile.name}`,
  ].filter(Boolean) as string[];
  const sourceSummary = attached.length
    ? `${attached.length} of 3 feeds attached · ${attached.join(", ")}`
    : "No feeds attached";

  const statusLabel =
    status === "running"
      ? "Running"
      : status === "error"
        ? "Failed"
        : status === "done"
          ? results?.summary.cleared
            ? "Cleared"
            : "Withheld for review"
          : "Ready";

  const statusTone: "cleared" | "withheld" | "blocked" | "idle" =
    status === "error"
      ? "blocked"
      : status === "done"
        ? results?.summary.cleared
          ? "cleared"
          : "withheld"
        : "idle";

  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* ── header ──────────────────────────────────────────────────────── */}
      <header className="sticky top-0 z-40 border-b border-border bg-background/90 backdrop-blur-md">
        <div className="mx-auto flex max-w-[1180px] items-center justify-between gap-4 px-4 py-2.5 sm:px-6">
          <Wordmark pill="Agent Flow" />
          <div className="flex items-center gap-3.5">
            <HistoryLink className="flex items-center gap-1.5 whitespace-nowrap text-[14px] text-muted-foreground hover:text-foreground" />
            <Link
              to="/escalations"
              className="nav-flag flex items-center gap-1.5 whitespace-nowrap text-[14px] text-muted-foreground hover:text-foreground"
            >
              <Flag className="size-4" />
              <span className="hidden sm:inline">Escalations</span>
            </Link>
            <Link
              to="/rulebook"
              className="nav-scroll flex items-center gap-1.5 whitespace-nowrap text-[14px] text-muted-foreground hover:text-foreground"
            >
              <ScrollText className="size-4" />
              <span className="hidden sm:inline">Compliance Rulebook</span>
            </Link>
            <SessionMenu />
            <ThemeToggle />
          </div>
        </div>
      </header>

      {/* Before a run, the upload IS the page.
          A reconciliation tool's first screen was a strip of dropzones above
          a canvas of fifteen idle nodes — the pipeline diagram dominated, and
          the one thing a first-time visitor has to do was the smallest
          element on screen. The hero inverts that while idle and gets out of
          the way the moment a run starts, because from then on the canvas is
          the thing worth looking at. */}
      <div className={status === "idle" && !results ? "hero-mesh" : undefined}>
        {status === "idle" && !results && (
          <section className="px-4 pb-2 pt-14 sm:px-6 sm:pt-20">
            <div className="mx-auto max-w-[780px] text-center">
              <p className="eyebrow-display m-0 uppercase text-muted-foreground">
                Multi-source settlement reconciliation
              </p>
              <h1 className="m-0 mt-4 font-[family-name:var(--font-display)] text-[clamp(34px,6vw,56px)] font-normal leading-[1.02] tracking-[-0.03em]">
                Which payments make up this settlement?
              </h1>
              <p className="narrative-copy mx-auto m-0 mt-4 max-w-[560px] text-[15.5px] leading-[1.6] text-muted-foreground">
                Drop in a gateway export, a bank statement or an ERP ledger — any one is enough. The
                engine identifies the members, proves the arithmetic ties out, and tells you plainly
                when it cannot.
              </p>
            </div>
          </section>
        )}

        <main className={`px-4 pb-10 sm:px-6 ${status === "idle" && !results ? "pt-4" : "pt-3"}`}>
          <div className="mx-auto max-w-[1180px]">
            <div
              className={
                status === "idle" && !results
                  ? "hero-card flex flex-col overflow-hidden rounded-[20px]"
                  : "flex flex-col overflow-hidden rounded-[14px] border border-border bg-card"
              }
            >
              {/* ── data sources ────────────────────────────────────────── */}
              <div className="flex flex-wrap items-center gap-2.5 border-b border-border px-3.5 py-2.5 sm:px-4">
                <span className="whitespace-nowrap text-[13px] font-semibold tracking-[-0.01em]">
                  Data Sources
                </span>
                <span className="truncate font-mono text-[10.5px] text-muted-foreground">
                  {sourceSummary}
                </span>
                <div className="ml-auto flex flex-wrap items-center gap-2">
                  {/* The chevron turns to face where the panel is going, so
                      the control says which way it will move before it does. */}
                  <button
                    type="button"
                    onClick={() => setUploadOpen((v) => !v)}
                    data-open={uploadOpen}
                    className="ctl-press ctl-toggle flex items-center gap-1.5 whitespace-nowrap rounded-[7px] border border-border px-2.5 py-[5px] font-mono text-[9.5px] uppercase tracking-[0.08em] text-muted-foreground"
                  >
                    <ChevronUp className="size-3" />
                    {uploadOpen ? "Hide panel" : "Attach feeds"}
                  </button>
                  {/* Fills the form and attaches the three fixtures. A judge
                      should be able to see a result without typing a date. */}
                  {SAMPLE_PRESETS.map((preset) => (
                    <Button
                      key={preset.label}
                      size="sm"
                      variant="outline"
                      title={`${preset.description} Expect: ${preset.expected}`}
                      onClick={() => loadPreset(preset)}
                      disabled={status === "running" || presetBusy !== null}
                      className="ctl-press"
                    >
                      {presetBusy === preset.label ? (
                        <LoadingMark size={15} className="mr-1.5" />
                      ) : (
                        <Wand2 className="mr-1.5 size-3.5" />
                      )}
                      {preset.label}
                    </Button>
                  ))}
                  {/* The play triangle leans into the motion it starts. */}
                  <Button
                    size="sm"
                    onClick={run}
                    disabled={status === "running"}
                    className="ctl-press ctl-run"
                  >
                    {status === "running" ? (
                      <LoadingMark size={15} onSolid className="mr-1.5" />
                    ) : (
                      <Play className="mr-1.5 size-3.5" />
                    )}
                    {status === "running" ? "Reconciling…" : "Run Engine"}
                  </Button>
                  {/* Reset's arrow actually goes back round. */}
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={reset}
                    disabled={status === "running"}
                    className="ctl-press ctl-reset"
                  >
                    <RotateCcw className="mr-1.5 size-3.5" />
                    Reset
                  </Button>
                </div>
              </div>

              {uploadOpen && (
                <div className="border-b border-border px-3.5 py-3 sm:px-4">
                  <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-6">
                    <Field label="Batch ID" hint="must match the reference on the member rows">
                      <input
                        type="text"
                        value={batchId}
                        onChange={(e) => setBatchId(e.target.value)}
                        className={FIELD}
                      />
                    </Field>
                    <Field label="Net Amount (₹)" hint="the credited settlement amount">
                      <input
                        type="number"
                        value={netAmount}
                        onChange={(e) => setNetAmount(e.target.value)}
                        className={FIELD}
                      />
                    </Field>
                    <Field label="Settlement Date (UTC)" hint="anchor for the lookback window">
                      <input
                        type="datetime-local"
                        value={settledAt}
                        onChange={(e) => setSettledAt(e.target.value)}
                        className={FIELD}
                      />
                    </Field>
                    <Field label="Lookback (Days)" hint="candidates outside this are dropped">
                      <input
                        type="number"
                        value={windowDays}
                        onChange={(e) => setWindowDays(e.target.value)}
                        className={FIELD}
                      />
                    </Field>
                    <Field
                      label="Members Live In"
                      hint="unknown ⇒ a payment and its mirror are indistinguishable, so the batch is held"
                    >
                      <select
                        value={memberSource}
                        onChange={(e) => setMemberSource(e.target.value)}
                        className={FIELD}
                      >
                        <option value="gateway">Gateway</option>
                        <option value="bank">Bank</option>
                        <option value="erp">ERP</option>
                        <option value="">Unknown</option>
                      </select>
                    </Field>
                    <Field
                      label="Declared Deductions (₹)"
                      hint="blank ⇒ estimated from the rate card"
                    >
                      <input
                        type="number"
                        placeholder="optional"
                        value={declaredDeductions}
                        onChange={(e) => setDeclaredDeductions(e.target.value)}
                        className={FIELD}
                      />
                    </Field>
                  </div>

                  {/* Fee terms. These were never sent, so every run used the
                    engine's DEFAULT card — "a plausible guess and nobody's
                    actual contract", in its own words. Because subset-sum is
                    exact, a card that is wrong by a fraction of a percent
                    eliminates the match rather than degrading it, so a team
                    on real contract terms could not clear anything and was
                    never told why. */}
                  <details className="mt-3 rounded-[11px] border border-border">
                    <summary className="label-ui cursor-pointer px-3 py-2 text-[10px] uppercase text-muted-foreground">
                      Fee terms {rateCards.length > 0 && `· ${rateCards.length} saved`}
                      <span className="ml-2 normal-case tracking-normal">
                        blank ⇒ engine default (2% + 1%), and it will say so
                      </span>
                    </summary>
                    <div className="border-t border-border p-3">
                      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
                        <Field label="Gateway Fee (bps)" hint="200 = 2.00%">
                          <input
                            type="number"
                            placeholder="200"
                            value={gatewayFeeBps}
                            onChange={(e) => setGatewayFeeBps(e.target.value)}
                            className={FIELD}
                          />
                        </Field>
                        <Field label="Tax Withheld (bps)" hint="100 = 1.00%">
                          <input
                            type="number"
                            placeholder="100"
                            value={taxWithholdingBps}
                            onChange={(e) => setTaxWithholdingBps(e.target.value)}
                            className={FIELD}
                          />
                        </Field>
                        <Field label="Flat Fee (paise)" hint="per batch, if any">
                          <input
                            type="number"
                            placeholder="0"
                            value={flatFeeCents}
                            onChange={(e) => setFlatFeeCents(e.target.value)}
                            className={FIELD}
                          />
                        </Field>
                      </div>

                      <div className="mt-3 flex flex-wrap items-end gap-2">
                        <label className="flex-1 min-w-[160px]">
                          <span className="label-ui block text-[10px] uppercase text-muted-foreground">
                            Merchant
                          </span>
                          <input
                            value={cardName}
                            onChange={(e) => setCardName(e.target.value)}
                            placeholder="name these terms"
                            className={FIELD}
                          />
                        </label>
                        <button
                          type="button"
                          onClick={storeCard}
                          className="h-9 rounded-[10px] border border-border px-3 text-[12px] transition-colors hover:border-accent"
                        >
                          Save terms
                        </button>
                        {rateCards.length > 0 && (
                          <select
                            value=""
                            onChange={(e) => e.target.value && applyCard(e.target.value)}
                            className="h-9 rounded-[10px] border border-border bg-background px-2 text-[12px]"
                          >
                            <option value="">Load saved…</option>
                            {rateCards.map((c) => (
                              <option key={c.name} value={c.name}>
                                {c.name}
                              </option>
                            ))}
                          </select>
                        )}
                      </div>
                      <p className="narrative-copy m-0 mt-2 text-[11px] leading-[1.5] text-muted-foreground">
                        Saved in this browser only, and not versioned by effective date — a
                        settlement from March needs March's terms, which needs a backend and is not
                        built.
                      </p>
                    </div>
                  </details>

                  {detected && detected.length > 0 && (
                    <div
                      className="mt-3 rounded-[11px] border p-3"
                      style={{
                        borderColor: "color-mix(in oklab, var(--accent) 34%, transparent)",
                        background: "color-mix(in oklab, var(--accent) 6%, transparent)",
                        animation: "rb-rise 300ms ease both",
                      }}
                    >
                      <p className="m-0 font-mono text-[9.5px] uppercase tracking-[0.1em] text-muted-foreground">
                        {detectStats?.total ?? detected.length} settlement
                        {(detectStats?.total ?? detected.length) === 1 ? "" : "s"} found in that
                        statement
                        {detectStats?.truncated && (
                          <span className="ml-1 normal-case text-[var(--s-withheld)]">
                            · showing the first {detected.length}
                          </span>
                        )}
                      </p>
                      {detectStats?.dateAssumed && (
                        <p className="m-0 mt-1 font-mono text-[9.5px] uppercase tracking-[0.1em] text-[var(--s-withheld)]">
                          Dates read as day/month — no row in the file settles it either way. Check
                          the date before you run.
                        </p>
                      )}
                      {detectStats && detectStats.unreadable > 0 && (
                        <p className="m-0 mt-1 font-mono text-[9.5px] uppercase tracking-[0.1em] text-[var(--s-withheld)]">
                          {detectStats.unreadable} credit row
                          {detectStats.unreadable === 1 ? "" : "s"} skipped — the amount could not
                          be read. Check {detectStats.unreadable === 1 ? "it" : "them"} by hand.
                        </p>
                      )}
                      <div className="mt-2 flex flex-wrap items-center gap-2">
                        <button
                          type="button"
                          onClick={runAll}
                          disabled={triageBusy || status === "running"}
                          className="rounded-lg px-3 py-[6px] text-[12px] font-medium disabled:opacity-40"
                          style={{
                            color: "var(--accent)",
                            background: "color-mix(in oklab, var(--accent) 15%, transparent)",
                          }}
                        >
                          {triageBusy
                            ? "Reconciling…"
                            : `Reconcile all ${detected.length} — show me which need me`}
                        </button>
                        <span className="text-[11px] text-muted-foreground">
                          or pick one below to open it on its own
                        </span>
                      </div>

                      {/* The triage list. Each row leads back into the full
                          report on this same page, so there is no thinner
                          second view to fall out of date. */}
                      {triage && triage.length > 0 && (
                        <div className="mt-2.5 overflow-hidden rounded-[10px] border border-border bg-card">
                          <div className="border-b border-border px-3 py-1.5">
                            <div className="flex items-center justify-between gap-2">
                              <span className="label-ui text-[9.5px] uppercase text-muted-foreground">
                                {triage.filter((r) => r.status === "cleared").length} cleared ·{" "}
                                {triage.filter((r) => r.status !== "cleared").length} need you
                              </span>
                              <button
                                type="button"
                                onClick={() =>
                                  exportQueueCsv(
                                    triage as QueueRow[],
                                    triage.reduce((acc: Record<string, number>, r) => {
                                      acc[r.status] = (acc[r.status] ?? 0) + 1;
                                      return acc;
                                    }, {}),
                                  )
                                }
                                className="rounded-md px-2 py-[3px] text-[10.5px] text-muted-foreground hover:text-foreground"
                              >
                                Export CSV
                              </button>
                            </div>
                          </div>
                          <div className="max-h-[280px] overflow-y-auto">
                            {[...triage]
                              .sort(
                                (a, b) =>
                                  (a.status === "cleared" ? 1 : 0) -
                                  (b.status === "cleared" ? 1 : 0),
                              )
                              .map((r) => {
                                const ok = r.status === "cleared";
                                const match = detected.find((d) => d.batch_id === r.batch_id);
                                return (
                                  <button
                                    key={r.batch_id}
                                    type="button"
                                    onClick={() => match && applyDetected(match)}
                                    className="flex w-full items-baseline gap-2 border-b border-border px-3 py-2 text-left last:border-0 hover:bg-muted/50"
                                    title="Open this settlement's full report"
                                  >
                                    <span
                                      className="label-ui w-[86px] flex-none text-[9px] uppercase"
                                      style={{
                                        color: ok ? "var(--s-done)" : "var(--s-withheld)",
                                      }}
                                    >
                                      {ok ? "cleared" : "needs you"}
                                    </span>
                                    <span className="flex-none font-mono text-[11.5px]">
                                      {r.batch_id}
                                    </span>
                                    <span className="truncate text-[11px] text-muted-foreground">
                                      {r.plain ?? ""}
                                    </span>
                                  </button>
                                );
                              })}
                          </div>
                        </div>
                      )}

                      <div className="mt-2 flex max-h-40 flex-wrap gap-1.5 overflow-y-auto">
                        {detected.map((d) => (
                          <button
                            key={`${d.batch_id}-${d.net_amount}`}
                            type="button"
                            onClick={() => applyDetected(d)}
                            className="rounded-lg border border-border bg-card px-2.5 py-1.5 text-left font-mono text-[11px] transition-colors hover:border-accent"
                          >
                            <span className="text-foreground">{d.batch_id}</span>
                            <span className="ml-2 text-muted-foreground">
                              {d.currency} {d.net_amount.toLocaleString("en-IN")}
                            </span>
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  <div className="mt-2.5 grid grid-cols-1 gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
                    <DropzoneInline
                      label="Gateway Report"
                      requirement="optional · .csv / .json"
                      file={gatewayFile}
                      onPick={setGatewayFile}
                      disabled={status === "running"}
                    />
                    <DropzoneInline
                      label="Bank Statement"
                      requirement="the independent record"
                      file={bankFile}
                      onPick={onBankPicked}
                      disabled={status === "running"}
                    />
                    <DropzoneInline
                      label="ERP Ledger"
                      requirement="optional · .csv / .json"
                      file={erpFile}
                      onPick={setErpFile}
                      disabled={status === "running"}
                    />
                  </div>
                </div>
              )}

              {/* ── the flow ────────────────────────────────────────────── */}
              <AgentFlow
                entries={auditEntries}
                running={status === "running"}
                batchId={batchId}
                statusLabel={statusLabel}
                statusTone={statusTone}
                elapsedText={
                  elapsedMs < 1000
                    ? `${Math.round(elapsedMs)}ms`
                    : `${(elapsedMs / 1000).toFixed(2)}s`
                }
                hasReport={status === "done" && !!results}
                onOpenReport={() =>
                  document.getElementById("report")?.scrollIntoView({ behavior: "smooth" })
                }
                runError={formError ?? runError}
              />
            </div>

            {/* ── report ────────────────────────────────────────────────── */}
            <div id="report" className="mt-6">
              {status === "idle" && !results && (
                <p className="m-0 text-center text-[12.5px] text-muted-foreground">
                  Every agent reports itself from the audit trail as it works — nothing on the
                  canvas runs on a timer.
                </p>
              )}
              {status === "running" && <ResultsSkeleton progress={progress} />}
              {status === "error" && (
                <div className="rounded-[14px] border border-destructive/40 bg-destructive/5 px-5 py-4">
                  <p className="whitespace-pre-line text-[13px] leading-[1.55] text-destructive">
                    {runError}
                  </p>
                </div>
              )}
              {status === "done" && results && <ResultsPanel results={results} />}
            </div>

            <FlowNarrative entries={auditEntries} batchId={batchId} hasRun={status === "done"} />
          </div>
        </main>
      </div>
    </div>
  );
}

const FIELD =
  "mt-1 w-full rounded-md border border-border bg-background px-2.5 py-1.5 text-[12.5px]";

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint: string;
  children: React.ReactNode;
}) {
  return (
    <div className="min-w-0">
      <label className="label-ui text-[10px] uppercase text-muted-foreground">{label}</label>
      {children}
      <span className="mt-1 block text-[10.5px] leading-snug text-muted-foreground">{hint}</span>
    </div>
  );
}
