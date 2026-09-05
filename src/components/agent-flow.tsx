/**
 * The agent flow canvas — a port of engine/Agent Flow.dc.html.
 *
 * Node states are DERIVED from the live audit trail, never authored. A node
 * lights up because that agent wrote a decision to GET /audit/{batch_id}
 * during the run. An animation on a timer would look identical and prove
 * nothing, so the join from audit key to node is the whole contract.
 *
 * The scrubber replays the same trail. Dragging it backwards re-derives every
 * node from a prefix of the entries, so the replay cannot drift from what
 * actually happened — there is no second source of truth to disagree with.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AGENT_COUNT,
  AGENT_KEYS,
  EDGES,
  GRAPH_H,
  GRAPH_W,
  LANES,
  NODE_BY_KEY,
  NODE_H,
  NODE_W,
  NODES,
  PORT_Y,
  STATE_LABEL,
  STATE_VAR,
  deriveMetrics,
  deriveStates,
  edgeCounts,
  edgeLength,
  edgeMid,
  edgePath,
  nodePos,
  type AuditEntry,
  type FlowNode,
  type NodeState,
} from "@/lib/agent-flow";

interface Props {
  entries: AuditEntry[];
  running: boolean;
  batchId: string;
  statusLabel: string;
  statusTone: "cleared" | "withheld" | "blocked" | "idle";
  elapsedText: string;
  onOpenReport?: () => void;
  hasReport?: boolean;
  runError?: string | null;
}

const SPEEDS = [0.5, 1, 2] as const;

export function AgentFlow({
  entries,
  running,
  batchId,
  statusLabel,
  statusTone,
  elapsedText,
  onOpenReport,
  hasReport,
  runError,
}: Props) {
  const [selected, setSelected] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [playhead, setPlayhead] = useState(-1);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<number>(1);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [wrapW, setWrapW] = useState(0);

  // While a run is live the canvas always shows the newest state. The scrubber
  // only takes over once the run has finished, so a replay can never fight
  // with incoming data.
  const live = running || playhead < 0;
  const visible = useMemo(
    () => (live ? entries : entries.slice(0, playhead + 1)),
    [entries, live, playhead],
  );

  const midReplay = !live && playhead < entries.length - 1;
  const states = useMemo(
    () => deriveStates(visible, running || midReplay),
    [visible, running, midReplay],
  );
  const metrics = useMemo(() => deriveMetrics(visible), [visible]);

  // How long each agent held the pipeline, measured from its own audit
  // timestamps. Shown on the card so a viewer sees WHERE the time went, not
  // only that the whole run was fast.
  const elapsedByAgent = useMemo(() => {
    const first = new Map<string, number>();
    const last = new Map<string, number>();
    for (const e of visible) {
      const t = Date.parse(e.timestamp_utc);
      if (Number.isNaN(t)) continue;
      if (!first.has(e.agent)) first.set(e.agent, t);
      last.set(e.agent, t);
    }
    const out: Record<string, number> = {};
    for (const [k, a] of first) out[k] = Math.max(0, (last.get(k) ?? a) - a);
    return out;
  }, [visible]);
  const counts = useMemo(() => edgeCounts(visible), [visible]);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver((es) => {
      const w = es[0]?.contentRect.width;
      if (typeof w === "number") setWrapW(w);
    });
    ro.observe(el);
    setWrapW(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    if (!playing || running) return;
    const id = window.setInterval(() => {
      setPlayhead((p) => {
        if (p >= entries.length - 1) {
          setPlaying(false);
          return p;
        }
        return p + 1;
      });
    }, 90 / speed);
    return () => window.clearInterval(id);
  }, [playing, speed, entries.length, running]);

  // Fit-to-width, but never below the point where a node card stops being
  // readable. Squeezing 1098px into a 375px phone gives a 0.34 scale and 70px
  // cards — a picture of a pipeline rather than a pipeline you can read. Below
  // the clamp the canvas scrolls horizontally instead, which keeps the node
  // text legible and lets the reader pan along the four lanes.
  // The legend and the hint sit ON the canvas, absolutely positioned in the
  // bottom corners. Sizing the canvas to exactly the scaled graph therefore
  // parks them on top of the last row of nodes. The design avoids this by
  // giving the canvas its own viewport-relative height and fitting the graph
  // into it; reserving the strip explicitly is the same idea, said out loud.
  const LEGEND_STRIP = 46;
  const MIN_SCALE = 0.62;
  const scale = wrapW ? Math.max(MIN_SCALE, Math.min(1, wrapW / GRAPH_W)) : 1;
  const canvasH = Math.round(GRAPH_H * scale) + LEGEND_STRIP;
  const scrolls = wrapW > 0 && wrapW < GRAPH_W * scale;
  const activeKey = visible.at(-1)?.agent ?? null;

  const counters = useMemo(() => {
    const seen = new Set(visible.map((e) => e.agent));
    const agentsRun = AGENT_KEYS.filter((k) => seen.has(k)).length;

    let records = 0;
    for (const e of visible) {
      if (e.agent !== "ingestion") continue;
      const m = e.detail.match(/(\d+)\s+of\s+(\d+)\s+row/i);
      if (m?.[1]) records += Number(m[1]);
    }
    const exceptions = visible.filter((e) => e.agent === "exception_diagnosis").length;

    return [
      { label: "Agents run", value: `${agentsRun} / ${AGENT_COUNT}`, tone: "" },
      { label: "Records", value: records.toLocaleString("en-IN"), tone: "" },
      {
        label: "Exceptions",
        value: String(exceptions),
        tone: exceptions > 0 ? "var(--s-withheld)" : "",
      },
    ];
  }, [visible]);

  const replay = useCallback(() => {
    setPlayhead(-1);
    setPlaying(true);
  }, []);

  const selectedNode = selected ? NODE_BY_KEY.get(selected) : null;
  const hoveredNode = hovered ? NODE_BY_KEY.get(hovered) : null;
  const hoveredDetail = hovered
    ? [...visible].reverse().find((e) => e.agent === hovered)?.detail
    : null;

  const toneVar =
    statusTone === "cleared"
      ? "var(--s-done)"
      : statusTone === "withheld"
        ? "var(--s-withheld)"
        : statusTone === "blocked"
          ? "var(--s-blocked)"
          : "var(--muted-foreground)";

  return (
    <div className="af-anim flex flex-col overflow-hidden rounded-2xl border border-border bg-card">
      {/* ── batch header ────────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-border px-4 py-2.5">
        <div className="flex items-center gap-2">
          <span className="font-mono text-[12.5px] font-medium">{batchId}</span>
          <span
            className="rounded-full border px-2 py-[3px] font-mono text-[9.5px] uppercase tracking-[0.1em]"
            style={{
              borderColor: `color-mix(in oklab, ${toneVar} 40%, transparent)`,
              background: `color-mix(in oklab, ${toneVar} 12%, transparent)`,
              color: toneVar,
            }}
          >
            {statusLabel}
          </span>
        </div>
        <div className="flex items-baseline gap-1.5">
          <span className="label-ui text-[10px] uppercase text-muted-foreground">Elapsed</span>
          <span className="font-mono text-[12.5px] tabular-nums">{elapsedText}</span>
        </div>
        <div className="ml-auto flex gap-5">
          {counters.map((c) => (
            <div key={c.label} className="flex min-w-[74px] flex-col gap-px">
              <span className="label-ui text-[10px] uppercase text-muted-foreground">
                {c.label}
              </span>
              <span
                className="font-mono text-[15px] tabular-nums"
                style={c.tone ? { color: c.tone } : undefined}
              >
                {c.value}
              </span>
            </div>
          ))}
        </div>
      </div>

      {/* ── canvas ──────────────────────────────────────────────────────── */}
      <div
        ref={wrapRef}
        className={`relative ${scrolls ? "overflow-x-auto overflow-y-hidden" : "overflow-hidden"}`}
        style={{
          height: canvasH,
          // All long-hand. React warns when a shorthand (`background`) and a
          // long-hand (`backgroundSize`) for the same property are both
          // updated across a rerender, because apply order is not guaranteed
          // — and this element rerenders on every audit poll.
          backgroundColor: "var(--canvas)",
          backgroundImage: "radial-gradient(var(--dot) 1px, transparent 1px)",
          backgroundSize: "22px 22px",
        }}
      >
        <div style={{ width: GRAPH_W * scale, height: GRAPH_H * scale, position: "relative" }}>
          <div
            style={{
              position: "absolute",
              left: 0,
              top: 0,
              width: GRAPH_W,
              height: GRAPH_H,
              transformOrigin: "0 0",
              transform: `scale(${scale})`,
            }}
          >
            <svg
              width={GRAPH_W}
              height={GRAPH_H}
              viewBox={`0 0 ${GRAPH_W} ${GRAPH_H}`}
              style={{ position: "absolute", inset: 0, overflow: "visible" }}
              aria-hidden="true"
            >
              {LANES.map((lane) => (
                <rect
                  key={lane.id}
                  x={lane.x - 14}
                  y={6}
                  width={NODE_W + 28}
                  height={434}
                  rx={14}
                  fill="var(--band)"
                  stroke="var(--band-stroke)"
                  strokeWidth={1}
                />
              ))}
              {EDGES.map((e) => {
                const a = NODE_BY_KEY.get(e.a);
                const b = NODE_BY_KEY.get(e.b);
                if (!a || !b) return null;
                const sa = states[e.a] ?? "idle";
                const sb = states[e.b] ?? "idle";
                const srcDone = sa === "done" || sa === "withheld" || sa === "blocked";

                // Three distinct edge lives, and the middle one is the whole
                // animation: as a node starts working, the edge feeding it DRAWS
                // ITSELF from source to target. Once both ends are done it
                // settles into a slow dash-march so live routes read as carrying
                // traffic while untaken ones are visibly still.
                const active = srcDone && sb === "running";
                const settled = srcDone && (sb === "done" || sb === "withheld" || sb === "blocked");
                const untaken = !!e.untaken;

                const col =
                  sb === "withheld"
                    ? "var(--s-withheld)"
                    : sb === "blocked"
                      ? "var(--s-blocked)"
                      : "var(--s-done)";
                const n = counts[`${e.a}->${e.b}`];
                // Volume is legible in the stroke itself: an edge arriving thick
                // and leaving thin at Linkage is the architectural claim, drawn.
                const w = !n ? 1.2 : n >= 10000 ? 3 : n >= 1000 ? 2.2 : n >= 100 ? 1.6 : 1.2;
                const d = edgePath(a, b);
                const len = edgeLength(a, b);

                return (
                  <path
                    key={`${e.a}->${e.b}`}
                    d={d}
                    fill="none"
                    stroke={untaken ? "var(--s-idle)" : active || settled ? col : "var(--s-idle)"}
                    strokeWidth={untaken ? 1.2 : w}
                    strokeLinecap="round"
                    opacity={untaken ? 0.3 : active || settled ? 0.9 : 0.45}
                    strokeDasharray={
                      untaken ? "4 5" : settled ? "10 6" : active ? `${len} ${len}` : undefined
                    }
                    style={
                      settled
                        ? { animation: "af-dashmarch 3s linear infinite" }
                        : active
                          ? ({
                              ["--len" as string]: `${len}px`,
                              animation: "af-drawin 450ms cubic-bezier(.22,.61,.36,1) both",
                            } as React.CSSProperties)
                          : undefined
                    }
                  />
                );
              })}
              {running &&
                EDGES.filter((e) => e.b === activeKey && !e.untaken).map((e) => {
                  const a = NODE_BY_KEY.get(e.a);
                  const b = NODE_BY_KEY.get(e.b);
                  if (!a || !b) return null;
                  const d = edgePath(a, b);
                  return (
                    <g key={`pk-${e.a}-${e.b}`}>
                      <circle r={7} fill="var(--s-running)" opacity={0.16}>
                        <animateMotion
                          dur="0.7s"
                          repeatCount="indefinite"
                          path={d}
                          begin="-0.05s"
                        />
                      </circle>
                      <circle r={2.6} fill="var(--s-running)" opacity={0.45}>
                        <animateMotion
                          dur="0.7s"
                          repeatCount="indefinite"
                          path={d}
                          begin="-0.09s"
                        />
                      </circle>
                      <circle
                        r={3.4}
                        fill="var(--s-running)"
                        style={{ filter: "drop-shadow(0 0 7px var(--s-running))" }}
                      >
                        <animateMotion dur="0.7s" repeatCount="indefinite" path={d} />
                      </circle>
                    </g>
                  );
                })}
            </svg>

            {LANES.map((lane) => (
              <div
                key={lane.id}
                style={{ position: "absolute", left: lane.x, top: 12 }}
                className="text-[10px] font-medium uppercase tracking-[0.22em] text-muted-foreground"
              >
                {lane.label}
              </div>
            ))}

            {NODES.map((n) => (
              <NodeCard
                key={n.k}
                node={n}
                state={states[n.k] ?? "idle"}
                metric={metrics[n.k] ?? n.metric}
                active={activeKey === n.k && running}
                elapsedMs={elapsedByAgent[n.k]}
                dimmed={!!selected && selected !== n.k}
                selected={selected === n.k}
                hovered={hovered === n.k}
                onSelect={() => setSelected(n.k === selected ? null : n.k)}
                onHover={(v) => setHovered(v ? n.k : null)}
              />
            ))}

            {EDGES.map((e) => {
              const a = NODE_BY_KEY.get(e.a);
              const b = NODE_BY_KEY.get(e.b);
              const n = counts[`${e.a}->${e.b}`];
              if (!a || !b || !n || e.untaken) return null;

              const sa = states[e.a] ?? "idle";
              const sb = states[e.b] ?? "idle";
              const srcDone = sa === "done" || sa === "withheld" || sa === "blocked";
              // A count is only true once the stage that produced it has
              // reported, so the label appears with the traffic rather than
              // ahead of it.
              if (!srcDone) return null;

              const live =
                sb === "running" || sb === "done" || sb === "withheld" || sb === "blocked";
              const colour =
                sb === "withheld"
                  ? "var(--s-withheld)"
                  : sb === "blocked"
                    ? "var(--s-blocked)"
                    : live
                      ? "var(--s-done)"
                      : "var(--muted-foreground)";
              const m = edgeMid(a, b);
              return (
                <span
                  key={`lbl-${e.a}-${e.b}`}
                  style={{
                    position: "absolute",
                    left: m.x,
                    top: m.y - 11,
                    transform: "translate(-50%,-50%)",
                    backgroundColor: "var(--canvas)",
                    color: colour,
                    animation: "af-rise 260ms ease both",
                  }}
                  className="pointer-events-none rounded-[5px] px-[5px] py-[2px] font-mono text-[12.5px] font-medium leading-none tabular-nums"
                >
                  {n.toLocaleString("en-IN")}
                </span>
              );
            })}

            {hoveredNode && hoveredDetail && (
              <div
                style={{
                  position: "absolute",
                  left: Math.min(nodePos(hoveredNode).x, GRAPH_W - 280),
                  top: nodePos(hoveredNode).y + NODE_H + 8,
                  width: 264,
                  zIndex: 9,
                  animation: "af-rise 160ms ease both",
                }}
                className="pointer-events-none rounded-[9px] border border-border bg-popover px-[11px] py-[9px] shadow-xl"
              >
                <p className="mb-1 font-mono text-[9.5px] uppercase tracking-[0.1em] text-muted-foreground">
                  {hoveredNode.k}
                </p>
                <p className="text-[11.5px] leading-[1.45]">{hoveredDetail}</p>
              </div>
            )}
          </div>
        </div>

        <div className="pointer-events-none absolute bottom-2.5 left-3 hidden flex-wrap items-center gap-3 rounded-lg border border-border bg-card/80 px-2.5 py-1.5 backdrop-blur sm:flex">
          {(["running", "done", "withheld", "blocked", "skipped"] as NodeState[]).map((s) => (
            <span
              key={s}
              className="flex items-center gap-1.5 font-mono text-[9px] uppercase tracking-[0.1em] text-muted-foreground"
            >
              <span className="h-[7px] w-[7px] rounded-sm" style={{ background: STATE_VAR[s] }} />
              {STATE_LABEL[s]}
            </span>
          ))}
        </div>
        <span className="pointer-events-none absolute bottom-3 right-3 hidden font-mono text-[9px] uppercase tracking-[0.12em] text-muted-foreground sm:block">
          Click a node for its definition
        </span>
      </div>

      {/* ── transport ───────────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-3 border-t border-border px-4 py-2.5">
        <button
          type="button"
          onClick={() => setPlaying((p) => !p)}
          disabled={running || entries.length === 0}
          className="rounded-[9px] bg-primary px-3 py-[7px] text-[12.5px] font-medium text-primary-foreground disabled:opacity-40"
        >
          {playing
            ? "Pause"
            : playhead >= 0 && playhead < entries.length - 1
              ? "Resume"
              : "Run trail"}
        </button>
        <button
          type="button"
          onClick={replay}
          disabled={running || entries.length === 0}
          className="rounded-[9px] border border-border px-3 py-[7px] text-[12.5px] disabled:opacity-40"
        >
          Replay
        </button>
        {hasReport && (
          <button
            type="button"
            onClick={onOpenReport}
            className="rounded-[9px] border px-3 py-[7px] text-[12.5px]"
            style={{
              borderColor: "color-mix(in oklab, var(--s-withheld) 40%, transparent)",
              background: "color-mix(in oklab, var(--s-withheld) 10%, transparent)",
              color: "var(--s-withheld)",
            }}
          >
            Report
          </button>
        )}
        <div className="flex overflow-hidden rounded-[9px] border border-border">
          {SPEEDS.map((sp) => (
            <button
              key={sp}
              type="button"
              disabled={running || entries.length === 0}
              title={`Replay the audit trail at ${sp}x`}
              onClick={() => {
                setSpeed(sp);
                // Picking a speed while nothing is playing used to do
                // nothing at all: the control looked live, changed colour,
                // and had no effect until Run trail was pressed separately.
                // A speed control that does not start the thing it paces is
                // indistinguishable from a broken one, so it starts it.
                if (!playing && entries.length > 0) {
                  if (playhead < 0 || playhead >= entries.length - 1) setPlayhead(-1);
                  setPlaying(true);
                }
              }}
              style={{
                background: speed === sp ? "var(--muted)" : "transparent",
                color: speed === sp ? "var(--foreground)" : "var(--muted-foreground)",
              }}
              className="border-0 px-2.5 py-1.5 font-mono text-[11px] disabled:opacity-40"
            >
              {sp}×
            </button>
          ))}
        </div>
        <span className="hidden font-mono text-[10.5px] text-muted-foreground md:inline">
          ◇ LLM-assisted — proposes, never decides
        </span>
        {runError && (
          <span className="flex items-center gap-1.5 text-[12px] text-destructive">
            <span>⚠</span>
            <span>{runError}</span>
          </span>
        )}
        <span className="ml-auto font-mono text-[10.5px] text-muted-foreground">
          {live
            ? `idle · ${entries.length} audit entries loaded`
            : `entry ${playhead + 1} / ${entries.length} · ${entries[playhead]?.agent ?? ""}`}
        </span>
      </div>

      {/* ── scrubber ────────────────────────────────────────────────────── */}
      <div className="bg-card px-4 pb-3 pt-1">
        <div className="relative h-[26px]">
          <div className="absolute left-0 right-0 top-3 h-[2px] bg-border" />
          <div
            className="absolute left-0 top-3 h-[2px] bg-accent"
            style={{
              width: `${entries.length ? ((live ? entries.length : playhead + 1) / entries.length) * 100 : 0}%`,
            }}
          />
          {entries.map((e, i) => {
            const st = states[e.agent];
            return (
              <span
                key={i}
                style={{
                  position: "absolute",
                  left: `${(i / Math.max(1, entries.length - 1)) * 100}%`,
                  top: st === "withheld" ? 6 : 8,
                  width: 1.5,
                  height: st === "withheld" ? 14 : 10,
                  transform: "translateX(-0.75px)",
                  background:
                    st === "withheld"
                      ? "var(--s-withheld)"
                      : st === "blocked"
                        ? "var(--s-blocked)"
                        : "color-mix(in oklab, var(--muted-foreground) 45%, transparent)",
                }}
              />
            );
          })}
          <input
            type="range"
            min={-1}
            max={Math.max(0, entries.length - 1)}
            step={1}
            value={live ? entries.length - 1 : playhead}
            onChange={(ev) => {
              setPlaying(false);
              setPlayhead(Number(ev.target.value));
            }}
            disabled={running || entries.length === 0}
            aria-label="Scrub the audit trail"
            className="absolute inset-x-0 top-0 h-[26px] w-full cursor-pointer opacity-0"
          />
        </div>
      </div>

      {selectedNode && (
        <NodeDrawer
          node={selectedNode}
          state={states[selectedNode.k] ?? "idle"}
          entries={visible.filter((e) => e.agent === selectedNode.k)}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}

function NodeCard({
  node,
  state,
  metric,
  active,
  elapsedMs,
  dimmed,
  selected,
  hovered,
  onSelect,
  onHover,
}: {
  node: FlowNode;
  state: NodeState;
  metric: string;
  active: boolean;
  elapsedMs?: number | undefined;
  dimmed: boolean;
  selected: boolean;
  hovered: boolean;
  onSelect: () => void;
  onHover: (v: boolean) => void;
}) {
  const pos = nodePos(node);
  const colour = STATE_VAR[state];
  const dim = state === "idle" ? 0.82 : state === "skipped" ? 0.5 : 1;

  // Depth carries state. A running node is ringed and glowing; a withheld one
  // wears a soft amber halo, deliberately calm, because abstaining is a
  // result; a done one just sits with an ordinary card shadow.
  const shadow =
    state === "running"
      ? "0 0 0 1.5px color-mix(in oklab, var(--s-running) 60%, transparent), 0 0 28px -6px var(--s-running)"
      : state === "withheld"
        ? "0 0 0 3px color-mix(in oklab, var(--s-withheld) 16%, transparent), 0 2px 4px rgb(0 0 0 / 0.14)"
        : state === "done"
          ? "0 1px 2px rgb(0 0 0 / 0.10), 0 10px 26px -18px rgb(0 0 0 / 0.5)"
          : "none";

  const lift = selected ? "translateY(-3px) scale(1.015)" : hovered ? "translateY(-2px)" : "none";

  const label =
    state === "skipped"
      ? "not needed"
      : state === "idle"
        ? "\u2014"
        : state === "running"
          ? "working\u2026"
          : metric;

  const portColour = state === "idle" || state === "skipped" ? "var(--s-idle)" : colour;

  return (
    <div
      style={{
        position: "absolute",
        left: pos.x,
        top: pos.y,
        width: NODE_W,
        height: NODE_H,
        // Selecting one node recedes the others, so the drawer's subject is
        // obvious without anything moving.
        opacity: dimmed ? 0.42 : 1,
        transition: "opacity 320ms ease",
      }}
    >
      <button
        type="button"
        onClick={onSelect}
        onMouseEnter={() => onHover(true)}
        onMouseLeave={() => onHover(false)}
        onFocus={() => onHover(true)}
        onBlur={() => onHover(false)}
        aria-label={`${node.agent} ${node.title} — ${STATE_LABEL[state]}`}
        style={{
          width: NODE_W,
          height: NODE_H,
          boxSizing: "border-box",
          background: "var(--card)",
          borderWidth: 1,
          borderLeftWidth: 2,
          borderColor: "var(--border)",
          borderLeftColor: colour,
          borderStyle: state === "skipped" ? "dashed" : "solid",
          opacity: dim,
          outline: `1.5px solid ${selected ? colour : "transparent"}`,
          outlineOffset: 3,
          boxShadow: shadow,
          transform: lift,
          animation: active ? "af-pulse 1.3s ease-in-out infinite" : undefined,
          transition:
            "transform 200ms cubic-bezier(.22,.61,.36,1), box-shadow 160ms ease, outline-color 200ms ease, opacity 260ms ease",
        }}
        className="group relative overflow-hidden rounded-[11px] text-left"
      >
        {active && (
          <span aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
            <span
              className="absolute inset-y-0 w-[44%]"
              style={{
                background:
                  "linear-gradient(90deg,transparent,color-mix(in oklab, var(--s-running) 20%, transparent),transparent)",
                animation: "af-shimmer 1.4s linear infinite",
              }}
            />
          </span>
        )}
        <div className="relative flex items-center gap-[7px] px-[9px] pt-[7px]">
          <span
            className="flex size-5 flex-none items-center justify-center rounded-md font-mono text-[10px] font-semibold"
            style={{
              background: `color-mix(in oklab, ${colour} 16%, transparent)`,
              border: `1px solid color-mix(in oklab, ${colour} 34%, transparent)`,
              color: colour,
              animation: active ? "af-spin 3.2s linear infinite" : undefined,
            }}
          >
            {node.glyph}
          </span>
          <span className="whitespace-nowrap font-mono text-[9.5px] uppercase tracking-[0.14em] text-muted-foreground">
            {node.agent}
          </span>
          <span className="ml-auto flex items-center gap-[5px]">
            {node.llm && (
              <span className="whitespace-nowrap rounded border border-border px-1 py-[2px] font-mono text-[9px] leading-none text-muted-foreground">
                ◇ LLM
              </span>
            )}
            {!!elapsedMs && (
              <span className="font-mono text-[10px] tabular-nums text-muted-foreground">
                {elapsedMs}ms
              </span>
            )}
          </span>
        </div>
        <div className="relative truncate px-[9px] pt-1 text-[13px] font-semibold tracking-[-0.01em]">
          {node.title}
        </div>
        <div
          className="relative truncate px-[9px] pt-[2px] font-mono text-[10.5px] tabular-nums"
          style={{
            color:
              state === "withheld"
                ? "var(--s-withheld)"
                : state === "done"
                  ? "var(--foreground)"
                  : "var(--muted-foreground)",
            // The figure rises in as it lands, so a value appearing reads as
            // an event rather than a silent swap.
            animation:
              state === "done" || state === "withheld" ? "af-rise 320ms ease both" : undefined,
          }}
        >
          {label}
        </div>
        <span
          aria-hidden="true"
          className="absolute size-[7px] rounded-full"
          style={{
            left: -4,
            top: PORT_Y - 4,
            background: "var(--canvas)",
            border: `1.5px solid ${portColour}`,
          }}
        />
        <span
          aria-hidden="true"
          className="absolute size-[7px] rounded-full"
          style={{
            right: -4,
            top: PORT_Y - 4,
            background: "var(--canvas)",
            border: `1.5px solid ${portColour}`,
          }}
        />
      </button>
    </div>
  );
}

function NodeDrawer({
  node,
  state,
  entries,
  onClose,
}: {
  node: FlowNode;
  state: NodeState;
  entries: AuditEntry[];
  onClose: () => void;
}) {
  const colour = STATE_VAR[state];
  return (
    <div className="border-t border-border bg-card px-4 py-4">
      <div className="flex items-start gap-2.5">
        <div className="flex-1">
          <p className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
            {node.agent}
          </p>
          <h3 className="mt-1 text-[17px] font-semibold tracking-[-0.015em]">{node.title}</h3>
          <p className="mt-1.5 font-mono text-[11px] text-muted-foreground">agent: {node.k}</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close node details"
          className="size-7 flex-none rounded-md border border-border text-[15px] text-muted-foreground"
        >
          ✕
        </button>
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <span
          className="rounded-full border px-2 py-[3px] font-mono text-[9.5px] uppercase tracking-[0.1em]"
          style={{
            borderColor: `color-mix(in oklab, ${colour} 40%, transparent)`,
            background: `color-mix(in oklab, ${colour} 12%, transparent)`,
            color: colour,
          }}
        >
          {STATE_LABEL[state]}
        </span>
        <span className="rounded-full border border-border px-2 py-[3px] font-mono text-[9.5px] uppercase tracking-[0.1em] text-muted-foreground">
          {entries.length} audit {entries.length === 1 ? "line" : "lines"}
        </span>
        {node.llm && (
          <span className="rounded-full border border-border px-2 py-[3px] font-mono text-[9.5px] uppercase tracking-[0.1em] text-muted-foreground">
            ◇ proposes, never decides
          </span>
        )}
      </div>
      <p className="mt-3 max-w-3xl text-[12.5px] leading-[1.55] text-muted-foreground">
        {node.blurb}
      </p>
      <div className="mt-3 max-h-64 overflow-y-auto pr-1">
        {entries.length === 0 ? (
          <div className="rounded-[10px] border border-dashed border-border p-4 text-center">
            <p className="text-[12.5px] text-muted-foreground">
              No audit lines. This agent was not needed for this batch — the rules ahead of it were
              sufficient.
            </p>
          </div>
        ) : (
          entries.map((e, i) => (
            <div key={i} className="relative border-l-[1.5px] border-border pb-3.5 pl-3.5">
              <span
                className="absolute size-[7px] rounded-full"
                style={{ left: -4.5, top: 5, background: colour }}
              />
              <p className="font-mono text-[10px] text-muted-foreground">
                {e.timestamp_utc.slice(11, 23)} UTC
              </p>
              <p className="mt-1 text-[12.5px] leading-[1.5]">{e.detail}</p>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
