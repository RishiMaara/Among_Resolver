# Design artifacts

The design canvases this app's UI is ported from.

- `Agent Flow.dc.html` — the reconciliation screen: upload, the n8n-style
  agent canvas, transport controls, scrubber and node drawer.
- `Compliance Rulebook.dc.html` — the published rulebook screen.
- `support.js` — the design canvas runtime these files load.

These are the **source of truth for the visual design**, not build inputs:
nothing imports them. The React port lives in `src/components/agent-flow.tsx`
and `src/lib/agent-flow.ts`, and the geometry there (`NODE_W` 206, `NODE_H` 74,
`ROW_STEP` 108, `TOP` 38, lane x-offsets, the state palette) is taken from
these files verbatim so the two do not drift.

The one deliberate divergence: the canvas ships with a mock audit trail so it
can be previewed standalone. The port replaces that with the live
`GET /audit/{batch_id}` endpoint. Node states are derived from real entries —
a node lights up because an agent recorded a decision, not because a timer
fired.
