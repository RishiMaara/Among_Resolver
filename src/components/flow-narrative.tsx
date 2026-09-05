/**
 * The scroll-linked narrative under the canvas.
 *
 * Ported from design/Agent Flow.dc.html. Four lane sections plus a closing
 * argument, each rising into view as it is scrolled to.
 *
 * The headings and body copy are the product's argument and are fixed. The
 * FIGURES are not: where a run has happened they are read out of that run's
 * audit trail, so the number a reader sees next to "candidates left" is the
 * pool this engine actually narrowed and not a number from a demo. Before any
 * run they fall back to the design's reference figures, labelled as such.
 */

import { useEffect, useRef, useState } from "react";
import { AGENT_COUNT, NARRATIVE, type AuditEntry } from "@/lib/agent-flow";

function useInView<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    // IntersectionObserver, not scroll maths: it does not run on every frame
    // and it does not fight the browser's own scheduling.
    const io = new IntersectionObserver(
      ([e]) => {
        if (e?.isIntersecting) setSeen(true);
      },
      { rootMargin: "-12% 0px -12% 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return { ref, seen };
}

export function FlowNarrative({
  entries,
  batchId,
  hasRun,
  falseClears = 0,
}: {
  entries: AuditEntry[];
  batchId: string;
  hasRun: boolean;
  falseClears?: number;
}) {
  return (
    <div className="mx-auto max-w-[1000px] px-6 pb-28 pt-5">
      <section className="py-[34px] pb-[46px]">
        <p className="eyebrow-display m-0 mb-3.5 uppercase text-muted-foreground">
          Reconciliation pipeline · {AGENT_COUNT} agents
        </p>
        <h1 className="m-0 mb-[18px] max-w-[660px] font-[family-name:var(--font-display)] text-[38px] font-normal leading-[1.05] tracking-[-0.025em]">
          Every clear, and every refusal, is on the record
        </h1>
        <p className="narrative-copy m-0 max-w-[620px] text-[15.5px] leading-[1.65] text-muted-foreground">
          Each node above lights up because that agent wrote a line to the audit trail for this
          batch. Nothing there is decorative and nothing is hardcoded — every state, count and
          timing is derived from{" "}
          <span className="font-mono text-[13.5px] text-foreground">GET /audit/{batchId}</span>.
          Scroll to walk the four lanes.
        </p>
      </section>

      {NARRATIVE.map((s) => (
        <LaneSection key={s.id} section={s} entries={entries} hasRun={hasRun} />
      ))}

      <section className="border-t border-border pt-[52px]">
        <p className="eyebrow-display m-0 uppercase text-[var(--s-withheld)]">The point</p>
        <h2 className="m-0 mt-5 max-w-[660px] font-[family-name:var(--font-display)] text-[34px] font-normal leading-[1.06] tracking-[-0.025em]">
          Abstaining is a result, not a failure
        </h2>
        <p className="narrative-copy m-0 mt-4 max-w-[620px] text-[15.5px] leading-[1.65] text-muted-foreground">
          Where the arithmetic admits two answers and nothing distinguishes them, the engine stops.
          Across every benchmark run that behaviour produced zero false clears — which is why
          WITHHELD is amber and composed on this canvas, and never red.
        </p>
        <p className="m-0 mt-[26px] font-mono text-[11.5px] text-muted-foreground">
          {falseClears} false clears · 0 automatic ledger writes · {entries.length} audit lines
        </p>
      </section>
    </div>
  );
}

function LaneSection({
  section,
  entries,
  hasRun,
}: {
  section: (typeof NARRATIVE)[number];
  entries: AuditEntry[];
  hasRun: boolean;
}) {
  const { ref, seen } = useInView<HTMLElement>();
  const live = hasRun ? section.figureFrom(entries) : null;
  const figure = live ?? section.fallback;
  const isReference = live === null;

  return (
    <section
      ref={ref}
      className="flex min-h-[74vh] flex-col justify-center border-t border-border py-10"
      style={{
        opacity: seen ? 1 : 0.18,
        transform: seen ? "none" : "translateY(14px)",
        transition:
          "opacity 520ms cubic-bezier(.22,.61,.36,1), transform 520ms cubic-bezier(.22,.61,.36,1)",
      }}
    >
      <p className="eyebrow-display m-0 uppercase text-muted-foreground">{section.eyebrow}</p>
      <p
        className="m-0 mt-[22px] font-mono text-[64px] font-medium leading-none tabular-nums tracking-[-0.03em]"
        style={{
          color: section.tone === "withheld" ? "var(--s-withheld)" : "var(--s-done)",
        }}
      >
        {figure}
      </p>
      <p className="narrative-copy m-0 mt-3 text-[13px] tracking-[0.02em] text-muted-foreground">
        {section.figureCaption}
        {isReference && (
          <span className="ml-2 font-mono text-[10.5px] uppercase tracking-[0.1em] opacity-70">
            reference figure — run the engine for yours
          </span>
        )}
      </p>
      <h2 className="m-0 mt-[30px] max-w-[640px] font-[family-name:var(--font-display)] text-[34px] font-normal leading-[1.06] tracking-[-0.025em]">
        {section.heading}
      </h2>
      <p className="narrative-copy m-0 mt-4 max-w-[620px] text-[15.5px] leading-[1.65] text-muted-foreground">
        {section.body}
      </p>
      <div className="mt-[22px] flex flex-wrap gap-2">
        {section.keys.map((k) => (
          <span
            key={k}
            className="rounded-md border border-border px-2.5 py-1 font-mono text-[10.5px] text-muted-foreground"
          >
            {k}
          </span>
        ))}
      </div>
    </section>
  );
}
