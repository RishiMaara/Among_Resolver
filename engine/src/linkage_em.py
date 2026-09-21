"""
Fellegi-Sunter record linkage: weights from data, not from judgement.

WHY
---
The linkage weights in linkage.py — 0.55 / 0.25 / 0.15 / 0.10 — were
reasoned, not fitted, and a sweep showed their exact values barely matter
where settlement ids are present (LINKAGE.md). Where ids are absent they have
nothing to say about the signal that matters most: TIMING. A gateway settles
on a cycle — Razorpay T+2 working days, the ReconRiver processor T+1 — so a
settlement's members were captured a fixed number of days before it. Which
number is a fact about the processor, not something to type in.

Fellegi-Sunter (1969) is the standard model for this, and the one Splink and
the US Census use. Every candidate is compared with the settlement on a few
signals; each level has an m-probability (how often a true member shows it)
and a u-probability (how often a non-member does); a candidate's match weight
is the sum of log2(m/u) in bits.

    anchor              reference names the settlement            yes / no
    ref_cluster         shares a reference token with others      yes / no
    ref_names_batch     that token also names the settlement      yes / no
    amount_peer         same amount appears in another feed       yes / no
    currency            same currency as the settlement           yes / no
    lag                 days from capture to settlement      0,1,2,3,4-7,other

WHERE m AND u COME FROM
-----------------------
u from the pool itself. m from whatever identifies members:

  * anchors in the pool — EM (Expectation-Maximisation) learns, from the
    records naming the settlement, how members differ on every other
    comparison, their capture lag included; records that lost their id but
    look like the anchored ones are lifted.
  * the settlement cycle learned from earlier VERIFIED clears
    (settlement_cycle.py) — for pools where nothing names the settlement.

With neither, it declines to fit. That is not caution for its own sake: a
two-component mixture over a single informative comparison is not
identifiable, and with references gone timing is the only one left. Tried
unsupervised on ReconRiver's stripped pools, EM collapsed to "no members" on
every one — FAILURE_LOG entry 19.

WHAT IT IS USED FOR, AND WHAT IT IS NOT
---------------------------------------
It ranks and it narrows. The best-supported cohort — whole comparison
patterns, highest weight first, until they cover the members the target
implies — becomes one more solving tier after the anchor tier. The arithmetic
still decides: a tier clears only if an exact subset sums to the target, and
every guard downstream is unchanged. What it finds on learned timing alone is
reported in its own confidence band, below the auto-clear gate, until enough
measured cases say otherwise.

EM runs on distinct comparison PATTERNS with counts, not rows: six small
features have at most a few hundred patterns, so a 200,000-row pool costs
what a 200-row one does.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

LAG_LEVELS = ("0", "1", "2", "3", "4-7", "other")
FEATURES: dict[str, tuple[str, ...]] = {
    "anchor": ("no", "yes"),
    "ref_cluster": ("no", "yes"),
    "ref_names_batch": ("no", "yes"),
    "amount_peer": ("no", "yes"),
    "currency": ("no", "yes"),
    "lag": LAG_LEVELS,
}

# A settlement is paid in one currency. That is structure, not a guess, so
# the currency comparison's m is fixed rather than estimated: left to EM, a
# pool of three currencies can be explained just as well by a "member"
# component that spans all of them.
M_CURRENCY = {"no": 0.01, "yes": 0.99}
M_ANCHOR = {"no": 0.10, "yes": 0.90}
SMOOTHING = 0.5          # Laplace pseudo-count per level
MAX_ITERATIONS = 200
TOLERANCE = 1e-7
LIKELY_MEMBER = 0.5      # posterior at which a candidate joins the learned tier


def lag_level(days: int) -> str:
    if 0 <= days <= 3:
        return str(days)
    if 4 <= days <= 7:
        return "4-7"
    return "other"


@dataclass
class FSModel:
    lam: float
    m: dict[str, dict[str, float]]
    u: dict[str, dict[str, float]]
    iterations: int
    converged: bool
    n: int
    patterns: int
    expected_members: float = 0.0
    notes: list[str] = field(default_factory=list)

    def weight(self, vector: dict[str, str]) -> float:
        """Match weight in bits: sum of log2(m/u) over the comparisons."""
        return sum(math.log2(self.m[f][vector[f]] / self.u[f][vector[f]]) for f in FEATURES)

    def posterior(self, vector: dict[str, str]) -> float:
        prior_bits = math.log2(self.lam / (1 - self.lam))
        bits = prior_bits + self.weight(vector)
        if bits > 60:
            return 1.0
        if bits < -60:
            return 0.0
        odds = 2.0 ** bits
        return odds / (1 + odds)

    def explain(self, top: int = 6) -> list[dict]:
        """The levels that moved the model most, largest |weight| first."""
        rows = []
        for f, levels in FEATURES.items():
            for lv in levels:
                m, u = self.m[f][lv], self.u[f][lv]
                rows.append({"comparison": f, "level": lv, "m": round(m, 4),
                             "u": round(u, 4), "weight_bits": round(math.log2(m / u), 2)})
        rows.sort(key=lambda r: abs(r["weight_bits"]), reverse=True)
        return rows[:top]

    def cohort(self, vectors: dict[str, dict[str, str]],
               expected_members: float) -> set[str]:
        """
        The best-supported group of candidates, by whole comparison pattern.

        Patterns are taken in descending match weight until they hold at least
        the expected member count, and only while they are more member-like
        than not (positive weight). Whole patterns, because records sharing a
        pattern are indistinguishable to the model; cutting between them would
        be the model pretending to know which. The arithmetic then decides
        which of the cohort, if any, compose the settlement.
        """
        groups: dict[tuple, list[str]] = {}
        for key, v in vectors.items():
            groups.setdefault(tuple(v[f] for f in FEATURES), []).append(key)
        ranked = sorted(groups.items(),
                        key=lambda kv: self.weight(dict(zip(FEATURES, kv[0]))),
                        reverse=True)
        chosen: set[str] = set()
        need = max(expected_members, 1.0)
        for pattern, keys in ranked:
            if self.weight(dict(zip(FEATURES, pattern))) <= 0:
                break
            chosen.update(keys)
            if len(chosen) >= need:
                break
        return chosen

    def summary(self) -> dict:
        return {
            "method": "Fellegi-Sunter, m and u estimated by EM on this pool",
            "prior_member_share": round(self.lam, 5),
            "expected_members": round(self.expected_members, 1),
            "pool": self.n,
            "patterns": self.patterns,
            "iterations": self.iterations,
            "converged": self.converged,
            "strongest_evidence": self.explain(),
            "notes": self.notes,
        }


def _normalise(counts: dict[str, float], levels: tuple[str, ...]) -> dict[str, float]:
    total = sum(counts.get(lv, 0.0) for lv in levels) + SMOOTHING * len(levels)
    return {lv: (counts.get(lv, 0.0) + SMOOTHING) / total for lv in levels}


def fit(vectors: list[dict[str, str]], expected_members: float | None = None,
        lag_m: dict[str, float] | None = None) -> FSModel | None:
    """
    Estimate lambda, m and u by EM over the pool's comparison vectors.

    EM can only separate members from non-members when something identifies
    them. A two-component mixture over a single informative comparison is not
    identifiable — any split that reproduces the pool's marginals fits it
    equally well — and a pool with no reference left has exactly one: timing.
    Measured on ReconRiver with references stripped: started from any
    neutral point, EM collapsed to "no members" on every pool.

    So the model needs one of two things to fit:

    * anchors in the pool — records naming the settlement identify members,
      and EM learns from them how members differ on everything else,
      including how many days they wait. Records whose reference was
      truncated but which match the members' pattern are then lifted.
    * a learned cycle (`lag_m`, from settlement_cycle.py) — the lag
      distribution of members in settlements that verifiably cleared. It is
      held fixed and EM estimates the rest.

    With neither it returns None: there is nothing to learn from, and the
    engine behaves exactly as it did without this model.

    `expected_members` seeds lambda — the target divided by a typical amount
    is a fair first guess at how many members to look for.
    """
    n = len(vectors)
    if n < 4:
        return None
    anchors_present = any(v["anchor"] == "yes" for v in vectors)
    if not anchors_present and not lag_m:
        return None

    patterns = Counter(tuple(v[f] for f in FEATURES) for v in vectors)
    keys = list(FEATURES)
    marginals = {f: Counter() for f in keys}
    for pat, c in patterns.items():
        for f, lv in zip(keys, pat):
            marginals[f][lv] += c
    u = {f: _normalise(marginals[f], FEATURES[f]) for f in keys}

    # Neutral start (m = u) except where there is evidence: anchors, the
    # fixed currency rule, and the learned cycle.
    m = {f: dict(u[f]) for f in keys}
    m["currency"] = dict(M_CURRENCY)
    if anchors_present:
        m["anchor"] = dict(M_ANCHOR)
    fixed = {"currency"}
    # A comparison every record answers the same way says nothing about
    # membership. Left in, its smoothing pseudo-counts still tilt the member
    # component away from the level everyone shares — measured: with four
    # such features, lambda collapsed to its floor on every stripped pool.
    constant = {f for f in keys if len(marginals[f]) <= 1}
    for f in constant:
        m[f] = dict(u[f])
    fixed |= constant
    if lag_m and not anchors_present:
        m["lag"] = {lv: lag_m.get(lv, 1e-6) for lv in LAG_LEVELS}
        fixed.add("lag")
    elif lag_m:
        m["lag"] = {lv: lag_m.get(lv, 1e-6) for lv in LAG_LEVELS}

    guess = expected_members if expected_members and expected_members > 0 else n * 0.05
    lam = min(max(guess / n, 1.0 / n), 0.5)

    converged, it = False, 0
    for it in range(1, MAX_ITERATIONS + 1):
        m_acc = {f: Counter() for f in keys}
        u_acc = {f: Counter() for f in keys}
        member_mass = 0.0
        for pat, c in patterns.items():
            a, b = lam, 1 - lam
            for f, lv in zip(keys, pat):
                a *= m[f][lv]
                b *= u[f][lv]
            w = a / (a + b) if (a + b) > 0 else 0.0
            member_mass += c * w
            for f, lv in zip(keys, pat):
                m_acc[f][lv] += c * w
                u_acc[f][lv] += c * (1 - w)
        # Without anchors nothing in the pool identifies lambda either — the
        # same identifiability limit — so it stays at the arithmetic's
        # estimate (target / typical amount) instead of drifting to zero.
        new_lam = (min(max(member_mass / n, 1.0 / n), 0.5) if anchors_present else lam)
        for f in keys:
            if f in constant:
                continue
            if f not in fixed:
                m[f] = _normalise(m_acc[f], FEATURES[f])
            if anchors_present:
                u[f] = _normalise(u_acc[f], FEATURES[f])
        if abs(new_lam - lam) < TOLERANCE:
            lam = new_lam
            converged = True
            break
        lam = new_lam

    model = FSModel(lam=lam, m=m, u=u, iterations=it, converged=converged, n=n,
                    patterns=len(patterns), expected_members=lam * n)
    model.notes.append("identified by anchors in the pool" if anchors_present
                       else "identified by the settlement cycle learned from earlier clears")
    if not converged:
        model.notes.append(f"EM stopped at {MAX_ITERATIONS} iterations without converging.")
    return model
