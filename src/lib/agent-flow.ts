/**
 * The agent graph, and how a live audit trail drives it.
 *
 * The point of this file is that node states are DERIVED, never authored.
 * A node lights up because that agent wrote a line to GET /audit/{batch_id}
 * during the run — the same trail a reviewer reads afterwards. A visualiser
 * that animates on a timer would look identical and prove nothing, so the
 * mapping from audit key to node is the whole contract.
 *
 * Ported from the design canvas at engine/Agent Flow.dc.html.
 * Lane/row coordinates, copy and state palette are kept as designed; the mock
 * trail is replaced by the real endpoint.
 */

export type NodeState = "idle" | "running" | "done" | "withheld" | "blocked" | "skipped";

export interface AuditEntry {
  timestamp_utc: string;
  batch_id: string;
  agent: string;
  detail: string;
}

export interface FlowNode {
  /** The `agent` string this node's audit entries carry. The join key. */
  k: string;
  lane: number;
  row: number;
  glyph: string;
  agent: string;
  title: string;
  /** Static fallback shown before a run supplies a real figure. */
  metric: string;
  blurb: string;
  /** LLM-assisted: proposes, never decides. */
  llm?: boolean;
  /** Linkage — given visual emphasis as the architectural claim. */
  key?: boolean;
}

export const LANES = [
  { id: "intake", label: "Lane 1 · Intake", x: 20 },
  { id: "constrain", label: "Lane 2 · Constrain", x: 304 },
  { id: "resolve", label: "Lane 3 · Resolve", x: 588 },
  { id: "report", label: "Lane 4 · Report", x: 872 },
] as const;

// Geometry taken verbatim from the design canvas.
export const GRAPH_W = 1098;
export const GRAPH_H = 446;
export const NODE_W = 206;
export const NODE_H = 74;
export const ROW_STEP = 108;
export const TOP = 38;
/** Port centre, measured from the node's top edge. */
export const PORT_Y = 37;
export const LANE_BAND_PAD = 14;

/** Execution order. `row` is the vertical slot inside the lane. */
export const NODES: FlowNode[] = [
  {
    k: "file_agent",
    lane: 0,
    row: 0,
    glyph: "0",
    agent: "Agent 0",
    title: "File Understanding",
    metric: "headers mapped",
    blurb:
      "Maps unpredictable CSV/JSON headers to the schema. Rejects a file outright when a field reconciliation depends on is absent.",
  },
  {
    k: "llm_header_mapper",
    lane: 0,
    row: 1,
    glyph: "0b",
    agent: "Agent 0b",
    llm: true,
    title: "LLM Header Mapping",
    metric: "—",
    blurb:
      "Gemini fallback, runs only when the rule-based mapper fails. Proposes; never decides. Skipping it is success.",
  },
  {
    k: "ingestion",
    lane: 0,
    row: 2,
    glyph: "1",
    agent: "Agent 1",
    title: "Ingestion & Normalize",
    metric: "rows normalized",
    blurb:
      "Amounts to integer paise, timestamps to UTC with a confidence flag, references to a canonical key.",
  },
  {
    k: "compliance_agent",
    lane: 1,
    row: 0,
    glyph: "7",
    agent: "Agent 7",
    title: "Compliance Screening",
    metric: "records screened",
    blurb: "12 published rules. Blocked records leave the pool before any matching runs.",
  },
  {
    k: "fee_decomposition",
    lane: 1,
    row: 1,
    glyph: "2",
    agent: "Agent 2",
    title: "Fee Decomposition",
    metric: "gross target",
    blurb:
      "Net settlement amount to the gross target the transactions summed to before fees and tax.",
  },
  {
    k: "settlement_window_filter",
    lane: 1,
    row: 2,
    glyph: "—",
    agent: "Window",
    title: "Settlement Window",
    metric: "in window",
    blurb: "Drops candidates outside the lookback window.",
  },
  {
    k: "linkage",
    lane: 1,
    row: 3,
    glyph: "2b",
    agent: "Agent 2b",
    title: "Linkage",
    metric: "candidates narrowed",
    key: true,
    blurb:
      "Narrows the candidate pool by identity before any arithmetic. The architectural claim of the engine.",
  },
  {
    k: "subset_sum",
    lane: 2,
    row: 0,
    glyph: "3",
    agent: "Agent 3",
    title: "Subset-Sum (CP-SAT)",
    metric: "exact solve",
    blurb: "Exact constraint solve for the subset summing to the target.",
  },
  {
    k: "tiebreak",
    lane: 2,
    row: 1,
    glyph: "—",
    agent: "Tiebreak",
    title: "Tiebreak",
    metric: "—",
    blurb:
      "Chooses between competing exact solutions, or declines to. Declining is a recorded outcome, not an error.",
  },
  {
    k: "fuzzy_match",
    lane: 2,
    row: 2,
    glyph: "4",
    agent: "Agent 4",
    llm: true,
    title: "Fuzzy / Semantic Match",
    metric: "—",
    blurb: "Runs only when Agent 3 fails to clear. Never auto-clears below threshold.",
  },
  {
    k: "exception_diagnosis",
    lane: 2,
    row: 3,
    glyph: "5",
    agent: "Agent 5",
    title: "Exception Diagnosis",
    metric: "root causes",
    blurb: "Assigns a root-cause category so a reviewer does not re-investigate from scratch.",
  },
  {
    k: "orchestrator",
    lane: 3,
    row: 0,
    glyph: "6",
    agent: "Agent 6",
    title: "Orchestrator / Governance",
    metric: "0 ledger writes",
    blurb: "Enforces the non-negotiable: nothing writes to a ledger automatically. Proposes only.",
  },
  {
    k: "tie_out",
    lane: 3,
    row: 1,
    glyph: "—",
    agent: "Tie-out",
    title: "Tie-out",
    metric: "—",
    blurb: "Proves matched gross − deductions − net == 0.",
  },
  {
    k: "cash_position",
    lane: 3,
    row: 2,
    glyph: "8",
    agent: "Agent 8",
    title: "Cash Position",
    metric: "buckets",
    blurb:
      "Buckets plus a balanced double-entry journal proposal. No proposal is generated unless the batch cleared.",
  },
  {
    k: "settlement_qa",
    lane: 3,
    row: 3,
    glyph: "9",
    agent: "Agent 9",
    llm: true,
    title: "Settlement Q&A",
    metric: "grounded",
    blurb: "Answers questions grounded strictly in recorded results.",
  },
];

export interface FlowEdge {
  a: string;
  b: string;
  /** Records crossing this edge; drives stroke width and the midpoint label. */
  n: number;
  /** A branch the run did not take — stays dashed and dim. */
  untaken?: boolean;
}

export const EDGES: FlowEdge[] = [
  { a: "file_agent", b: "llm_header_mapper", n: 0, untaken: true },
  { a: "file_agent", b: "ingestion", n: 0 },
  { a: "ingestion", b: "compliance_agent", n: 0 },
  { a: "compliance_agent", b: "fee_decomposition", n: 0 },
  { a: "fee_decomposition", b: "settlement_window_filter", n: 0 },
  { a: "settlement_window_filter", b: "linkage", n: 0 },
  { a: "linkage", b: "subset_sum", n: 0 },
  { a: "subset_sum", b: "tiebreak", n: 0 },
  { a: "tiebreak", b: "fuzzy_match", n: 0, untaken: true },
  { a: "tiebreak", b: "exception_diagnosis", n: 0 },
  { a: "exception_diagnosis", b: "orchestrator", n: 0 },
  { a: "orchestrator", b: "tie_out", n: 0, untaken: true },
  { a: "tie_out", b: "cash_position", n: 0, untaken: true },
  { a: "orchestrator", b: "cash_position", n: 0 },
  { a: "cash_position", b: "settlement_qa", n: 0 },
];

export const NODE_BY_KEY = new Map(NODES.map((n) => [n.k, n]));
export const NODE_ORDER = NODES.map((n) => n.k);

/** Geometry. Lanes are columns; rows stack inside them. */
export function nodePos(n: FlowNode) {
  const lane = LANES[n.lane] ?? LANES[0];
  return { x: lane.x, y: TOP + n.row * ROW_STEP };
}

export function edgePath(a: FlowNode, b: FlowNode): string {
  const pa = nodePos(a);
  const pb = nodePos(b);
  const x1 = pa.x + NODE_W;
  const y1 = pa.y + PORT_Y;
  const x2 = pb.x;
  const y2 = pb.y + PORT_Y;
  // Horizontal departure and arrival, like n8n. The control offset scales
  // with horizontal distance so same-lane hops do not loop absurdly.
  const dx = Math.max(36, Math.abs(x2 - x1) * 0.5);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

/**
 * States for every node, derived from the entries seen so far.
 *
 * Rules, in order:
 *   - an agent with entries is `done`, or `running` if it is the most recent
 *   - `withheld` / `blocked` are read from what the entry actually SAYS,
 *     because those are the outcomes that must never be mistaken for errors
 *   - an agent with no entries is `idle` while the run is live and `skipped`
 *     once it has finished — skipping is frequently the correct outcome
 *     (the LLM mapper never running means the rules sufficed)
 */
export function deriveStates(entries: AuditEntry[], running: boolean): Record<string, NodeState> {
  const seen = new Map<string, AuditEntry[]>();
  for (const e of entries) {
    const list = seen.get(e.agent);
    if (list) list.push(e);
    else seen.set(e.agent, [e]);
  }

  const lastAgent = entries.at(-1)?.agent ?? null;
  const states: Record<string, NodeState> = {};

  for (const node of NODES) {
    const mine = seen.get(node.k);
    if (!mine || mine.length === 0) {
      // "Not needed" is a claim about a run that HAPPENED. Before anything has
      // run there is no such claim to make, so an empty trail leaves every
      // node waiting rather than asserting the pipeline skipped itself.
      states[node.k] = running || entries.length === 0 ? "idle" : "skipped";
      continue;
    }

    const text = mine
      .map((e) => e.detail)
      .join(" ")
      .toLowerCase();

    // Parse the COUNT, never the word. "0 transaction(s) blocked" contains
    // "blocked" and was painting a clean compliance screen red — the exact
    // inversion this palette exists to avoid.
    const blockedCount = Number(text.match(/(\d+)\s+transaction\(s\)\s+blocked/)?.[1] ?? 0);
    if (node.k === "compliance_agent" && blockedCount > 0) {
      states[node.k] = "blocked";
    } else if (text.includes("withheld") || text.includes("refusing to auto-clear")) {
      states[node.k] = "withheld";
    } else if (running && node.k === lastAgent) {
      states[node.k] = "running";
    } else {
      states[node.k] = "done";
    }
  }
  return states;
}

/** The live figure for a node, pulled from its most recent audit line. */
export function deriveMetrics(entries: AuditEntry[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const e of entries) {
    const node = NODE_BY_KEY.get(e.agent);
    if (!node) continue;
    const next = summariseDetail(e.agent, e.detail);
    if (next) out[e.agent] = next;
  }
  return out;
}

/**
 * Compress one audit line to a figure that fits on a card.
 *
 * Deliberately regex over the engine's own phrasing rather than a parallel
 * numeric API: the trail is the record of what happened, and reading it is
 * what keeps the card honest. When nothing matches, the card keeps its
 * static label rather than inventing a number.
 */
function summariseDetail(agent: string, detail: string): string | null {
  const num = (re: RegExp): string | null => detail.match(re)?.[1] ?? null;

  switch (agent) {
    case "linkage": {
      const pool = detail.match(/Pool\s+([\d,]+)\s*->\s*([\d,]+)/i);
      if (pool) return `${pool[1] ?? "?"} → ${pool[2] ?? "?"} candidates`;
      if (/refusing to auto-clear/i.test(detail)) return "WITHHELD FOR REVIEW";
      const anchors = num(/(\d+)\s+transaction\(s\) reference/i);
      if (anchors) return `${anchors} anchored`;
      if (/empty candidate pool/i.test(detail)) return "no candidates in window";
      if (/no .* candidate is linked/i.test(detail)) return "nothing linked";
      return null;
    }
    case "settlement_window_filter": {
      const m = detail.match(/([\d,]+)\s+candidates?\s*->\s*([\d,]+)/i);
      return m?.[2] ? `${m[2]} in window` : null;
    }
    case "fee_decomposition": {
      const g = num(/gross_target=(\d+)c/i);
      if (!g) return null;
      const inr = (Number(g) / 100).toLocaleString("en-IN", {
        maximumFractionDigits: 2,
      });
      return `₹${inr} gross target`;
    }
    case "subset_sum": {
      const c = num(/(\d+)\s+transactions?\s+sum/i);
      if (c) return `${c} txns sum to target`;
      if (/no subset|no candidate survived/i.test(detail)) return "no exact subset";
      return null;
    }
    case "tiebreak":
      return /withheld|refus/i.test(detail) ? "WITHHELD FOR REVIEW" : null;
    case "exception_diagnosis": {
      const c = num(/(\d+)\s+exception/i);
      return c ? `${c} diagnosed` : null;
    }
    case "compliance_agent": {
      const b = num(/(\d+)\s+transaction\(s\)?\s+blocked/i);
      if (b) return `${b} blocked`;
      const s = num(/Screening\s+([\d,]+)/i);
      return s ? `${s} screened` : null;
    }
    case "tie_out": {
      const r = num(/residual\s+(-?\d+)c/i);
      return r !== null ? `residual ${r}c` : null;
    }
    case "orchestrator":
      return /ledger/i.test(detail) ? "0 ledger writes" : null;
    case "file_agent": {
      if (/REJECTED/.test(detail)) return "FILE REJECTED";
      const rows = num(/(\d+)\s+row\(s\) parsed/i);
      return rows ? `${Number(rows).toLocaleString("en-IN")} rows parsed` : null;
    }
    case "ingestion": {
      const m = detail.match(/(\d+)\s+of\s+(\d+)\s+row/i);
      return m ? `${Number(m[1]).toLocaleString("en-IN")} rows normalized` : null;
    }
    case "cash_position": {
      const b = num(/(\d+)\s+bucket/i);
      return b ? `${b} buckets` : null;
    }
    default:
      return null;
  }
}

export const STATE_LABEL: Record<NodeState, string> = {
  idle: "Waiting",
  running: "Running",
  done: "Done",
  withheld: "Withheld for review",
  blocked: "Blocked",
  skipped: "Not needed",
};

/**
 * `withheld` is amber and composed, never red and never alarming.
 *
 * Abstaining on an ambiguous batch is what produces zero false clears. If the
 * visualiser renders it as a failure, the demo argues against the product.
 */
export const STATE_VAR: Record<NodeState, string> = {
  idle: "var(--s-idle)",
  running: "var(--s-running)",
  done: "var(--s-done)",
  withheld: "var(--s-withheld)",
  blocked: "var(--s-blocked)",
  skipped: "var(--s-skipped)",
};

/**
 * Path length of an edge, for the draw-in animation.
 *
 * stroke-dashoffset animation needs the length up front. Measuring the real
 * SVG path would mean a DOM read per edge per frame; a flattened-bezier
 * approximation is within a few percent and costs nothing, and the only thing
 * riding on it is how far a dash travels.
 */
export function edgeLength(a: FlowNode, b: FlowNode): number {
  const pa = nodePos(a);
  const pb = nodePos(b);
  const x1 = pa.x + NODE_W;
  const y1 = pa.y + PORT_Y;
  const x2 = pb.x;
  const y2 = pb.y + PORT_Y;
  const dx = Math.max(36, Math.abs(x2 - x1) * 0.5);
  const pts: Array<[number, number]> = [];
  for (let i = 0; i <= 12; i++) {
    const t = i / 12;
    const u = 1 - t;
    const x =
      u * u * u * x1 + 3 * u * u * t * (x1 + dx) + 3 * u * t * t * (x2 - dx) + t * t * t * x2;
    const y = u * u * u * y1 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y2;
    pts.push([x, y]);
  }
  let len = 0;
  for (let i = 1; i < pts.length; i++) {
    const p0 = pts[i - 1]!;
    const p1 = pts[i]!;
    len += Math.hypot(p1[0] - p0[0], p1[1] - p0[1]);
  }
  return Math.round(len);
}

/** Midpoint of an edge, where its record count is labelled. */
export function edgeMid(a: FlowNode, b: FlowNode) {
  const pa = nodePos(a);
  const pb = nodePos(b);
  return {
    x: (pa.x + NODE_W + pb.x) / 2,
    y: (pa.y + pb.y) / 2 + PORT_Y,
  };
}

/**
 * Records crossing each edge, read out of the trail.
 *
 * The one number a judge should take away is the narrowing at Linkage — an
 * edge arriving thick and leaving thin. Deriving it from the audit line
 * rather than hardcoding it is what makes the picture an argument rather
 * than an illustration.
 */
export function edgeCounts(entries: AuditEntry[]): Record<string, number> {
  const out: Record<string, number> = {};
  const put = (a: string, b: string, n: number) => {
    if (Number.isFinite(n)) out[`${a}->${b}`] = n;
  };
  const digits = (v: string | undefined) => (v ? Number(v.replace(/,/g, "")) : NaN);

  for (const e of entries) {
    if (e.agent === "settlement_window_filter") {
      const m = e.detail.match(/([\d,]+)\s+candidates?\s*->\s*([\d,]+)/i);
      if (m) {
        put("fee_decomposition", "settlement_window_filter", digits(m[1]));
        put("settlement_window_filter", "linkage", digits(m[2]));
      }
    }
    if (e.agent === "linkage") {
      const m = e.detail.match(/Pool\s+([\d,]+)\s*->\s*([\d,]+)/i);
      if (m) put("linkage", "subset_sum", digits(m[2]));
    }
    if (e.agent === "ingestion") {
      const m = e.detail.match(/(\d+)\s+of\s+(\d+)\s+row/i);
      if (m) {
        const n = digits(m[1]);
        put("file_agent", "ingestion", n);
        put("ingestion", "compliance_agent", n);
      }
    }
    if (e.agent === "compliance_agent") {
      const m = e.detail.match(/(\d+)\s+transaction\(s\)\s+blocked,\s+(\d+)\s+remain/i);
      if (m) put("compliance_agent", "fee_decomposition", digits(m[2]));
    }
  }
  return out;
}

/**
 * Agents, as distinct from NODES.
 *
 * The canvas draws 15 boxes but the design counts 12 agents, and the
 * difference is not cosmetic: Window, Tiebreak and Tie-out are STAGES inside
 * other agents, not agents in their own right. Counting boxes would overstate
 * the system to a judge, which is the one direction this project must not err
 * in.
 */
export const AGENT_KEYS = NODES.filter((n) => /^Agent/.test(n.agent)).map((n) => n.k);
export const AGENT_COUNT = AGENT_KEYS.length;

/** The scroll narrative under the canvas. Copy is from the design. */
export interface NarrativeSection {
  id: string;
  eyebrow: string;
  figureCaption: string;
  heading: string;
  body: string;
  keys: string[];
  tone: "done" | "withheld";
  /** Which audit line supplies the live figure, when a run has happened. */
  figureFrom: (entries: AuditEntry[]) => string | null;
  fallback: string;
}

const firstMatch = (entries: AuditEntry[], agent: string, re: RegExp) => {
  for (const e of entries) {
    if (e.agent !== agent) continue;
    const m = e.detail.match(re);
    if (m) return m;
  }
  return null;
};

export const NARRATIVE: NarrativeSection[] = [
  {
    id: "intake",
    eyebrow: "Lane 1 · Intake",
    figureCaption: "rows read across gateway, bank and ERP",
    heading: "Three feeds, one schema, before anything is trusted",
    body: "The file agent maps unpredictable headers by rule and rejects a file outright when a field reconciliation depends on is missing. The LLM mapper sat this batch out — the rules were sufficient, which is the outcome you want.",
    keys: ["file_agent", "llm_header_mapper", "ingestion"],
    tone: "done",
    fallback: "50,000",
    figureFrom: (es) => {
      let total = 0;
      for (const e of es) {
        if (e.agent !== "ingestion") continue;
        const m = e.detail.match(/(\d+)\s+of\s+(\d+)\s+row/i);
        if (m?.[1]) total += Number(m[1]);
      }
      return total ? total.toLocaleString("en-IN") : null;
    },
  },
  {
    id: "constrain",
    eyebrow: "Lane 2 · Constrain",
    figureCaption: "candidates left, after narrowing by identity",
    heading: "Narrow by identity before you narrow by arithmetic",
    body: "Compliance pulls blocked records out of the pool before matching. Fees are decomposed to a gross target. Then Linkage cuts the pool on identity alone — the thick edge arriving and the hairline leaving is the architectural argument in one picture.",
    keys: ["compliance_agent", "fee_decomposition", "settlement_window_filter", "linkage"],
    tone: "done",
    fallback: "55",
    figureFrom: (es) => {
      const m = firstMatch(es, "linkage", /Pool\s+([\d,]+)\s*->\s*([\d,]+)/i);
      return m?.[2] ?? null;
    },
  },
  {
    id: "resolve",
    eyebrow: "Lane 3 · Resolve",
    figureCaption: "transactions in the matched set",
    heading: "The arithmetic verifies; it does not identify",
    body: "Subset-sum runs over the narrowed pool, where the answer is actually determined. Where two disjoint sets both sum exactly to the target, Tiebreak has no discriminator and declines rather than guessing — and the batch is withheld.",
    keys: ["subset_sum", "tiebreak", "fuzzy_match", "exception_diagnosis"],
    tone: "withheld",
    fallback: "2",
    figureFrom: (es) => {
      const m = firstMatch(es, "subset_sum", /(\d+)\s+transactions?\s+sum/i);
      return m?.[1] ?? null;
    },
  },
  {
    id: "report",
    eyebrow: "Lane 4 · Report",
    figureCaption: "automatic ledger writes, ever",
    heading: "It reports, proposes, and waits",
    body: "Governance recorded the outcome and proposed nothing. Tie-out proves matched gross minus deductions minus net is zero. Cash position tells the controller where the money sits, and Q&A answers only from what was recorded.",
    keys: ["orchestrator", "tie_out", "cash_position", "settlement_qa"],
    tone: "done",
    fallback: "0",
    figureFrom: () => "0",
  },
];
