import { createFileRoute, Link } from "@tanstack/react-router";
import { CitationLink } from "@/components/citation-link";
import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Building2,
  CheckCircle2,
  ChevronDown,
  Flag,
  Info,
  Layers,
  ListChecks,
  Scale,
  Search,
  ShieldAlert,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { HistoryLink } from "@/components/history-link";
import { LoadingMark } from "@/components/loading-mark";
import { ThemeToggle } from "@/components/theme-toggle";
import { SessionMenu } from "@/components/session-menu";
import { engineFetch } from "@/lib/api";
import { Wordmark } from "@/components/wordmark";

export const Route = createFileRoute("/rulebook")({
  component: Rulebook,
});

// Refined, premium color palette using vibrant OKLCH values
const BASIS: Record<string, { label: string; tone: string; note: string; icon: LucideIcon }> = {
  statutory: {
    label: "Statutory",
    tone: "oklch(0.65 0.25 25)", // Vibrant Rose/Red
    note: "Required by law or binding rules — the parameter is set by the source, not by us.",
    icon: Scale,
  },
  regulatory_guidance: {
    label: "Regulatory guidance",
    tone: "oklch(0.75 0.2 70)", // Vibrant Amber
    note: "Supervisory or standard-setter guidance states the obligation; the detection parameters are ours.",
    icon: Building2,
  },
  internal_policy: {
    label: "Internal policy — not law",
    tone: "oklch(0.7 0.15 260)", // Vibrant Indigo/Blue
    note: "This system's own risk appetite. No statutory force, and no external source imposes it.",
    icon: ShieldAlert,
  },
};

interface Rule {
  rule_id: string;
  title: string;
  severity: string; // present in the API (verified: "HIGH")
  action: string;
  basis: string; // Changed back to string to match original interface
  authority: string;
  source_name: string;
  citation: string;
  reference_url: string;
  rule_text: string;
  threshold_applied: string;
  why: string;
  remediation: string;
}

function Rulebook() {
  const [data, setData] = useState<{ rules: Rule[]; scope_note?: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [basisFilter, setBasisFilter] = useState<string>("all");

  // Fetch Rules
  useEffect(() => {
    async function fetchRules() {
      try {
        setLoading(true);
        // Fixed endpoint and response handling
        const response = await engineFetch("compliance/rulebook");
        if (!response.ok) throw new Error(`Request failed (${response.status})`);
        setData(await response.json());
      } catch (err) {
        setError("Failed to load rulebook. Please try again later.");
        console.error(err);
      } finally {
        setLoading(false);
      }
    }
    fetchRules();
  }, []);

  // Filter Logic
  const filteredRules = useMemo(() => {
    if (!data?.rules) return [];

    return data.rules.filter((rule) => {
      const matchesSearch =
        rule.title.toLowerCase().includes(query.toLowerCase()) ||
        rule.rule_id.toLowerCase().includes(query.toLowerCase()) ||
        rule.rule_text.toLowerCase().includes(query.toLowerCase());

      const matchesBasis = basisFilter === "all" || rule.basis === basisFilter;

      return matchesSearch && matchesBasis;
    });
  }, [data, query, basisFilter]);

  return (
    <div className="min-h-screen bg-neutral-50 dark:bg-neutral-950 text-neutral-900 dark:text-neutral-100 font-sans">
      {/* Top Navigation */}
      <header className="sticky top-0 z-50 flex items-center justify-between px-6 py-4 bg-white/80 dark:bg-neutral-950/80 backdrop-blur-md border-b border-neutral-200 dark:border-neutral-800">
        <div className="flex items-center gap-4">
          <Wordmark pill="Rulebook" />
        </div>
        <div className="flex items-center gap-4">
          <Link
            to="/"
            className="text-sm font-medium text-neutral-500 hover:text-neutral-900 dark:hover:text-neutral-100 transition-colors mr-2"
          >
            ← Agent Flow
          </Link>
          {/* Parity with the other screens. This header reached home and
              nowhere else, so a reviewer checking a rule had to go back
              through the main page to get to history or escalations. */}
          <HistoryLink className="flex items-center gap-1.5 whitespace-nowrap text-sm font-medium text-neutral-500 hover:text-neutral-900 dark:hover:text-neutral-100 transition-colors">
            History
          </HistoryLink>
          <Link
            to="/escalations"
            className="nav-flag flex items-center gap-1.5 whitespace-nowrap text-sm font-medium text-neutral-500 hover:text-neutral-900 dark:hover:text-neutral-100 transition-colors"
          >
            <Flag className="size-4" />
            <span className="hidden sm:inline">Escalations</span>
          </Link>
          <ThemeToggle />
          <SessionMenu />
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 py-8">
        {/* ── masthead (from visual reference) ──────────────────────────── */}
        <section className="grid items-end gap-12 mb-12 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,0.85fr)]">
          <div>
            <p className="font-[family-name:var(--font-eyebrow)] text-[12px] m-0 mb-4 uppercase tracking-[0.09em] text-neutral-500">
              Published control set · Agent 7
            </p>
            <h1 className="m-0 mb-5 font-[family-name:var(--font-display)] text-[52px] font-normal leading-[1.03] tracking-[-0.025em] text-neutral-900 dark:text-neutral-100">
              {data?.rules?.length === 0
                ? "The control set"
                : data?.rules?.length === 12
                  ? "Twelve controls"
                  : `${data?.rules?.length || 0} controls`}
              , and who says so
            </h1>
            <p className="m-0 max-w-[600px] text-[16px] leading-[1.65] text-neutral-500">
              Every control this engine enforces, the authority it derives from, what that authority
              actually requires, and the threshold this system applies. A rule enforced because the
              law requires it and a rule enforced because we prefer it are different claims, so the
              rulebook never lets them look alike.
            </p>
          </div>
          <div className="rounded-[14px] border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 px-[18px] py-4 shadow-sm">
            <p className="m-0 mb-2 font-mono text-[9.5px] uppercase tracking-[0.16em] text-[var(--s-withheld)]">
              Scope
            </p>
            <p className="m-0 text-[12.5px] leading-[1.6] text-neutral-500">
              {data?.scope_note ?? "Fetching scope…"}
            </p>
          </div>
        </section>

        {/* Controls: Search & Filters */}
        <div className="flex flex-col sm:flex-row gap-4 mb-8">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-neutral-400" />
            <input
              type="text"
              placeholder="Search by rule ID, title, or keywords..."
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="w-full pl-10 pr-4 py-2.5 bg-white dark:bg-neutral-900 border border-neutral-200 dark:border-neutral-800 rounded-xl shadow-sm focus:ring-2 focus:ring-blue-500 focus:border-transparent outline-none transition-all"
            />
          </div>

          <div className="relative shrink-0">
            <select
              value={basisFilter}
              onChange={(e) => setBasisFilter(e.target.value)}
              className="appearance-none pl-4 pr-10 py-2.5 bg-white dark:bg-neutral-900 border border-neutral-200 dark:border-neutral-800 rounded-xl shadow-sm focus:ring-2 focus:ring-blue-500 outline-none transition-all cursor-pointer font-medium"
            >
              <option value="all">All Basis Types</option>
              {Object.entries(BASIS).map(([key, { label }]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
            <ChevronDown className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-neutral-400 pointer-events-none" />
          </div>
        </div>

        {/* Content State Handling */}
        {loading && (
          <div className="flex items-center justify-center py-24">
            <LoadingMark size={40} />
          </div>
        )}

        {error && (
          <div className="flex flex-col items-center justify-center py-16 text-red-500 bg-red-50 dark:bg-red-950/20 rounded-2xl border border-red-200 dark:border-red-900/50">
            <AlertTriangle className="w-10 h-10 mb-4" />
            <p className="font-medium">{error}</p>
          </div>
        )}

        {/* Rule Grid */}
        {!loading && !error && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {filteredRules.length > 0 ? (
              filteredRules.map((rule) => <RuleCard key={rule.rule_id} rule={rule} />)
            ) : (
              <div className="col-span-full py-16 text-center text-neutral-500 dark:text-neutral-400 bg-white dark:bg-neutral-900 rounded-2xl border border-dashed border-neutral-200 dark:border-neutral-800">
                <ListChecks className="w-12 h-12 mx-auto mb-4 opacity-50" />
                <h3 className="text-lg font-medium text-neutral-900 dark:text-neutral-100">
                  No rules found
                </h3>
                <p>Try adjusting your search query or filters.</p>
              </div>
            )}
          </div>
        )}

        {/* ── closing argument ──────────────────────────────────────────── */}
        {!loading && !error && (
          <section className="mt-16 border-t border-neutral-200 dark:border-neutral-800 pt-[34px]">
            <p className="font-[family-name:var(--font-eyebrow)] text-[12px] m-0 uppercase tracking-[0.09em] text-amber-600 dark:text-amber-500">
              On honesty
            </p>
            <h2 className="m-0 mt-[18px] max-w-[620px] font-[family-name:var(--font-display)] text-[30px] font-normal leading-[1.06] tracking-[-0.025em] text-neutral-900 dark:text-neutral-100">
              A threshold we invented is labelled as one
            </h2>
            <p className="m-0 mt-[15px] max-w-[620px] text-[15px] leading-[1.65] text-neutral-600 dark:text-neutral-400">
              No Indian law caps a single transaction at ₹5 crore. The ceiling in LIMIT_EXCEEDED is
              ours, and the rulebook says so in the same breath it states the control. Presenting an
              internal threshold as a statutory requirement would misrepresent the law to whoever
              relies on this output.
            </p>
            <p className="m-0 mt-6 font-mono text-[11.5px] text-neutral-400 dark:text-neutral-500">
              {data?.rules?.length || 0} controls ·{" "}
              {data?.rules?.filter((r) => r.action === "BLOCKED").length || 0} block funds · served
              live from /compliance/rulebook
            </p>
          </section>
        )}
      </main>
    </div>
  );
}

// Extracted RuleCard Component for cleaner rendering
function RuleCard({ rule }: { rule: Rule }) {
  // noUncheckedIndexedAccess makes an index lookup possibly-undefined, and
  // the fallback must itself be indexed rather than dotted.
  const basisConfig = BASIS[rule.basis] ?? BASIS["internal_policy"]!;
  const BasisIcon = basisConfig.icon;
  // severity is present in the API; this only covers an older engine.
  const severity = rule.severity || (rule.action === "BLOCKED" ? "High" : "Medium");

  return (
    <div className="flex flex-col bg-white dark:bg-neutral-900 rounded-2xl border border-neutral-200 dark:border-neutral-800 shadow-sm overflow-hidden hover:shadow-md transition-shadow">
      {/* Top Banner indicating Basis via OKLCH color */}
      <div
        className="px-5 py-3 border-b flex items-center justify-between"
        style={{
          backgroundColor: `color-mix(in oklch, ${basisConfig.tone} 10%, transparent)`,
          borderColor: `color-mix(in oklch, ${basisConfig.tone} 20%, transparent)`,
        }}
      >
        <div className="flex items-center gap-2">
          <BasisIcon className="w-4 h-4" style={{ color: basisConfig.tone }} />
          <span
            className="text-xs font-semibold uppercase tracking-wider"
            style={{ color: basisConfig.tone }}
          >
            {basisConfig.label}
          </span>
        </div>
        <span className="text-xs font-mono text-neutral-500 dark:text-neutral-400">
          {rule.rule_id}
        </span>
      </div>

      {/* Main Content */}
      <div className="p-5 flex-1 space-y-4">
        {/* What the basis MEANS. BASIS carried this note and nothing rendered
            it, so the page whose whole purpose is publishing the control set
            showed the label — STATUTORY, INTERNAL POLICY — without saying
            what follows from it. The label alone is the distinction asserted;
            the note is the distinction explained. */}
        <p className="m-0 text-[12px] leading-[1.5] text-neutral-500 dark:text-neutral-400">
          {basisConfig.note}
        </p>
        <div>
          <h3 className="text-lg font-semibold leading-tight mb-2">{rule.title}</h3>
          <p className="text-sm text-neutral-600 dark:text-neutral-400">{rule.rule_text}</p>
        </div>

        {/* Key/Value Meta Grid */}
        <div className="grid grid-cols-2 gap-4 py-3 border-y border-neutral-100 dark:border-neutral-800">
          <div>
            <span className="block text-xs font-medium text-neutral-500 mb-1">Action</span>
            <span className="text-sm font-medium">
              {rule.action === "BLOCKED" ? "Blocks funds" : "Flags for review"}
            </span>
          </div>
          <div>
            <span className="block text-xs font-medium text-neutral-500 mb-1">Severity</span>
            <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium bg-neutral-100 dark:bg-neutral-800">
              <span
                className={`w-2 h-2 rounded-full ${
                  severity.toLowerCase() === "high"
                    ? "bg-red-500"
                    : severity.toLowerCase() === "medium"
                      ? "bg-amber-500"
                      : "bg-blue-500"
                }`}
              />
              {severity}
            </span>
          </div>
        </div>

        {/* Rationale & Remediation */}
        <div className="space-y-3">
          <div>
            <div className="flex items-center gap-1.5 text-xs font-semibold text-neutral-900 dark:text-neutral-100 mb-1">
              <Info className="w-3.5 h-3.5 text-neutral-500" /> Why
            </div>
            <p className="text-sm text-neutral-600 dark:text-neutral-400">{rule.why}</p>
          </div>
          <div>
            <div className="flex items-center gap-1.5 text-xs font-semibold text-neutral-900 dark:text-neutral-100 mb-1">
              <CheckCircle2 className="w-3.5 h-3.5 text-neutral-500" /> Remediation
            </div>
            <p className="text-sm text-neutral-600 dark:text-neutral-400">{rule.remediation}</p>
          </div>
          {/* threshold_applied and citation are returned by the API and this
              redesign dropped both. The threshold is the number that decides
              whether a rule fires at all, and on a compliance screen the
              citation is the claim's evidence — a rule shown without either
              cannot be checked by the person it is shown to. */}
          {rule.threshold_applied && (
            <div>
              <div className="flex items-center gap-1.5 text-xs font-semibold text-neutral-900 dark:text-neutral-100 mb-1">
                <Layers className="w-3.5 h-3.5 text-neutral-500" /> Threshold applied
              </div>
              <p className="font-mono text-sm text-neutral-600 dark:text-neutral-400">
                {rule.threshold_applied}
              </p>
            </div>
          )}
          {rule.citation && (
            <div>
              <div className="flex items-center gap-1.5 text-xs font-semibold text-neutral-900 dark:text-neutral-100 mb-1">
                <Info className="w-3.5 h-3.5 text-neutral-500" /> Citation
              </div>
              <p className="text-sm text-neutral-600 dark:text-neutral-400">{rule.citation}</p>
            </div>
          )}
        </div>
      </div>

      {/* Footer / Citations */}
      <div className="px-5 py-3 bg-neutral-50 dark:bg-neutral-950/50 border-t border-neutral-200 dark:border-neutral-800 text-xs text-neutral-500 dark:text-neutral-400 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Layers className="w-3.5 h-3.5" />
          <span>
            {rule.authority} • {rule.source_name}
          </span>
        </div>
        {rule.reference_url && (
          <CitationLink
            url={rule.reference_url}
            label="Citation"
            className="text-blue-600 dark:text-blue-400 font-medium"
          />
        )}
      </div>
    </div>
  );
}
