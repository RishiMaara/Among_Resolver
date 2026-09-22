"""
What a confidence figure has actually been worth.

Each result carries `calibrated_confidence`: the raw figure through an
isotonic (monotone) map fitted on the benchmark and judged on ReconRiver
(scripts/fit_calibration.py). The auto-clear gate still reads the raw figure,
which is the one with a record at the gate. Reviewer confirmations are
recorded per band and shown beside the map (GET /calibration); a divergence
is reported, never silently refitted.
"""

from __future__ import annotations

import bisect
import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass

import audit

logger = logging.getLogger(__name__)
MAP_PATH = os.path.join(os.path.dirname(__file__), "calibration_map.json")
BANDS = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 0.93), (0.93, 1.01)]
_REDIS_KEY = "reviewer_outcomes"
_lock = threading.Lock()
_memory: list[dict] = []


# ── isotonic regression, pool-adjacent-violators ──────────────────────────

@dataclass
class IsotonicMap:
    xs: list[float]          # block upper edges (raw confidence), ascending
    ys: list[float]          # calibrated value for each block, non-decreasing
    fitted_on: int = 0
    source: str = ""

    def __call__(self, x: float) -> float:
        """Interpolated between fitted blocks, flat beyond the ends."""
        if not self.xs:
            return x
        if x <= self.xs[0]:
            return self.ys[0]
        if x >= self.xs[-1]:
            return self.ys[-1]
        i = bisect.bisect_left(self.xs, x)
        x0, x1, y0, y1 = self.xs[i - 1], self.xs[i], self.ys[i - 1], self.ys[i]
        return round(y0 + (y1 - y0) * (x - x0) / (x1 - x0), 4)

    def to_dict(self) -> dict:
        return {"xs": self.xs, "ys": self.ys, "fitted_on": self.fitted_on, "source": self.source}


def fit(pairs: list[tuple[float, bool]], source: str = "") -> IsotonicMap:
    """Monotone non-decreasing fit of correct ~ confidence (PAV)."""
    # Tied confidences pooled first. The engine emits a handful of discrete
    # values, so ties are the rule; left as separate blocks, a lookup at 0.05
    # returned whichever single outcome happened to sort first.
    tied: dict[float, list[float]] = {}
    for c, ok in pairs:
        t = tied.setdefault(round(float(c), 6), [0.0, 0.0])
        t[0] += 1.0 if ok else 0.0
        t[1] += 1.0
    blocks: list[list[float]] = []            # [sum_y, count, max_x]
    for x in sorted(tied):
        blocks.append([tied[x][0], tied[x][1], x])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, n, mx = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += n
            blocks[-1][2] = mx
    # Laplace-smoothed, (right + 1) / (n + 2): seven right out of seven is
    # good evidence, not proof, and a calibrated 1.0 beside a proposal the
    # engine withheld would read as "certain". Smoothing can dent monotonicity
    # where block sizes differ, so a running maximum restores it.
    ys, top = [], 0.0
    for s_, n, _ in blocks:
        top = max(top, (s_ + 1) / (n + 2))
        ys.append(round(top, 4))
    return IsotonicMap(xs=[b[2] for b in blocks], ys=ys, fitted_on=len(pairs), source=source)


def ece(pairs: list[tuple[float, bool]], mapping=None) -> float:
    """Expected calibration error over BANDS, of the raw or mapped figure."""
    f = mapping or (lambda c: c)
    total = len(pairs) or 1
    err = 0.0
    for lo, hi in BANDS:
        inb = [(f(c), ok) for c, ok in pairs if lo <= c < hi]
        if inb:
            said = sum(p for p, _ in inb) / len(inb)
            right = sum(1 for _, ok in inb if ok) / len(inb)
            err += len(inb) / total * abs(right - said)
    return round(err, 4)


_SHIPPED: IsotonicMap | None = None


def shipped() -> IsotonicMap | None:
    """The map fitted by scripts/fit_calibration.py, if present."""
    global _SHIPPED
    if _SHIPPED is None and os.path.exists(MAP_PATH):
        try:
            with open(MAP_PATH, encoding="utf-8") as f:
                d = json.load(f)
            _SHIPPED = IsotonicMap(d["xs"], d["ys"], d.get("fitted_on", 0), d.get("source", ""))
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("Calibration map unreadable (%s); raw confidence only.", exc)
    return _SHIPPED


def calibrated(confidence: float) -> float | None:
    m = shipped()
    return m(confidence) if m else None


# ── reviewer outcomes ─────────────────────────────────────────────────────

def _shared():
    return audit._get_redis() if audit.redis_url() else None


def _db():
    try:
        conn = audit._get_db()
    except Exception:
        return None
    if conn is None:
        return None
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS reviewer_outcomes ("
                     " batch_id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        conn.commit()
    except sqlite3.Error:
        return None
    return conn


def record_outcome(batch_id: str, confidence: float, confirmed: bool, reviewer: str) -> None:
    """A person's verdict on a proposed set. The latest verdict per batch counts."""
    body = json.dumps({"confidence": float(confidence), "confirmed": bool(confirmed),
                       "reviewer": reviewer})
    with _lock:
        shared = _shared()
        if shared is not None:
            try:
                shared.hset(_REDIS_KEY, batch_id, body)
                return
            except Exception:
                pass
        conn = _db()
        if conn is not None:
            try:
                conn.execute("INSERT OR REPLACE INTO reviewer_outcomes VALUES (?, ?)", (batch_id, body))
                conn.commit()
                return
            except sqlite3.Error:
                pass
        _memory[:] = [m for m in _memory if m.get("batch_id") != batch_id]
        _memory.append({"batch_id": batch_id, **json.loads(body)})


def outcomes() -> list[dict]:
    shared = _shared()
    if shared is not None:
        try:
            return [json.loads(v) for v in shared.hgetall(_REDIS_KEY).values()]
        except Exception:
            pass
    conn = _db()
    if conn is not None:
        try:
            return [json.loads(r[0]) for r in conn.execute("SELECT body FROM reviewer_outcomes")]
        except sqlite3.Error:
            pass
    return list(_memory)


def _reset_for_tests() -> None:
    _memory.clear()
    conn = _db()
    if conn is not None:
        conn.execute("DELETE FROM reviewer_outcomes")
        conn.commit()


def report() -> dict:
    """Per band: what the shipped map predicts, and what reviewers decided."""
    m = shipped()
    rows = outcomes()
    bands = []
    for lo, hi in BANDS:
        inb = [r for r in rows if lo <= r["confidence"] < hi]
        mid = (lo + min(hi, 1.0)) / 2
        bands.append({
            "band": f"[{lo:.2f}, {min(hi, 1.0):.2f})",
            "shipped_calibrated_at_midpoint": m(mid) if m else None,
            "reviewer_decisions": len(inb),
            "reviewers_confirmed": sum(1 for r in inb if r["confirmed"]),
            "reviewer_agreement": (round(sum(1 for r in inb if r["confirmed"]) / len(inb), 4)
                                   if inb else None),
        })
    drift = [b["band"] for b in bands
             if b["reviewer_decisions"] >= 20 and b["shipped_calibrated_at_midpoint"] is not None
             and abs(b["reviewer_agreement"] - b["shipped_calibrated_at_midpoint"]) > 0.15]
    return {
        "map": m.to_dict() if m else None,
        "reviewer_decisions": len(rows),
        "bands": bands,
        "drift": drift,
        "plain": (("Reviewers and the shipped calibration disagree by more than 15 points in "
                   + ", ".join(drift) + ". The map is out of date for this data; refit it with "
                   "scripts/fit_calibration.py.") if drift else
                  "No band has 20 reviewer decisions that disagree with the shipped calibration."),
    }
