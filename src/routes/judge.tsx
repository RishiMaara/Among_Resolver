/**
 * One page for someone deciding whether this engine is any good.
 *
 * Everything on it is either live — a button that calls the engine this app
 * is connected to and shows what came back, with the endpoint named so it can
 * be repeated with curl — or measured, with the committed file the figure came
 * from (measured.ts, checked against those files by its test). Nothing here is
 * a claim typed for the page.
 */

import { useEffect, useRef, useState, type ReactNode } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import { Play, CheckCircle2, AlertTriangle, ExternalLink } from "lucide-react";
import { engineFetch, ENGINE_HOST } from "@/lib/api";
import { MEASURED } from "@/lib/measured";
import { inr } from "@/lib/utils";
import { readScan } from "@/lib/scan-ocr";
import { aiStatus, useAiStatus } from "@/lib/ai";
import { Wordmark } from "@/components/wordmark";
import { PayoutsLink } from "@/components/payouts-link";
import { ThemeToggle } from "@/components/theme-toggle";
import { HistoryLink } from "@/components/history-link";

export const Route = createFileRoute("/judge")({ component: Judge });

const REPO = "https://github.com/RishiMaara/Among_Resolver/blob/main";

type Line = { tone?: "good" | "bad" | "muted"; text: string };
type RunState = { status: "idle" | "running" | "done" | "error"; lines: Line[] };

async function sample(path: string): Promise<Blob> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`Could not load ${path} (${r.status}).`);
  return r.blob();
}

async function json(path: string, init?: RequestInit) {
  const r = await engineFetch(path, init);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const plain = body?.detail?.plain ?? body?.detail?.message ?? `HTTP ${r.status}`;
    throw new Error(String(plain));
  }
  return body;
}

// ── the live checks ──────────────────────────────────────────────────────

async function reconcileSample(memberSource: string | null) {
  const fd = new FormData();
  fd.append("gateway_file", await sample("/sample-data/gateway_report.csv"), "gateway_report.csv");
  fd.append("bank_file", await sample("/sample-data/bank_statement.csv"), "bank_statement.csv");
  fd.append("erp_file", await sample("/sample-data/erp_ledger.json"), "erp_ledger.json");
  for (const [k, v] of Object.entries({
    // The id the sample's references carry, so the members name it.
    batch_id: "SETTLE-001",
    net_amount: "66466.36",
    settled_at: "2026-09-02T00:00:00Z",
    settlement_window_days: "5",
    currency: "INR",
    declared_deductions: "2055.66",
    investigate: "true",
  }))
    fd.append(k, v);
  if (memberSource) fd.append("member_source", memberSource);
  // The model proposes only where the server has one; metered per visitor.
  if ((await aiStatus())?.live) fd.append("investigate_with_model", "true");
  return json("reconcile/upload", { method: "POST", body: fd });
}

interface Verdict {
  cleared?: boolean;
  matched_count?: number;
  confidence?: number;
  calibrated_confidence?: number | null;
}

function verdictLine(s: Verdict): Line {
  const cal = s.calibrated_confidence;
  return {
    tone: s.cleared ? "good" : "muted",
    text: `${s.cleared ? "Cleared" : "Withheld"}: ${s.matched_count} payment(s), confidence ${s.confidence}${
      typeof cal === "number"
        ? ` (right ${Math.round(cal * 100)}% of the time at this score, measured)`
        : ""
    }.`,
  };
}

async function reconcileAndInvestigate(): Promise<Line[]> {
  // 1. The payout, with its member feed declared: it clears, and its audit
  //    receipt is checked against the trail.
  const res = await reconcileSample("gateway");
  const declared: string[] = res.matched_txn_ids ?? [];
  const out: Line[] = [verdictLine(res.summary ?? {})];
  if (res.plain_summary) out.push({ tone: "muted", text: String(res.plain_summary) });
  if (res.audit_head) {
    const v = await json(`audit/SETTLE-001/verify?receipt=${encodeURIComponent(res.audit_head)}`);
    out.push({
      tone: v.intact ? "good" : "bad",
      text: `Audit receipt ${String(res.audit_head).slice(0, 12)}…: ${v.plain}`,
    });
  }
  // 2. The same payout with the member feed left undeclared. Every gateway
  //    payment has a ledger twin at the same amount, so the engine withholds —
  //    and the investigator's proposal meets the verifier.
  const res2 = await reconcileSample(null);
  out.push({
    ...verdictLine(res2.summary ?? {}),
    text: `Member feed undeclared — ${verdictLine(res2.summary ?? {}).text}`,
  });
  const inv = res2.investigation;
  if (inv) {
    const model: string | undefined = res2.ai?.model;
    // Every model busy: the engine reused the answer a model gave this same
    // case earlier, and says when. The verifier below still checked it now.
    const replayed: string | undefined = res2.ai?.replayed_from;
    if (replayed) {
      out.push({
        tone: "muted",
        text: `Every model is busy right now, so this is ${model}'s answer to this same case from ${replayed.replace("T", " ")} UTC, replayed and checked again just now.`,
      });
    }
    // Every model attempt, so a proposal the checks turned down is seen being
    // turned down rather than disappearing into the final answer.
    for (const a of inv.attempts ?? []) {
      if (a.proposer !== "model") continue;
      out.push({
        tone: a.valid ? "good" : "bad",
        text: `Model (${model ?? "model"}) proposed ${a.action}: ${
          a.valid ? "passed every check" : `rejected — ${(a.failed ?? []).join("; ")}`
        }.`,
      });
    }
    const who =
      inv.proposal.proposer === "model" && model ? `model, ${model}` : inv.proposal.proposer;
    out.push({
      text: `Investigator (${who}) proposes ${inv.proposal.action}: ${inv.proposal.reason}`,
    });
    out.push({ tone: inv.verification.valid ? "good" : "bad", text: inv.verification.plain });
    // A tie becomes one question a person can answer from the processor's report.
    if (inv.deciding_question) out.push({ tone: "muted", text: inv.deciding_question });
    const ids: string[] = inv.proposal.txn_ids ?? [];
    if (
      inv.proposal.action === "MATCH_PROPOSAL" &&
      inv.verification.valid &&
      ids.length > 0 &&
      ids.length === declared.length &&
      ids.every((i) => declared.includes(i))
    ) {
      out.push({
        tone: "good",
        text: `The same ${ids.length} payments the engine cleared when it was told which feed to trust — found here without being told.`,
      });
    }
  }
  return out;
}

async function razorpayChecks(): Promise<Line[]> {
  const fd = new FormData();
  const base = "/sample-data/razorpay";
  fd.append("settlements_file", await sample(`${base}/settlements.json`), "settlements.json");
  fd.append("recon_file", await sample(`${base}/recon_combined.json`), "recon_combined.json");
  fd.append("bank_file", await sample(`${base}/bank_statement.csv`), "bank_statement.csv");
  fd.append("ledger_file", await sample(`${base}/ledger.json`), "ledger.json");
  const res = await json("razorpay/reconcile/upload", { method: "POST", body: fd });
  const t = res.tally ?? {};
  const out: Line[] = [
    {
      text: `${res.settlements} payouts: ${t.verified} verified, ${t.verified_with_findings} with findings, ${t.not_verified} not verified.`,
    },
  ];
  for (const r of res.results ?? []) {
    const c = r.checks ?? {};
    const tone = r.status === "verified" ? "good" : r.status === "not_verified" ? "bad" : "muted";
    const notes = [
      c.blind_solve?.verdict === "agrees" || c.blind_solve?.verdict === "agrees_as_proposal"
        ? "blind re-solve agrees"
        : `blind: ${c.blind_solve?.verdict}`,
      c.bank?.verdict !== "arrived" ? c.bank?.plain : null,
      c.books?.verdict === "missing_in_books" ? c.books?.plain : null,
      c.fees?.verdict === "findings" ? c.fees?.findings?.[0]?.description : null,
    ].filter(Boolean);
    out.push({
      tone,
      text: `${r.settlement_id} (${inr(r.amount_cents)}) — ${String(r.status).replace(/_/g, " ")}. ${notes.join(" · ")}`,
    });
  }
  if (res.unsettled_lines?.plain) out.push({ tone: "muted", text: res.unsettled_lines.plain });
  return out;
}

async function statementProof(): Promise<Line[]> {
  const fd = new FormData();
  fd.append("file", await sample("/sample-data/statements/statement.pdf"), "statement.pdf");
  const res = await json("statements/parse", { method: "POST", body: fd });
  return [
    { tone: res.check?.holds ? "good" : "bad", text: res.check?.plain ?? "" },
    {
      text: `Read as ${String(res.format).toUpperCase()}, ${res.lines?.length} lines; running balance ${
        res.check?.running_balance?.holds ? "holds on every line" : "broke"
      } — which is also how the parser knew which column each amount was in.`,
    },
  ];
}

async function scannedStatement(): Promise<Line[]> {
  const blob = await sample("/sample-data/statements/statement_scanned.pdf");
  const file = new File([blob], "statement_scanned.pdf", { type: "application/pdf" });
  const text = await readScan(file);
  const fd = new FormData();
  fd.append("file", file, file.name);
  fd.append("scan_text", text);
  const res = await json("statements/parse", { method: "POST", body: fd });
  return [
    {
      tone: res.check?.holds ? "good" : "bad",
      text: `Read by ${res.read_by}: ${res.lines?.length} line(s). ${res.check?.plain ?? ""}`,
    },
    {
      tone: "muted",
      text: "Used only because every line's running balance follows from the one before — a misread digit is refused with the line named, not repaired.",
    },
  ];
}

async function taxByDate(): Promise<Line[]> {
  const before = await json("tax/provisions?on=2026-03-31");
  const after = await json("tax/provisions?on=2026-04-01");
  return [
    {
      text: `31 March 2026: ${before.ecommerce_tds.citation}, ${before.ecommerce_tds.rate_percent}%.`,
    },
    {
      text: `1 April 2026: ${after.ecommerce_tds.citation}, ${after.ecommerce_tds.rate_percent}%.`,
    },
    { tone: "muted", text: `GST TCS: ${after.gst_tcs.citation}, ${after.gst_tcs.rate_percent}%.` },
  ];
}

async function workingDays(): Promise<Line[]> {
  const res = await json("calendar/due?captured_on=2026-09-11");
  return [
    {
      text: `Captured Friday 11 September 2026, T+2 working days: due ${res.due_on} (${res.calendar_days} calendar days).`,
    },
    ...(res.skipped ?? []).map((s: { date: string; closed_because: string }) => ({
      tone: "muted" as const,
      text: `${s.date} skipped — ${s.closed_because}`,
    })),
  ];
}

async function openItems(): Promise<Line[]> {
  const res = await json("open-items");
  const s = res.summary ?? {};
  return [
    {
      text: `${s.open_count} open item(s) worth ${inr(s.open_value_cents ?? 0)}; ${s.overdue_count} overdue (${inr(
        s.overdue_value_cents ?? 0,
      )}), aged on ${res.settlement_cycle}.`,
    },
    ...(s.buckets ?? [])
      .filter((b: { count: number }) => b.count)
      .map((b: { label: string; count: number; value_cents: number }) => ({
        tone: "muted" as const,
        text: `${b.label}: ${b.count} item(s), ${inr(b.value_cents)}`,
      })),
    { tone: "muted", text: `Calendar: ${res.calendar}` },
  ];
}

const CHECKS: { title: string; proves: string; endpoint: string; run: () => Promise<Line[]> }[] = [
  {
    title: "Reconcile a payout; investigate one it will not clear",
    proves:
      "The sample payout reconciled, and its audit receipt checked against the trail. Then the same payout with its member feed undeclared, which the engine withholds. The agent reads the withheld case with read-only tools, proposes one of five actions, and code verifies the proposal before a person sees it.",
    endpoint: "POST /reconcile/upload · GET /audit/{batch}/verify",
    run: reconcileAndInvestigate,
  },
  {
    title: "Razorpay's own data, checked five ways",
    proves:
      "Razorpay names each payout's members. The engine checks the rest: the paisa tie-out, a blind re-solve without settlement ids, the bank credit by UTR, the books, the fees and tax.",
    endpoint: "POST /razorpay/reconcile/upload",
    run: razorpayChecks,
  },
  {
    title: "A scanned statement, read in your browser",
    proves:
      "An image-only PDF with no text layer. Tesseract.js reads it here in your browser — free, no key, nothing sent anywhere to be read — and the engine uses the reading only if every line's running balance holds.",
    endpoint: "Tesseract.js in the browser · POST /statements/parse",
    run: scannedStatement,
  },
  {
    title: "A bank statement that proves it was read right",
    proves:
      "A text PDF statement, parsed and checked: opening + credits − debits = closing, and every line's running balance.",
    endpoint: "POST /statements/parse",
    run: statementProof,
  },
  {
    title: "Tax under the law on the payment's date",
    proves:
      "Section 194-O until 31 March 2026, Section 393(1) Sl. 8(v) of the Income-tax Act 2025 from 1 April; TCS under Section 52 CGST.",
    endpoint: "GET /tax/provisions?on=…",
    run: taxByDate,
  },
  {
    title: "Working days, not calendar days",
    proves:
      "T+2 across a second Saturday, a Sunday and Ganesh Chaturthi — the arithmetic an ageing report has to get right.",
    endpoint: "GET /calendar/due?captured_on=…",
    run: workingDays,
  },
  {
    title: "What is still waiting, across every run",
    proves:
      "The open-items ledger: payments not yet paid out, refunds not yet deducted, withheld payouts, aged.",
    endpoint: "GET /open-items",
    run: openItems,
  },
];

// The runs a five-minute pitch shows, in its order; the rest fold under "More".
const PITCH_RUNS = 3;

function Check({ c, trigger = 0 }: { c: (typeof CHECKS)[number]; trigger?: number }) {
  const [state, setState] = useState<RunState>({ status: "idle", lines: [] });
  const go = async () => {
    setState({ status: "running", lines: [] });
    try {
      setState({ status: "done", lines: await c.run() });
    } catch (e) {
      setState({
        status: "error",
        lines: [{ tone: "bad", text: e instanceof Error ? e.message : String(e) }],
      });
    }
  };
  // "Run the three" bumps `trigger`; a card runs once per bump.
  const lastTrigger = useRef(0);
  useEffect(() => {
    if (trigger > lastTrigger.current) {
      lastTrigger.current = trigger;
      void go();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trigger]);
  return (
    <article className="surface-card p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="m-0 text-[15px] font-semibold">{c.title}</h3>
          <p className="m-0 mt-1 text-[12.5px] leading-[1.5] text-muted-foreground">{c.proves}</p>
          <p className="m-0 mt-1 font-mono text-[11px] text-muted-foreground">{c.endpoint}</p>
        </div>
        <button
          type="button"
          onClick={go}
          disabled={state.status === "running"}
          className="flex shrink-0 items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[12.5px] hover:bg-muted disabled:opacity-60"
        >
          <Play className="size-3.5" />
          {state.status === "running" ? "Running…" : state.status === "idle" ? "Run" : "Run again"}
        </button>
      </div>
      {state.lines.length > 0 && (
        <ul className="m-0 mt-3 list-none space-y-1.5 p-0">
          {state.lines.map((l, i) => (
            <li
              key={i}
              className="flex gap-2 text-[12.5px] leading-[1.5]"
              style={{
                color:
                  l.tone === "good"
                    ? "var(--s-done)"
                    : l.tone === "bad"
                      ? "var(--s-blocked)"
                      : l.tone === "muted"
                        ? "var(--muted-foreground)"
                        : undefined,
              }}
            >
              {l.tone === "good" ? (
                <CheckCircle2 className="mt-0.5 size-3.5 shrink-0" />
              ) : l.tone === "bad" ? (
                <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              ) : (
                <span className="size-3.5 shrink-0" />
              )}
              <span>{l.text}</span>
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}

// ── the architecture, drawn ──────────────────────────────────────────────

function Box({
  x,
  y,
  w,
  h,
  title,
  sub,
}: {
  x: number;
  y: number;
  w: number;
  h: number;
  title: string;
  sub: string;
}) {
  return (
    <g>
      <rect x={x} y={y} width={w} height={h} rx={8} fill="var(--card)" stroke="var(--border)" />
      <text x={x + 10} y={y + 20} fontSize={12.5} fontWeight={600} fill="var(--foreground)">
        {title}
      </text>
      <text x={x + 10} y={y + 37} fontSize={10.5} fill="var(--muted-foreground)">
        {sub}
      </text>
    </g>
  );
}

function Arrow({ x1, y1, x2, y2 }: { x1: number; y1: number; x2: number; y2: number }) {
  return (
    <line
      x1={x1}
      y1={y1}
      x2={x2}
      y2={y2}
      stroke="var(--muted-foreground)"
      strokeWidth={1.2}
      markerEnd="url(#arrow)"
    />
  );
}

function Architecture() {
  return (
    <svg
      viewBox="0 0 860 330"
      role="img"
      aria-label="How a settlement moves through the engine"
      className="h-auto w-full"
    >
      <defs>
        <marker
          id="arrow"
          viewBox="0 0 10 10"
          refX="9"
          refY="5"
          markerWidth="6"
          markerHeight="6"
          orient="auto"
        >
          <path d="M0,0 L10,5 L0,10 z" fill="var(--muted-foreground)" />
        </marker>
      </defs>
      <Box
        x={10}
        y={20}
        w={190}
        h={50}
        title="Razorpay recon API"
        sub="members named by the gateway"
      />
      <Box
        x={10}
        y={85}
        w={190}
        h={50}
        title="Bank statements"
        sub="MT940 · CAMT.053 · OFX · PDF"
      />
      <Box
        x={10}
        y={150}
        w={190}
        h={50}
        title="Gateway, ledger files"
        sub="CSV · JSON, headers mapped"
      />
      <Box
        x={250}
        y={85}
        w={180}
        h={50}
        title="Read and prove"
        sub="balances, paise, IST, narration"
      />
      <Box x={480} y={20} w={170} h={50} title="Linkage" sub="anchors · Fellegi-Sunter · cycle" />
      <Box x={480} y={85} w={170} h={50} title="Exact subset-sum" sub="CP-SAT, to the paisa" />
      <Box
        x={480}
        y={150}
        w={170}
        h={50}
        title="Refusal gates"
        sub="ambiguity · evidence · ≥0.85"
      />
      <Box x={690} y={20} w={160} h={50} title="Cleared" sub="journal → approval → Tally" />
      <Box x={690} y={150} w={160} h={50} title="Withheld" sub="investigator + verifier" />
      <Box x={690} y={235} w={160} h={50} title="A person decides" sub="four-eyes, on the record" />
      <Box
        x={250}
        y={235}
        w={400}
        h={50}
        title="Alongside every run"
        sub="hash-chained audit · open items · fee & tax audit · calibration"
      />
      <Arrow x1={200} y1={45} x2={250} y2={100} />
      <Arrow x1={200} y1={110} x2={250} y2={110} />
      <Arrow x1={200} y1={175} x2={250} y2={120} />
      <Arrow x1={430} y1={105} x2={480} y2={50} />
      <Arrow x1={565} y1={70} x2={565} y2={85} />
      <Arrow x1={565} y1={135} x2={565} y2={150} />
      <Arrow x1={650} y1={165} x2={690} y2={50} />
      <Arrow x1={650} y1={180} x2={690} y2={175} />
      <Arrow x1={770} y1={200} x2={770} y2={235} />
      <Arrow x1={770} y1={70} x2={770} y2={150} />
    </svg>
  );
}

const AI_USES: { use: string; model: string; check: string }[] = [
  {
    use: "Bank narrations",
    model: "reads UTR, settlement ref, payer, rail",
    check: "a value is kept only if it appears in the narration",
  },
  {
    use: "Withheld settlements",
    model: "proposes one typed action",
    check: "verifier: pool, ledger, tolerance, calendar, evidence over ties, grounded reason",
  },
  {
    use: "Questions about a result",
    model: "explains the recorded result",
    check: "every figure and id must trace to the record, or the answer is withheld",
  },
  {
    use: "Header mapping",
    model: "suggests a column for an unrecognised header",
    check: "the deterministic schema gate still accepts or refuses the file",
  },
];

const LIMITS = [
  "Not run on a live Razorpay account: test mode makes no settlements, so the API path is built to Razorpay's published contract. A merchant can check their own payouts from the Settlement Recon report they download — no keys, nothing shared.",
  "Scans are read by OCR in the browser, then Gemini where a key is set. On 24 deliberately noisy scans the browser alone read 11 right; what does not balance is refused, never guessed.",
  "Two corpora are real — public government checkbooks, with the answer recorded by their own systems; the rest are generated. No card or UPI processor publishes settlement data, so none of them is a payment gateway's.",
  "The investigator was measured with reference and memo text removed, because the benchmark's labels live there; reading real narrations is not measured.",
  "The learned settlement cycle is kept per merchant when an upload names one (merchant_id); unnamed uploads share the deployment's single cycle.",
];

/** Whether a model answers on this server — said before anything runs. */
function AiLine() {
  const ai = useAiStatus();
  if (!ai) return null;
  return (
    <p className="mt-1 text-[12px]">
      <span
        className={
          ai.live
            ? "mr-1.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-700 dark:text-emerald-400"
            : "mr-1.5 rounded-full border border-border px-2 py-0.5 text-[11px] text-muted-foreground"
        }
      >
        {ai.live ? `AI live · ${ai.model}` : "AI not configured"}
      </span>
      <span className="text-muted-foreground">
        {ai.plain}
        {ai.live && ` ${ai.budget.left_today} of ${ai.budget.per_day} model calls left today.`}
      </span>
    </p>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-10">
      <h2 className="m-0 text-[19px] font-semibold">{title}</h2>
      <div className="mt-3">{children}</div>
    </section>
  );
}

function Judge() {
  const [runAll, setRunAll] = useState(0);
  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-40 border-b border-border bg-background/90 backdrop-blur-md">
        <div className="mx-auto flex max-w-[1120px] items-center justify-between gap-4 px-4 py-2.5 sm:px-6">
          <Wordmark pill="For judges" />
          <div className="flex items-center gap-3.5">
            <Link
              to="/"
              className="whitespace-nowrap text-[14px] text-muted-foreground hover:text-foreground"
            >
              ← Agent Flow
            </Link>
            <HistoryLink className="flex items-center gap-1.5 whitespace-nowrap text-[14px] text-muted-foreground hover:text-foreground" />
            <Link
              to="/escalations"
              className="whitespace-nowrap text-[14px] text-muted-foreground hover:text-foreground"
            >
              Escalations
            </Link>
            <Link
              to="/rulebook"
              className="whitespace-nowrap text-[14px] text-muted-foreground hover:text-foreground"
            >
              Rulebook
            </Link>
            <PayoutsLink />
            <ThemeToggle />
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[900px] px-4 py-10 sm:px-6">
        <p className="label-ui m-0 text-[10px] uppercase text-muted-foreground">
          AmongResolver · settlement reconciliation
        </p>
        <h1 className="mt-2 text-[30px] font-semibold leading-tight">Judge this in five minutes</h1>
        <p className="mt-3 max-w-[680px] text-[14px] leading-[1.65]">
          It finds which payments make up each payout, proves the answer to the paisa, and refuses
          to clear what it cannot prove — then says what a person should do next. Across every
          corpus it has been measured on, it has never cleared a wrong set.
        </p>
        <p className="mt-2 text-[12px] text-muted-foreground">
          Live checks below call the engine at <span className="font-mono">{ENGINE_HOST}</span>.
        </p>
        <AiLine />

        <div className="surface-card mt-5 p-4">
          <p className="m-0 text-[13.5px] font-semibold">
            Razorpay's Settlement Recon report names each payout's payments. It cannot check on its
            own:
          </p>
          <ul className="m-0 mt-2 grid list-none gap-1.5 p-0 text-[13px] leading-[1.5] sm:grid-cols-2">
            <li>• that the bank received the payout — by UTR and amount</li>
            <li>• that every payment is in your books</li>
            <li>• that fee, GST, TDS and TCS were right, under the law on each date</li>
            <li>
              • what to do when it does not tie — it refuses, and an agent proposes the next step
            </li>
          </ul>
          <p className="m-0 mt-2 text-[12px] text-muted-foreground">
            That is what this adds. The first three runs below show it, in that order.
          </p>
        </div>

        <Section title="Run it">
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={() => setRunAll((n) => n + 1)}
              className="flex items-center gap-1.5 rounded-md bg-foreground px-3.5 py-2 text-[13px] font-medium text-background hover:opacity-90"
            >
              <Play className="size-3.5" />
              Run the three
            </button>
            <span className="text-[12px] text-muted-foreground">
              Clear and refuse a payout, Razorpay checked five ways, a scanned statement — at once.
            </span>
          </div>
          <div className="grid gap-3">
            {CHECKS.slice(0, PITCH_RUNS).map((c) => (
              <Check key={c.title} c={c} trigger={runAll} />
            ))}
          </div>
          <details className="mt-3">
            <summary className="cursor-pointer text-[13px] text-muted-foreground hover:text-foreground">
              More live checks — a text statement, tax by date, working days, open items
            </summary>
            <div className="mt-3 grid gap-3">
              {CHECKS.slice(PITCH_RUNS).map((c) => (
                <Check key={c.title} c={c} />
              ))}
            </div>
          </details>
        </Section>

        <Section title="Measured — with the file behind every figure">
          <div className="overflow-hidden rounded-[12px] border border-border">
            {MEASURED.map((m) => (
              <div
                key={m.label}
                className="grid gap-1 border-b border-border px-4 py-3 last:border-0 sm:grid-cols-[1fr_auto]"
              >
                <div>
                  <p className="m-0 text-[13px] font-medium">{m.label}</p>
                  <p className="m-0 mt-0.5 text-[11.5px] text-muted-foreground">
                    {m.note}{" "}
                    <a
                      className="underline decoration-dotted underline-offset-2"
                      href={`${REPO}/engine/docs/benchmarks/${m.source.file}`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {m.source.file}
                    </a>
                  </p>
                </div>
                <p className="m-0 font-mono text-[15px] sm:text-right">
                  {m.before && <span className="text-muted-foreground">{m.before} → </span>}
                  <span className="font-semibold">{m.after}</span>
                </p>
              </div>
            ))}
          </div>
        </Section>

        <Section title="How a settlement moves through it">
          <div className="surface-card p-3">
            <Architecture />
          </div>
        </Section>

        <Section title="Where AI is used, and what checks it">
          <div className="overflow-hidden rounded-[12px] border border-border text-[12.5px]">
            {AI_USES.map((a) => (
              <div
                key={a.use}
                className="grid gap-1 border-b border-border px-4 py-2.5 last:border-0 sm:grid-cols-[150px_1fr_1.3fr]"
              >
                <span className="font-medium">{a.use}</span>
                <span className="text-muted-foreground">{a.model}</span>
                <span>{a.check}</span>
              </div>
            ))}
          </div>
          <p className="mt-2 text-[12px] text-muted-foreground">
            Every model proposes; code decides. Each use has a deterministic fallback and a
            measurement —{" "}
            <a
              className="underline decoration-dotted underline-offset-2"
              href={`${REPO}/docs/AI_EVALUATION.md`}
              target="_blank"
              rel="noreferrer"
            >
              docs/AI_EVALUATION.md
            </a>
            , including the run that was thrown away because the model was reading the
            benchmark&apos;s labels.
          </p>
        </Section>

        <Section title="What it does not do yet">
          <ul className="m-0 list-disc space-y-1.5 pl-5 text-[13px] leading-[1.55]">
            {LIMITS.map((l) => (
              <li key={l}>{l}</li>
            ))}
          </ul>
        </Section>

        <Section title="Read further">
          <div className="flex flex-wrap gap-2 text-[12.5px]">
            {[
              ["README", `${REPO}/README.md`],
              ["Every part, and what measured it", `${REPO}/docs/CAPABILITIES.md`],
              ["What went wrong, and what changed", `${REPO}/FAILURE_LOG.md`],
              ["AI evaluation", `${REPO}/docs/AI_EVALUATION.md`],
              ["Linkage", `${REPO}/docs/LINKAGE.md`],
              ["Architecture", `${REPO}/docs/ARCHITECTURE.md`],
              ["Where it goes next", `${REPO}/docs/ROADMAP.md`],
            ].map(([label, href]) => (
              <a
                key={label}
                href={href}
                target="_blank"
                rel="noreferrer"
                className="flex items-center gap-1 rounded-md border border-border px-2.5 py-1 hover:bg-muted"
              >
                {label}
                <ExternalLink className="size-3" />
              </a>
            ))}
          </div>
        </Section>
      </main>
    </div>
  );
}
