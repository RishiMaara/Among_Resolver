/**
 * Recovery claims: what the payout check found that someone owes the
 * merchant, drafted so a person can send it.
 *
 * Nothing here sends anything. Each claim is a message to copy into a
 * Razorpay support ticket or an email to the bank, with its evidence as a
 * CSV, and each says what its figure rests on: the data alone, or the rate
 * card the engine audits against, which a merchant with negotiated pricing
 * must check first.
 */

import { useState } from "react";
import { Copy, Download, Check as CheckIcon } from "lucide-react";
import { inr } from "@/lib/utils";

export interface Claim {
  claim_id: string;
  settlement_id: string;
  kind: string;
  to: string;
  amount_cents: number;
  basis: "stated" | "estimated";
  check_first: string;
  subject: string;
  message: string;
  evidence_csv: string;
}

export interface Recovery {
  claims: Claim[];
  total_cents: number;
  stated_cents: number;
  estimated_cents: number;
  plain: string;
}

function download(name: string, text: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

const fileSafe = (s: string) => s.replace(/[^A-Za-z0-9_-]+/g, "_");

function ClaimCard({ c }: { c: Claim }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(`${c.subject}\n\n${c.message}`);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setOpen(true); // clipboard refused: show the text so it can be selected
    }
  };
  return (
    <li className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="m-0 text-sm font-medium">{c.subject}</p>
        <p className="m-0 text-sm font-semibold tabular-nums">{inr(c.amount_cents)}</p>
      </div>
      <p className="m-0 mt-1 text-xs text-muted-foreground">
        To {c.to} ·{" "}
        {c.basis === "stated"
          ? "both figures come from your files"
          : "the expected figure comes from the rate card"}
      </p>
      {c.check_first && (
        <p className="m-0 mt-1.5 text-xs text-[var(--s-withheld)]">{c.check_first}</p>
      )}
      <div className="mt-2 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={copy}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs hover:bg-muted"
        >
          {copied ? <CheckIcon className="size-3.5" /> : <Copy className="size-3.5" />}
          {copied ? "Copied" : "Copy message"}
        </button>
        <button
          type="button"
          onClick={() =>
            download(
              `claim_${fileSafe(c.claim_id)}.txt`,
              `${c.subject}\n\n${c.message}\n`,
              "text/plain",
            )
          }
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs hover:bg-muted"
        >
          <Download className="size-3.5" />
          Download claim
        </button>
        {c.evidence_csv && (
          <button
            type="button"
            onClick={() =>
              download(`evidence_${fileSafe(c.claim_id)}.csv`, c.evidence_csv, "text/csv")
            }
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs hover:bg-muted"
          >
            <Download className="size-3.5" />
            Download evidence (CSV)
          </button>
        )}
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          className="px-1 text-xs text-muted-foreground underline-offset-2 hover:underline"
        >
          {open ? "Hide message" : "Read message"}
        </button>
      </div>
      {open && (
        <pre className="mt-2 whitespace-pre-wrap rounded-md bg-muted p-3 font-sans text-xs leading-relaxed">
          {c.message}
        </pre>
      )}
    </li>
  );
}

export function RecoveryClaims({ recovery }: { recovery: Recovery }) {
  return (
    <div className="mt-4 border-t border-border pt-4">
      <h3 className="m-0 text-sm font-semibold">Recovery claims</h3>
      <p className="m-0 mt-1 text-xs text-muted-foreground">{recovery.plain}</p>
      {recovery.claims.length > 0 && (
        <ul className="m-0 mt-3 grid list-none gap-2 p-0">
          {recovery.claims.map((c) => (
            <ClaimCard key={c.claim_id} c={c} />
          ))}
        </ul>
      )}
    </div>
  );
}
