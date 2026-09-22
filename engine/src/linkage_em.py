"""
Fellegi-Sunter record linkage: weights from data, not judgement.

Each candidate is compared with the settlement on a few signals (anchor,
ref_cluster, ref_names_batch, amount_peer, currency, lag in days); each
level has m (members) and u (non-members) probabilities, and the match
weight is the sum of log2(m/u). u comes from the pool; m from anchors in the
pool (EM) or from the settlement cycle learned from verified clears
(settlement_cycle.py). With neither it declines: one informative comparison
is not identifiable, and unsupervised EM collapsed on every stripped pool
(FAILURE_LOG 19). It ranks and narrows into one more solving tier; the
arithmetic and every gate still decide. EM runs over distinct patterns with
counts, so pool size barely matters.
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

    Needs anchors in the pool (members identify themselves and EM learns the
    rest, lag included) or a learned cycle (`lag_m`, held fixed). With neither
    it returns None and the engine behaves as without the model.
    `expected_members` (target / typical amount) seeds lambda.
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
