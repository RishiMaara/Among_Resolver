/**
 * What compliance found, on the screen of the person who has to act on it.
 *
 * The engine attaches a full finding to every transaction a rule hits —
 * rule, severity, the authority behind it, the citation, what was observed,
 * what to do next — and until now the API surfaced only BLOCKED ones. Every
 * FLAGGED finding, which is most of what a reviewer actually looks at, was
 * computed and thrown away. On one realistic merchant day that meant six
 * flagged payments under two rules reaching no screen at all.
 *
 * TWO THINGS THIS REFUSES TO BLUR
 *
 * Basis. A rule enforced because the law requires it and a rule enforced
 * because this firm prefers it are different claims, and a compliance screen
 * that renders them identically is quietly lying about which is which. The
 * basis badge is the loudest thing on each card, and an internal-policy rule
 * says NOT LAW in as many words. A reviewer pushing back on an internal
 * threshold is exercising judgement; pushing back on a statutory one is not.
 *
 * Suspicion. A flag is not an accusation. Most structuring patterns are
 * instalments, most duplicates are double ingestion, and the engine's own
 * `why` text says so — so it is shown rather than hidden behind a chevron.
 * A reviewer who has been taught that every alert is a criminal stops
 * reading them.
 *
 * Grouped by rule rather than by transaction, because a reviewer decides
 * once per pattern: "those four are one wholesale customer restocking" is a
 * single judgement covering four payments.
 */

import { CitationLink } from "@/components/citation-link";
import { ComplianceDecision } from "@/components/compliance-decision";

export interface ComplianceFindingGroup {
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
  transaction_count: number;
  total_amount_cents: number;
  transactions: {
    txn_id: string;
    amount_cents: number;
    currency: string;
    timestamp_utc: string;
    payer_id: string;
    memo: string;
  }[];
}

function money(cents: number, currency = "INR") {
  const symbol = currency === "INR" ? "₹" : `${currency} `;
  return (
    symbol +
    (cents / 100).toLocaleString("en-IN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  );
}

/** The three bases, and how emphatically each may speak. */
const BASIS = {
  statutory: {
    label: "STATUTORY",
    token: "var(--s-statutory)",
    note: "Required by law.",
  },
  regulatory_guidance: {
    label: "REGULATORY GUIDANCE",
    token: "var(--s-guidance)",
    note: "Supervisory guidance — the threshold below is this engine's calibration, not a legal line.",
  },
  internal_policy: {
    label: "INTERNAL POLICY — NOT LAW",
    token: "var(--s-internal)",
    note: "This firm's own control. No statutory force; a reviewer may override it.",
  },
} as const;

function basisOf(b: string) {
  return BASIS[b as keyof typeof BASIS] ?? BASIS.internal_policy;
}

const SEVERITY: Record<string, string> = {
  HIGH: "var(--destructive)",
  MEDIUM: "var(--s-withheld)",
  LOW: "var(--muted-foreground)",
};

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="label-ui m-0 mb-1 text-[9.5px] uppercase text-muted-foreground">{label}</p>
      <div className="text-[12.5px] leading-[1.55]">{children}</div>
    </div>
  );
}

export interface TriagedFinding extends ComplianceFindingGroup {
  auto_disposition?: {
    disposition: "auto" | "human";
    reason: string;
    residual?: string;
  };
}

export interface ComplianceTriage {
  findings: TriagedFinding[];
  auto_closed: number;
  needs_human: number;
  summary: string;
}

export function ComplianceReview({
  review,
  batchId,
}: {
  review: ComplianceTriage | null | undefined;
  batchId: string;
}) {
  const findings = review?.findings;
  // A clean run must say so out loud. Silence reads as "not checked", and on
  // a compliance screen that is the one ambiguity worth spending space on.
  if (!findings || findings.length === 0) {
    return (
      <div className="surface-card p-5">
        <p className="label-ui m-0 text-[10px] uppercase text-muted-foreground">
          Compliance review
        </p>
        <p className="m-0 mt-2 text-[13px]" style={{ color: "var(--s-done)" }}>
          Screened, nothing flagged.
        </p>
        <p className="m-0 mt-1 text-[11.5px] text-muted-foreground">
          Every payment in this batch was checked against the published rulebook and none matched a
          rule. This is advisory screening to support a human review, not a compliance decision.
        </p>
      </div>
    );
  }

  const blocked = findings.filter((f) => f.action === "BLOCKED").length;

  return (
    <div className="surface-card p-6">
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
        <p className="label-ui m-0 text-[10px] uppercase text-muted-foreground">
          Compliance review · {findings.length} to look at
        </p>
        <p className="m-0 text-[11.5px] text-muted-foreground">
          {review?.summary ??
            (blocked > 0
              ? `${blocked} blocked and excluded from matching; the rest are flagged for judgement.`
              : "All flagged for judgement — none blocked. A flag is not an accusation.")}
        </p>
      </div>

      <div className="space-y-3.5">
        {findings.map((f) => {
          const basis = basisOf(f.basis);
          const currency = f.transactions[0]?.currency ?? "INR";
          return (
            <article
              key={f.rule_id}
              className="overflow-hidden rounded-[12px] border border-border"
              style={{ borderLeft: `3px solid ${basis.token}` }}
            >
              <div className="flex flex-wrap items-center gap-2 border-b border-border bg-muted/40 px-4 py-2.5">
                <span
                  className="label-ui rounded-full px-2 py-[3px] text-[9.5px] uppercase"
                  style={{
                    color: basis.token,
                    background: `color-mix(in oklab, ${basis.token} 14%, transparent)`,
                  }}
                >
                  {basis.label}
                </span>
                <span
                  className="label-ui text-[9.5px] uppercase"
                  style={{ color: SEVERITY[f.severity] ?? "var(--muted-foreground)" }}
                >
                  {f.severity}
                </span>
                {f.action === "BLOCKED" && (
                  <span
                    className="label-ui rounded-full px-2 py-[3px] text-[9.5px] uppercase"
                    style={{
                      color: "var(--s-blocked)",
                      background: "color-mix(in oklab, var(--s-blocked) 14%, transparent)",
                    }}
                  >
                    Blocked
                  </span>
                )}
                <span className="ml-auto font-mono text-[10.5px] text-muted-foreground">
                  {f.rule_id}
                </span>
              </div>

              <div className="space-y-3 px-4 py-3.5">
                <div>
                  <h4 className="m-0 text-[14px] font-semibold">{f.title}</h4>
                  <p className="m-0 mt-0.5 text-[11.5px] text-muted-foreground">{basis.note}</p>
                </div>

                {f.why && (
                  <Field label="Why this rule exists">
                    <span className="text-muted-foreground">{f.why}</span>
                  </Field>
                )}

                {f.threshold_applied && (
                  <Field label="The threshold this engine used">
                    <span className="font-mono text-[11.5px] text-muted-foreground">
                      {f.threshold_applied}
                    </span>
                  </Field>
                )}

                {/* Only when someone is actually being asked. On an
                    auto-closed finding this printed "Confirm whether this is a
                    genuine second payment" directly above "Closed
                    automatically — no person needed to look at this", which
                    are instructions to do a thing and to not do it. Anything
                    genuinely left over is carried by the residual note. */}
                {f.remediation && f.auto_disposition?.disposition !== "auto" && (
                  <Field label="What to do next">{f.remediation}</Field>
                )}

                {(f.authority || f.reference_url) && (
                  <Field label="Authority">
                    <span className="text-muted-foreground">
                      {f.authority}
                      {f.citation ? ` — ${f.citation}` : ""}
                    </span>
                    {/* An internal rule has no official source, and calling
                        one that is not its own "the official source" borrows
                        authority it does not have. All five internal_policy
                        rules point at FIU-IND, FATF or the US BSA/AML manual
                        — on a card that says NOT LAW and "no statutory force"
                        in the same breath. The link is genuinely useful
                        background, so it stays; the label stops implying the
                        rule rests on it. */}
                    {f.reference_url && (
                      <div className="mt-1">
                        <CitationLink
                          url={f.reference_url}
                          label={
                            f.basis === "internal_policy"
                              ? "Background reading — not the basis for this rule"
                              : "Read the official source"
                          }
                          className="text-[12px] text-accent"
                        />
                      </div>
                    )}
                  </Field>
                )}
              </div>

              <div className="border-t border-border">
                <div className="flex items-baseline justify-between gap-2 px-4 py-2">
                  <span className="label-ui text-[9.5px] uppercase text-muted-foreground">
                    {f.transaction_count} payment
                    {f.transaction_count === 1 ? "" : "s"} matched this rule
                  </span>
                  <span className="font-mono text-[11.5px] tabular-nums">
                    {money(f.total_amount_cents, currency)}
                  </span>
                </div>
                <div className="max-h-[190px] overflow-y-auto">
                  <table className="w-full border-collapse text-left">
                    <tbody>
                      {f.transactions.map((t) => (
                        <tr key={t.txn_id} className="border-t border-border">
                          <td className="px-4 py-1.5 font-mono text-[11.5px]">{t.txn_id}</td>
                          <td className="px-2 py-1.5 text-right font-mono text-[11.5px] tabular-nums">
                            {money(t.amount_cents, t.currency)}
                          </td>
                          <td className="px-2 py-1.5 font-mono text-[11px] text-muted-foreground">
                            {t.timestamp_utc.slice(0, 10)} {t.timestamp_utc.slice(11, 16)}
                          </td>
                          <td className="max-w-[170px] truncate px-4 py-1.5 font-mono text-[11px] text-muted-foreground">
                            {t.payer_id || <span className="italic opacity-60">no payer</span>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* The card said what to do next and had no way to say it was
                  done. Per rule, not per payment: one judgement covers the
                  pattern. */}
              {f.auto_disposition?.disposition === "auto" ? (
                <div className="border-t border-border px-4 py-2.5">
                  <p className="m-0 text-[12px]">
                    <span style={{ color: "var(--s-done)" }}>Closed automatically</span>{" "}
                    <span className="text-muted-foreground">
                      — no person needed to look at this.
                    </span>
                  </p>
                  <p className="m-0 mt-1 text-[11.5px] leading-[1.5] text-muted-foreground">
                    {f.auto_disposition.reason}
                  </p>
                  {f.auto_disposition.residual && (
                    <p
                      className="m-0 mt-1 text-[11px] leading-[1.5]"
                      style={{ color: "var(--s-withheld)" }}
                    >
                      {f.auto_disposition.residual}
                    </p>
                  )}
                </div>
              ) : (
                <>
                  {f.auto_disposition?.reason && (
                    <p className="m-0 border-t border-border px-4 pt-2.5 text-[11.5px] leading-[1.5] text-muted-foreground">
                      <span className="label-ui text-[9.5px] uppercase">
                        Why this one is yours:
                      </span>{" "}
                      {f.auto_disposition.reason}
                    </p>
                  )}
                  <ComplianceDecision
                    batchId={batchId}
                    ruleId={f.rule_id}
                    basis={f.basis}
                    txnIds={f.transactions.map((t) => t.txn_id)}
                  />
                </>
              )}
            </article>
          );
        })}
      </div>

      <p className="m-0 mt-4 text-[11px] leading-[1.5] text-muted-foreground">
        This is automated screening to support a human review. It does not file CTRs or STRs, and it
        screens sanctions names by exact match after normalisation, so a misspelt or transliterated
        name will pass — these are advisory inputs to a reviewer, not compliance decisions in their
        own right.
      </p>
    </div>
  );
}
