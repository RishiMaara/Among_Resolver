/**
 * Payouts: Razorpay's settlements checked five ways, and the money still
 * waiting across every run.
 *
 * The two belong on one page because they answer one question from two
 * sides. Razorpay's Settlement Recon API says which payments each payout
 * contained; the engine checks what that list does not prove on its own. The
 * open-items ledger holds what no payout has contained yet — payments not
 * paid out, refunds not deducted, settlements withheld — aged in working
 * days, so "late" means late by the bank's calendar and not the wall's.
 */

import { useCallback, useEffect, useState } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import { Landmark, Upload, Play, ChevronDown, Flag, ScrollText } from "lucide-react";
import { engineFetch, engineErrorMessage } from "@/lib/api";
import { cn, inr } from "@/lib/utils";
import { Wordmark } from "@/components/wordmark";
import { ThemeToggle } from "@/components/theme-toggle";
import { SessionMenu } from "@/components/session-menu";
import { HistoryLink } from "@/components/history-link";
import { LoadingMark } from "@/components/loading-mark";

export const Route = createFileRoute("/payouts")({ component: Payouts });

const NAV =
  "flex items-center gap-1.5 whitespace-nowrap text-[14px] text-muted-foreground hover:text-foreground";

// ── Razorpay ─────────────────────────────────────────────────────────────

type Check = { verdict: string; plain?: string; findings?: { description: string }[] };

interface PayoutResult {
  settlement_id: string;
  utr?: string;
  amount_cents: number;
  settled_on: string;
  members: number;
  status: "verified" | "verified_with_findings" | "not_verified" | "no_lines";
  checks: Record<string, Check>;
}

interface RazorpayRun {
  settlements: number;
  tally: { verified?: number; verified_with_findings?: number; not_verified?: number };
  results: PayoutResult[];
  unsettled_lines?: { count: number; value_cents: number; plain: string };
}

const STATUS: Record<PayoutResult["status"], { label: string; tone: string }> = {
  verified: { label: "Verified", tone: "good" },
  verified_with_findings: { label: "Verified, with findings", tone: "warn" },
  not_verified: { label: "Not verified", tone: "bad" },
  no_lines: { label: "No lines", tone: "warn" },
};

const CHECK_NAMES: [string, string][] = [
  ["tie_out", "Sums to the paisa"],
  ["blind_solve", "Blind re-solve"],
  ["bank", "Bank credit by UTR"],
  ["books", "In the books"],
  ["fees", "Fees and tax"],
];

const GOOD = new Set([
  "ties_out",
  "agrees",
  "agrees_as_proposal",
  "arrived",
  "all_booked",
  "clean",
]);
const BAD = new Set(["does_not_tie_out", "disagrees", "amount_differs", "not_found", "error"]);

function tone(v: string) {
  return GOOD.has(v) ? "good" : BAD.has(v) ? "bad" : "warn";
}

const TONE_CLASS: Record<string, string> = {
  good: "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
  warn: "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400",
  bad: "border-red-500/30 bg-red-500/10 text-red-600",
};

function checkSentence(key: string, c: Check): string {
  if (c.plain) return c.plain;
  if (key === "fees") {
    return c.verdict === "clean"
      ? "Fees, GST, TDS and TCS on every line agree with the rate card and the law on its date."
      : (c.findings?.[0]?.description ?? "The fee audit has findings.");
  }
  return c.verdict.replace(/_/g, " ");
}

const SAMPLE = "/sample-data/razorpay";

async function sampleFile(name: string): Promise<File> {
  const r = await fetch(`${SAMPLE}/${name}`);
  if (!r.ok) throw new Error(`Could not load the sample ${name} (${r.status}).`);
  return new File([await r.blob()], name);
}

function PayoutRow({ r }: { r: PayoutResult }) {
  const [open, setOpen] = useState(false);
  const st = STATUS[r.status];
  // The checks this payout actually ran: bank and books need their files.
  const ran = CHECK_NAMES.flatMap(([k, name]) => {
    const c = r.checks[k];
    return c ? [{ k, name, c }] : [];
  });
  return (
    <>
      <tr className="border-b border-border/50 align-top">
        <td className="py-2 pr-3">
          <button
            type="button"
            onClick={() => setOpen(!open)}
            className="flex items-center gap-1 text-left font-mono text-xs hover:underline"
            aria-expanded={open}
          >
            <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} />
            {r.settlement_id}
          </button>
          {r.utr && (
            <span className="block pl-[18px] text-[11px] text-muted-foreground">UTR {r.utr}</span>
          )}
        </td>
        <td className="whitespace-nowrap py-2 pr-3 text-right tabular-nums">
          {inr(r.amount_cents)}
        </td>
        <td className="py-2 pr-3 text-xs text-muted-foreground">{r.settled_on}</td>
        <td className="py-2 pr-3">
          <div className="flex flex-wrap gap-1">
            {ran.map(({ k, name, c }) => (
              <span
                key={k}
                title={checkSentence(k, c)}
                className={cn(
                  "rounded-full border px-1.5 py-0.5 text-[10.5px]",
                  TONE_CLASS[tone(c.verdict)],
                )}
              >
                {name}
              </span>
            ))}
          </div>
        </td>
        <td className="py-2">
          <span
            className={cn(
              "whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium",
              TONE_CLASS[st.tone],
            )}
          >
            {st.label}
          </span>
        </td>
      </tr>
      {open && (
        <tr className="border-b border-border/50">
          <td colSpan={5} className="pb-3 pl-[18px]">
            <ul className="m-0 list-none space-y-1 p-0 text-[12.5px]">
              <li className="text-muted-foreground">{r.members} line(s) in Razorpay's list.</li>
              {ran.map(({ k, name, c }) => (
                <li key={k}>
                  <span className="font-medium">{name}:</span> {checkSentence(k, c)}
                </li>
              ))}
            </ul>
          </td>
        </tr>
      )}
    </>
  );
}

type FileKey = "settlements_file" | "recon_file" | "bank_file" | "ledger_file";
type Files = Record<FileKey, File | null>;

function RazorpaySection() {
  const [files, setFiles] = useState<Files>({
    settlements_file: null,
    recon_file: null,
    bank_file: null,
    ledger_file: null,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [run, setRun] = useState<RazorpayRun | null>(null);

  const reconcile = async (use: Files) => {
    if (!use.settlements_file || !use.recon_file) {
      setError("Both Razorpay files are needed: the settlements list and the recon report.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const fd = new FormData();
      for (const [k, f] of Object.entries(use)) if (f) fd.append(k, f, f.name);
      const r = await engineFetch("razorpay/reconcile/upload", { method: "POST", body: fd });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(engineErrorMessage(body, `The engine refused it (${r.status}).`));
      setRun(body as RazorpayRun);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const loadSample = async () => {
    try {
      const sample = {
        settlements_file: await sampleFile("settlements.json"),
        recon_file: await sampleFile("recon_combined.json"),
        bank_file: await sampleFile("bank_statement.csv"),
        ledger_file: await sampleFile("ledger.json"),
      };
      setFiles(sample);
      await reconcile(sample);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const inputs: [FileKey, string, string][] = [
    ["settlements_file", "Settlements", "JSON from GET /v1/settlements"],
    ["recon_file", "Recon report", "JSON from GET /v1/settlements/recon/combined"],
    ["bank_file", "Bank statement", "optional — CSV, MT940, CAMT.053, OFX or PDF"],
    ["ledger_file", "Ledger", "optional — your books, any format the main upload reads"],
  ];

  return (
    <section className="mt-10">
      <h2 className="m-0 text-[19px] font-semibold">Razorpay payouts, checked five ways</h2>
      <p className="mt-2 max-w-[680px] text-[13.5px] leading-[1.6] text-muted-foreground">
        Razorpay names each payout's payments. The engine checks what that list does not prove: that
        the lines sum to the payout to the paisa; that solving blind, without the settlement ids,
        reaches the same set; that the bank credit carries the UTR and the exact amount; that every
        order is in your books; and that fees, GST, TDS and TCS are right for their dates. Read-only
        — the engine never writes to Razorpay.
      </p>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        {inputs.map(([key, label, hint]) => (
          <label key={key} className="surface-card flex cursor-pointer flex-col gap-1 p-3 text-sm">
            <span className="flex items-center gap-1.5 font-medium">
              <Upload className="size-3.5 text-muted-foreground" />
              {label}
            </span>
            <span className="text-[11.5px] text-muted-foreground">{files[key]?.name ?? hint}</span>
            <input
              type="file"
              className="sr-only"
              aria-label={label}
              onChange={(e) => setFiles({ ...files, [key]: e.target.files?.[0] ?? null })}
            />
          </label>
        ))}
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => void reconcile(files)}
          disabled={busy}
          className="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[13px] hover:bg-muted disabled:opacity-60"
        >
          {busy ? <LoadingMark size={14} /> : <Play className="size-3.5" />}
          Check these payouts
        </button>
        <button
          type="button"
          onClick={() => void loadSample()}
          disabled={busy}
          className="rounded-md border border-border px-3 py-1.5 text-[13px] hover:bg-muted disabled:opacity-60"
        >
          Use the sample payouts
        </button>
      </div>

      {error && <p className="mt-3 text-sm text-red-600">{error}</p>}

      {run && (
        <div className="surface-card mt-4 p-4">
          <p className="m-0 text-sm">
            {run.settlements} payout(s): {run.tally.verified ?? 0} verified,{" "}
            {run.tally.verified_with_findings ?? 0} with findings, {run.tally.not_verified ?? 0} not
            verified.
          </p>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-muted-foreground">
                  <th className="pb-2 text-left font-normal">Payout</th>
                  <th className="pb-2 text-right font-normal">Amount</th>
                  <th className="pb-2 text-left font-normal">Settled</th>
                  <th className="pb-2 text-left font-normal">Checks</th>
                  <th className="pb-2 text-left font-normal">Result</th>
                </tr>
              </thead>
              <tbody>
                {run.results.map((r) => (
                  <PayoutRow key={r.settlement_id} r={r} />
                ))}
              </tbody>
            </table>
          </div>
          {run.unsettled_lines?.plain && (
            <p className="mt-3 text-xs text-muted-foreground">{run.unsettled_lines.plain}</p>
          )}
        </div>
      )}
    </section>
  );
}

// ── Open items ───────────────────────────────────────────────────────────

interface OpenItem {
  item_id: string;
  kind: string;
  ref: string;
  amount_cents: number;
  occurred_on: string;
  due_on: string;
  status: string;
  first_seen_batch: string;
  age_working_days: number;
  overdue_working_days: number;
}

interface OpenReport {
  as_of: string;
  calendar: string;
  settlement_cycle: string;
  summary: {
    open_count: number;
    open_value_cents: number;
    overdue_count: number;
    overdue_value_cents: number;
    buckets: { label: string; count: number; value_cents: number }[];
  };
  items: OpenItem[];
  truncated: number;
}

const KIND: Record<string, string> = {
  unsettled_payment: "Payment not yet paid out",
  refund_not_deducted: "Refund not yet deducted",
  withheld_settlement: "Settlement withheld",
};

function DueWhy({ item }: { item: OpenItem }) {
  const [why, setWhy] = useState<string[] | null>(null);
  const load = async () => {
    try {
      const r = await engineFetch(`calendar/due?captured_on=${item.occurred_on}`);
      const body = await r.json();
      if (!r.ok) throw new Error(engineErrorMessage(body, "The calendar check failed."));
      const skipped = (body.skipped ?? []) as { date: string; closed_because: string }[];
      setWhy([
        `Captured ${body.captured_on}; T+${body.t_plus_working_days} working days is ${body.due_on}, ${body.calendar_days} calendar day(s) later.`,
        ...skipped.map((s) => `${s.date} does not count: ${s.closed_because}.`),
      ]);
    } catch (e) {
      setWhy([e instanceof Error ? e.message : String(e)]);
    }
  };
  if (item.kind === "withheld_settlement") return null;
  return why ? (
    <ul className="m-0 mt-1 list-none p-0 text-[11.5px] text-muted-foreground">
      {why.map((w) => (
        <li key={w}>{w}</li>
      ))}
    </ul>
  ) : (
    <button
      type="button"
      onClick={() => void load()}
      className="mt-0.5 text-[11.5px] text-muted-foreground underline"
    >
      Why this due date?
    </button>
  );
}

function OpenItemsSection() {
  const [report, setReport] = useState<OpenReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [closed, setClosed] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await engineFetch(`open-items${closed ? "?include_closed=true" : ""}`);
      const body = await r.json().catch(() => ({}));
      if (!r.ok)
        throw new Error(engineErrorMessage(body, `Could not load open items (${r.status}).`));
      setReport(body as OpenReport);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [closed]);

  useEffect(() => {
    void load();
  }, [load]);

  const s = report?.summary;
  return (
    <section className="mt-12">
      <h2 className="m-0 text-[19px] font-semibold">Still waiting</h2>
      <p className="mt-2 max-w-[680px] text-[13.5px] leading-[1.6] text-muted-foreground">
        What no payout has contained yet, carried from one reconciliation to the next: payments not
        yet paid out, refunds not yet deducted, settlements withheld for review. Aged in working
        days — second and fourth Saturdays, Sundays and bank holidays do not count.
      </p>

      {error && <p className="mt-3 text-sm text-red-600">{error}</p>}

      {s && (
        <>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <div className="surface-card p-4">
              <p className="m-0 text-xs text-muted-foreground">Open</p>
              <p className="m-0 mt-1 text-xl font-semibold tabular-nums">
                {inr(s.open_value_cents)}
              </p>
              <p className="m-0 text-[11.5px] text-muted-foreground">{s.open_count} item(s)</p>
            </div>
            <div className="surface-card p-4">
              <p className="m-0 text-xs text-muted-foreground">
                Overdue on {report?.settlement_cycle}
              </p>
              <p
                className={cn(
                  "m-0 mt-1 text-xl font-semibold tabular-nums",
                  s.overdue_count > 0 && "text-red-600",
                )}
              >
                {inr(s.overdue_value_cents)}
              </p>
              <p className="m-0 text-[11.5px] text-muted-foreground">{s.overdue_count} item(s)</p>
            </div>
          </div>

          <div className="mt-3 flex flex-wrap gap-2">
            {s.buckets
              .filter((b) => b.count > 0)
              .map((b) => (
                <span
                  key={b.label}
                  className="rounded-full border border-border bg-muted/50 px-2 py-0.5 text-[11px]"
                >
                  {b.label}: {b.count} · {inr(b.value_cents)}
                </span>
              ))}
          </div>

          <label className="mt-4 flex items-center gap-2 text-[12.5px] text-muted-foreground">
            <input type="checkbox" checked={closed} onChange={(e) => setClosed(e.target.checked)} />
            Show closed items too
          </label>

          {report && report.items.length === 0 ? (
            <p className="mt-3 text-sm text-muted-foreground">
              Nothing is waiting. Reconcile a settlement on the{" "}
              <Link to="/" className="underline">
                main screen
              </Link>{" "}
              and whatever it leaves behind appears here.
            </p>
          ) : (
            <div className="surface-card mt-3 overflow-x-auto p-4">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-xs text-muted-foreground">
                    <th className="pb-2 text-left font-normal">Item</th>
                    <th className="pb-2 text-right font-normal">Amount</th>
                    <th className="pb-2 text-left font-normal">Due</th>
                    <th className="pb-2 text-right font-normal">Age</th>
                  </tr>
                </thead>
                <tbody>
                  {report?.items.slice(0, 100).map((it) => (
                    <tr key={it.item_id} className="border-b border-border/50 align-top">
                      <td className="py-2 pr-3">
                        <span className="font-mono text-xs">{it.ref}</span>
                        <span className="block text-[11.5px] text-muted-foreground">
                          {KIND[it.kind] ?? it.kind}
                          {it.status !== "open" && ` · ${it.status}`} · seen in{" "}
                          {it.first_seen_batch}
                        </span>
                      </td>
                      <td className="whitespace-nowrap py-2 pr-3 text-right tabular-nums">
                        {inr(Math.abs(it.amount_cents))}
                      </td>
                      <td className="py-2 pr-3 text-xs">
                        {it.due_on}
                        {it.overdue_working_days > 0 && (
                          <span className="block text-red-600">
                            {it.overdue_working_days} working day(s) overdue
                          </span>
                        )}
                        <DueWhy item={it} />
                      </td>
                      <td className="whitespace-nowrap py-2 text-right text-xs tabular-nums">
                        {it.age_working_days} wd
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {(report?.items.length ?? 0) > 100 && (
                <p className="mt-2 text-xs text-muted-foreground">
                  Showing the first 100, most overdue first.
                </p>
              )}
            </div>
          )}
          <p className="mt-2 text-[11px] text-muted-foreground">Calendar: {report?.calendar}</p>
        </>
      )}
    </section>
  );
}

function Payouts() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-40 border-b border-border bg-background/90 backdrop-blur-md">
        <div className="mx-auto flex max-w-[1120px] items-center justify-between gap-4 px-4 py-2.5 sm:px-6">
          <Wordmark pill="Payouts" />
          <div className="flex items-center gap-3.5">
            <Link
              to="/"
              className="whitespace-nowrap text-[14px] text-muted-foreground hover:text-foreground"
            >
              ← Agent Flow
            </Link>
            <HistoryLink className={NAV} />
            <Link to="/escalations" className={NAV}>
              <Flag className="size-4" />
              <span className="hidden sm:inline">Escalations</span>
            </Link>
            <Link to="/rulebook" className={NAV}>
              <ScrollText className="size-4" />
              <span className="hidden sm:inline">Rulebook</span>
            </Link>
            <SessionMenu />
            <ThemeToggle />
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[960px] px-4 py-10 sm:px-6">
        <p className="label-ui m-0 flex items-center gap-1.5 text-[10px] uppercase text-muted-foreground">
          <Landmark className="size-3.5" />
          Payouts · Razorpay and what is still waiting
        </p>
        <h1 className="mt-2 text-[30px] font-semibold leading-tight">Where the money is</h1>
        <RazorpaySection />
        <OpenItemsSection />
      </main>
    </div>
  );
}
