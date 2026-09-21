/**
 * Fees, GST, TDS and TCS on the matched payments, checked line by line.
 *
 * The engine computed this on every run and, until it was wired here,
 * returned it to nobody. A clean result is stated as clean, with what was
 * checked, because "no findings" and "not checked" must not look alike.
 */

import { Receipt } from "lucide-react";
import type { FeeAudit as FeeAuditData } from "@/lib/engine-types";
import { inr } from "@/lib/utils";

const OK = (v: string) => v === "ok";

export function FeeAudit({ audit }: { audit: FeeAuditData }) {
  const s = audit.summary;
  const flags: [string, string][] = [
    ["TDS", s.tds_compliance],
    ["TCS", s.tcs_compliance],
    ["GST", s.gst_issues ? "issue" : "ok"],
    ["Settlement integrity", s.settlement_integrity],
  ];
  return (
    <div className="surface-card p-6">
      <h3 className="flex items-center gap-2 text-lg font-semibold">
        <Receipt className="size-5 text-muted-foreground" />
        Fees and tax
      </h3>
      <p className="mt-1 text-sm text-muted-foreground">
        {s.total_findings === 0
          ? "Every matched payment's fee, GST, TDS and TCS agree with the rate card and with the law in force on its date."
          : `${s.total_findings} finding(s)${
              s.total_overcharge_cents > 0
                ? `, ${inr(s.total_overcharge_cents)} charged above the rate card`
                : ""
            }. Largest first.`}
      </p>
      <div className="mt-3 flex flex-wrap gap-2 text-[11px]">
        {flags.map(([k, v]) => (
          <span
            key={k}
            className={
              OK(v)
                ? "rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-emerald-700 dark:text-emerald-400"
                : "rounded-full border border-red-500/30 bg-red-500/10 px-2 py-0.5 text-red-600"
            }
          >
            {k}: {OK(v) ? "ok" : v}
          </span>
        ))}
      </div>
      {audit.findings.length > 0 && (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-xs text-muted-foreground">
                <th className="pb-2 text-left font-normal">Payment</th>
                <th className="pb-2 text-left font-normal">Finding</th>
                <th className="pb-2 text-right font-normal">Expected</th>
                <th className="pb-2 text-right font-normal">Charged</th>
              </tr>
            </thead>
            <tbody>
              {audit.findings.slice(0, 20).map((f, i) => (
                <tr key={i} className="border-b border-border/50 align-top">
                  <td className="py-2 pr-3 font-mono text-xs">{f.txn_id}</td>
                  <td className="py-2 pr-3">
                    {f.description}
                    {f.citation && (
                      <span className="block text-[11px] text-muted-foreground">{f.citation}</span>
                    )}
                  </td>
                  <td className="whitespace-nowrap py-2 text-right tabular-nums">
                    {inr(f.expected_cents)}
                  </td>
                  <td className="whitespace-nowrap py-2 text-right tabular-nums">
                    {inr(f.actual_cents)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {audit.findings.length > 20 && (
            <p className="mt-2 text-xs text-muted-foreground">
              …and {audit.findings.length - 20} more.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
