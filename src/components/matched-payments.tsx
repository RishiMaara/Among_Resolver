/**
 * The payments the engine picked, named.
 *
 * WHY THIS EXISTS
 * ---------------
 * A withheld batch told the reviewer "please confirm these are the right
 * payments" and then showed them nothing — no ids, no amounts, no dates. The
 * engine knew exactly which payments it had selected (matched_txn_ids was in
 * the response the whole time) and every screen threw that away, so the
 * person was sent back to the source file to find them by hand.
 *
 * That inverts the argument the whole project rests on. Adding a column of
 * numbers until they hit a target is the easy half and anyone can do it.
 * Saying WHICH payments make up a settlement — so a reviewer can agree or
 * disagree in seconds instead of searching — is the half worth building, and
 * it was the half not being shown.
 *
 * Ordered by amount, because that is how a person scans a list like this: the
 * large ones carry the risk and are checked first. The total is stated so the
 * arithmetic can be verified at a glance rather than taken on trust.
 */

import { AcceptFifo, type FifoProposal } from "@/components/accept-fifo";

export interface MatchedTxn {
  txn_id: string;
  source: string;
  amount_cents: number;
  currency: string;
  timestamp_utc: string;
  reference: string;
  memo: string;
}

function rupees(cents: number, currency = "INR") {
  const symbol = currency === "INR" ? "₹" : `${currency} `;
  return (
    symbol +
    (cents / 100).toLocaleString("en-IN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  );
}

export interface Interchangeable {
  groups: {
    amount_cents: number;
    currency: string;
    picked: number;
    identical_available: number;
    txn_ids: string[];
  }[];
  wholly_interchangeable: boolean;
  fifo_proposal?: FifoProposal | null;
}

export function MatchedPayments({
  rows,
  targetCents,
  compact = false,
  interchangeable,
  batchId,
}: {
  rows: MatchedTxn[];
  /** The gross figure being searched for, so the sum can be checked here. */
  targetCents?: number | null | undefined;
  compact?: boolean;
  /** Needed only to offer the oldest-first convention on a fungible set. */
  batchId?: string;
  /** Groups where the pool holds more identical payments than were picked.
   *  Without this a reviewer sees three specific ids and assumes they were
   *  identified, when in fact any three of ten would have done — and the
   *  question they should be asking is a different one. */
  interchangeable?: Interchangeable | null | undefined;
}) {
  if (!rows || rows.length === 0) return null;

  // The doc comment above promises this ordering and the component did not
  // enforce it — the sort lived in the engine's matched_rows, so any caller
  // passing rows straight from somewhere else got whatever order they came
  // in. A component that documents a guarantee should hold it itself.
  const ordered = [...rows].sort((a, b) => b.amount_cents - a.amount_cents);

  const total = rows.reduce((a, r) => a + r.amount_cents, 0);
  const currency = rows[0]?.currency ?? "INR";
  const diff = targetCents == null ? null : total - targetCents;

  // Which of these rows could have been swapped for an identical one.
  const swappable = new Set<string>();
  for (const g of interchangeable?.groups ?? []) {
    for (const id of g.txn_ids) swappable.add(id);
  }

  return (
    <div className="overflow-hidden rounded-[11px] border border-border">
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border bg-muted/40 px-3 py-2">
        <span className="label-ui text-[10px] uppercase text-muted-foreground">
          The {rows.length} payment{rows.length === 1 ? "" : "s"} it picked
        </span>
        <span className="font-mono text-[11.5px] tabular-nums">
          {rupees(total, currency)}
          {diff != null && (
            <span
              className="ml-2"
              style={{
                color: diff === 0 ? "var(--s-done)" : "var(--s-withheld)",
              }}
            >
              {diff === 0
                ? "= target"
                : `${diff > 0 ? "over" : "short"} by ${rupees(Math.abs(diff), currency)}`}
            </span>
          )}
        </span>
      </div>

      <div className={compact ? "max-h-[220px] overflow-y-auto" : ""}>
        <table className="w-full border-collapse text-left">
          <thead>
            <tr className="border-b border-border">
              {["Payment", "Amount", "Date", "Reference"].map((h) => (
                <th
                  key={h}
                  className="label-ui px-3 py-1.5 text-[9.5px] uppercase text-muted-foreground"
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ordered.map((r) => (
              <tr key={r.txn_id} className="border-b border-border last:border-0">
                <td className="px-3 py-1.5 font-mono text-[11.5px]">
                  {r.txn_id}
                  <span className="ml-1.5 text-[9.5px] uppercase text-muted-foreground">
                    {r.source}
                  </span>
                  {swappable.has(r.txn_id) && (
                    <span
                      className="ml-1.5 rounded-full px-1.5 py-0.5 text-[9px] uppercase"
                      style={{
                        color: "var(--s-withheld)",
                        background: "color-mix(in oklab, var(--s-withheld) 12%, transparent)",
                      }}
                      title="Identical payments exist in the pool — this one is interchangeable with them"
                    >
                      any of many
                    </span>
                  )}
                </td>
                <td className="px-3 py-1.5 text-right font-mono text-[11.5px] tabular-nums">
                  {rupees(r.amount_cents, r.currency)}
                </td>
                <td className="px-3 py-1.5 font-mono text-[11px] text-muted-foreground">
                  {r.timestamp_utc.slice(0, 10)}
                </td>
                <td
                  className="max-w-[220px] truncate px-3 py-1.5 font-mono text-[11px] text-muted-foreground"
                  title={r.reference || r.memo}
                >
                  {r.reference || <span className="italic opacity-60">none</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {interchangeable?.groups?.length ? (
        <p className="m-0 border-t border-border px-3 py-2 text-[11px] leading-[1.5] text-muted-foreground">
          {interchangeable.groups.map((g, i) => (
            <span key={i}>
              {i > 0 && " "}
              The pool holds {g.identical_available} payments of{" "}
              {rupees(g.amount_cents, g.currency)} that are identical in amount, time and reference;{" "}
              {g.picked} were named. Which ones is arbitrary — check instead that they are not
              claimed by another settlement.
            </span>
          ))}
        </p>
      ) : null}

      {/* The way out, offered only where it is defensible: every picked
          payment interchangeable, so the choice is genuinely arbitrary. */}
      {interchangeable?.wholly_interchangeable && interchangeable.fifo_proposal && batchId ? (
        <div className="px-3 pb-3">
          <AcceptFifo batchId={batchId} proposal={interchangeable.fifo_proposal} />
        </div>
      ) : null}
    </div>
  );
}
