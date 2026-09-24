"""
Linkage: which transactions are plausibly connected to this settlement.

Subset-sum cannot IDENTIFY a settlement: 2^n subsets compete for ~2e6
distinct paise values, and arithmetic alone scored 0.0% auto-clear on 120
benchmark scenarios. Identification is entity resolution (a reference naming
the settlement, the same payment in two feeds, clustering); the sum then
VERIFIES the linked set. Blocking is high-recall (a dropped member is
unrecoverable); precision comes from scoring and the sum. Confidence is
calibrated against outcomes (link_confidence).
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

import linkage_em
from schema import NormalizedTxn, SettlementBatch

# A token shorter than this carries no identifying power — "1", "AB" match
# everything. Blocking on them would defeat the purpose.
MIN_TOKEN_LEN = 4

# Candidate cap per settlement. Subset-sum is exponential; letting an
# unbounded block through would reintroduce the 2^n problem this module
# exists to remove.
MAX_CANDIDATES = 400


def txn_key(t: NormalizedTxn) -> str:
    """
    A transaction's identity: "{feed}:{id}". Ids are unique per feed, not
    across feeds (gateway "1001" and ERP "1001" are different records), and
    keying by bare id once lent one record another's anchor.
    """
    return f"{t.source.value}:{t.source_txn_id}"


def members_of(result, pool: list[NormalizedTxn]) -> list[NormalizedTxn]:
    """
    The records a MatchResult names, found in `pool`.

    By txn_key when the result carries keys, which every solve now does: a
    gateway payment "1001" and an ERP line "1001" are two records, and a
    bare-id lookup returned both. Measured: a correct clear reported "does
    not tie" by the other record's amount, and its fee audit read that
    record (FAILURE_LOG 38). Falls back to bare ids only for a result built
    without keys.
    """
    keys = set(getattr(result, "matched_keys", None) or [])
    if keys:
        return [t for t in pool if txn_key(t) in keys]
    ids = set(getattr(result, "matched_txn_ids", None) or [])
    return [t for t in pool if t.source_txn_id in ids]


# Linkage weights.
#
# Named constants rather than literals inside link_score() because a number
# nothing can vary is a number nothing can measure. These were reasoned from
# how forgeable each signal is — a settlement id is near-conclusive, a shared
# amount is weak alone because unrelated payments collide on round figures —
# and NOT fitted to data. That was listed as a gap in LINKAGE.md, and
# scripts/sweep_linkage_weights.py is what checks whether the reasoning
# produced good numbers. Overridable so that sweep does not have to edit source.
from dynamic_weights import get_dynamic_weights

_weights = get_dynamic_weights()
W_SETTLEMENT_ID = _weights["W_SETTLEMENT_ID"]
W_SHARED_REF_TOKEN = _weights["W_SHARED_REF_TOKEN"]
W_REF_PREFIX_CLUSTER = _weights["W_REF_PREFIX_CLUSTER"]
W_CROSS_SOURCE_AMOUNT = _weights["W_CROSS_SOURCE_AMOUNT"]
W_OUT_OF_WINDOW_PENALTY = _weights["W_OUT_OF_WINDOW_PENALTY"]


@dataclass
class LinkSignals:
    """Why a transaction is considered connected to the settlement."""
    shared_ref_token: bool = False
    ref_prefix_cluster: bool = False
    settlement_id_match: bool = False
    cross_source_amount_peer: bool = False
    in_window: bool = True

    def score(self) -> float:
        """
        Weighted linkage strength in [0, 1].

        Weights are ordered by how forgeable each signal is. A settlement-ID
        match is near-conclusive; sharing an amount with another source is
        weak on its own (many payments share round amounts) but meaningful
        in combination.
        """
        s = 0.0
        if self.settlement_id_match:
            s += W_SETTLEMENT_ID
        if self.shared_ref_token:
            s += W_SHARED_REF_TOKEN
        if self.ref_prefix_cluster:
            s += W_REF_PREFIX_CLUSTER
        if self.cross_source_amount_peer:
            s += W_CROSS_SOURCE_AMOUNT
        if not self.in_window:
            s -= W_OUT_OF_WINDOW_PENALTY
        return max(0.0, min(1.0, s))


@dataclass
class LinkedCandidate:
    txn: NormalizedTxn
    signals: LinkSignals
    score: float
    # Probability this record is a member, from the Fellegi-Sunter model EM
    # fitted to this pool (linkage_em.py). 0.0 when the model is off or the
    # pool was too small to learn from.
    em_posterior: float = 0.0


@dataclass
class LinkageResult:
    candidates: list[NormalizedTxn]
    scored: list[LinkedCandidate]
    pool_before: int
    pool_after: int
    method: str
    reasoning: str
    # Bare ids, for humans: audit lines, reports, tests.
    anchor_cluster_ids: list[str] = field(default_factory=list)
    # Collision-safe identities, for code. Callers deciding whether a specific
    # transaction is anchored MUST use this — see txn_key.
    anchor_keys: set[str] = field(default_factory=set)
    # Records the learned model rates more likely members than not.
    learned_keys: set[str] = field(default_factory=set)
    # What the model learned, for the report and the audit trail.
    em: dict | None = None

    @property
    def reduction_factor(self) -> float:
        if self.pool_after == 0:
            return 0.0
        return self.pool_before / self.pool_after


_TOKEN_RE = re.compile(r"[A-Za-z]+|\d+")


def tokenize_ref(ref: str) -> set[str]:
    """
    Split a reference into comparable tokens.

    Real references are composites — "STL0042_LEG3", "RZP-88121/A",
    "UTR20260818X99". The identifying part is usually one token inside,
    not the whole string, which is why exact-string matching alone misses
    legitimate links.
    """
    if not ref:
        return set()
    return {t.upper() for t in _TOKEN_RE.findall(ref) if len(t) >= MIN_TOKEN_LEN}


def _is_identifier_token(tok: str) -> bool:
    """
    Can this token ANCHOR, i.e. is it an identifier rather than a word? Real
    references carry digits; batch ids often carry words ("SETTLE", "BATCH",
    "near") that would anchor unrelated records. Word tokens still add cluster
    signal; they cannot anchor.
    """
    return any(ch.isdigit() for ch in tok)


def _settlement_tokens(batch: SettlementBatch) -> set[str]:
    """Tokens usable to ANCHOR a transaction to this settlement."""
    return {t for t in tokenize_ref(batch.batch_id) if _is_identifier_token(t)}


def canonical_key(s: str) -> str:
    """Alphanumeric, uppercased — the form ingestion stores references in."""
    return "".join(ch for ch in (s or "") if ch.isalnum()).upper()


# A canonical settlement id shorter than this is not identifying enough to
# anchor on by containment — "B1" appears inside countless references.
MIN_CANONICAL_ANCHOR_LEN = 6


def _contains_identifier(haystack: str, needle: str) -> bool:
    """
    Does `haystack` contain `needle` as a WHOLE identifier? A digit next to the
    match means the number continues, so "SETTLE1" does not anchor "SETTLE10"
    or "SETTLE100"; a letter is fine ("SETTLE1ORDER001"). Padded synthetic ids
    hide this; production ids are often unpadded.
    """
    if not needle or not haystack:
        return False

    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        before_ok = not (needle[0].isdigit()
                         and start > 0 and haystack[start - 1].isdigit())
        after_ok = not (needle[-1].isdigit()
                        and end < len(haystack) and haystack[end].isdigit())
        if before_ok and after_ok:
            return True
        start = haystack.find(needle, start + 1)
    return False


_SEPARATED = re.compile(r"[0-9A-Za-z]+")


def _raw_ref(t: NormalizedTxn) -> str:
    """
    The reference as the source wrote it — only while it is still the same
    reference. Code that rewrites ref_id_canonical (a benchmark stripping the
    settlement id, the blind re-solve swapping in the order id) leaves a raw
    copy that still names what was removed, and the first run of this check
    read it: ReconRiver's "references stripped" condition rose from 24% to
    95%, because nothing had been stripped from the copy. A raw reference
    that no longer canonicalises to the canonical one is ignored.
    """
    raw = str((t.extra or {}).get("ref_raw") or "")
    same = re.sub(r"[^A-Za-z0-9]", "", raw).upper() == (t.ref_id_canonical or "").upper()
    return raw if raw and same else ""


def _names_by_its_separators(raw_ref: str, canon: str) -> bool:
    """
    Does the reference, as written, name the id as whole separator-delimited
    pieces? Canonical form glues a payee code to a numeric invoice number
    ("VNDE960709B38-4277809164"); the source's separator says where the id ends
    (glued, 15 of 51 vendors were found; readable, every one). "SETTLE-10-ORD" does not name
    "SETTLE-1".
    """
    if not raw_ref:
        return False
    parts = [p.upper() for p in _SEPARATED.findall(raw_ref)]
    for i in range(len(parts)):
        joined = ""
        for part in parts[i:]:
            joined += part
            if joined == canon:
                return True
            if len(joined) >= len(canon):
                break
    return False


def _canonical_anchor_hits(batch: SettlementBatch, pool: list[NormalizedTxn]) -> set[str]:
    """
    Transactions whose reference contains the settlement id as a whole
    identifier, compared in canonical (separator-free) form; returns txn_keys.
    Token intersection alone found zero anchors on ReconRiver, whose batch ids
    carry separators its references do not.
    """
    canon = canonical_key(batch.batch_id)
    if len(canon) < MIN_CANONICAL_ANCHOR_LEN:
        return set()
    return {
        txn_key(t) for t in pool
        if _contains_identifier(canonical_key(t.ref_id_canonical), canon)
        or _names_by_its_separators(_raw_ref(t), canon)
    }


def build_candidate_links(
    batch: SettlementBatch,
    pool: list[NormalizedTxn],
    settlement_window_days: int = 5,
    max_candidates: int = MAX_CANDIDATES,
) -> LinkageResult:
    """
    Narrow `pool` to the transactions plausibly connected to `batch`.

    Returns every input transaction unchanged when no linkage signal exists
    anywhere in the pool — degrading to the previous behaviour rather than
    silently discarding candidates on data that carries no references. That
    matters: on a feed with no usable reference fields this module must be a
    no-op, not a shredder.
    """
    if not pool:
        return LinkageResult([], [], 0, 0, "empty", "Empty candidate pool.")

    window = timedelta(days=settlement_window_days)
    stmt_tokens = _settlement_tokens(batch)

    # ── index the pool once ───────────────────────────────────────────────
    by_token: dict[str, list[NormalizedTxn]] = defaultdict(list)
    by_amount: dict[int, list[NormalizedTxn]] = defaultdict(list)
    for t in pool:
        for tok in tokenize_ref(t.ref_id_canonical):
            by_token[tok].append(t)
        by_amount[t.amount_cents].append(t)

    # ── signal 1: transactions naming the settlement directly ─────────────
    signals: dict[str, LinkSignals] = defaultdict(LinkSignals)
    direct_hits: set[str] = set()

    # Containment on canonical forms first — separator-agnostic, so it works
    # whatever punctuation the source system used.
    for key in _canonical_anchor_hits(batch, pool):
        signals[key].settlement_id_match = True
        direct_hits.add(key)

    # Token match as a second path: catches a reference carrying only part of
    # the settlement id (a batch number without its prefix, say).
    for tok in stmt_tokens:
        for t in by_token.get(tok, ()):
            signals[txn_key(t)].settlement_id_match = True
            direct_hits.add(txn_key(t))

    # ── signal 2: reference-token clusters ────────────────────────────────
    #
    # A settlement's legs usually share a token with EACH OTHER even when
    # none of them names the settlement (the batch ref lives only in the
    # bank file, say). Clusters that are small relative to the pool are
    # informative; a token shared by half the pool is not a reference, it is
    # boilerplate, so it is ignored.
    cluster_limit = max(2, min(60, len(pool) // 8))
    for tok, members in by_token.items():
        if 2 <= len(members) <= cluster_limit:
            for t in members:
                signals[txn_key(t)].shared_ref_token = True
                if tok in stmt_tokens:
                    signals[txn_key(t)].ref_prefix_cluster = True

    # ── signal 3: cross-source amount peers ───────────────────────────────
    #
    # The same payment appearing in two feeds is the single most reliable
    # non-reference link available, and it is what a human reconciler looks
    # for first.
    for amt, members in by_amount.items():
        if len(members) < 2:
            continue
        sources = {m.source for m in members}
        if len(sources) > 1:
            for t in members:
                signals[txn_key(t)].cross_source_amount_peer = True

    # ── window flag ───────────────────────────────────────────────────────
    for t in pool:
        signals[txn_key(t)].in_window = (
            abs(t.timestamp_utc - batch.settled_at_utc) <= window
        )

    posteriors, em_summary, cohort = _learned_posteriors(batch, pool, signals)
    scored = [
        LinkedCandidate(txn=t, signals=signals[txn_key(t)],
                        score=signals[txn_key(t)].score(),
                        em_posterior=posteriors.get(txn_key(t), 0.0))
        for t in pool
    ]
    # The learned posterior breaks ties within a score. Without it the cap
    # below kept whichever equal-scored records came first in the file, and
    # in a pool with no references almost every record scores the same.
    scored.sort(key=lambda c: (c.score, c.em_posterior), reverse=True)
    learned_keys = cohort

    # A record the learned model rates a likely member is linked even with no
    # hand-weighted signal: in a feed with no references, timing and currency
    # may be the only evidence there is.
    linked = [c for c in scored
              if c.score > 0.0 or txn_key(c.txn) in learned_keys]

    if not linked:
        return LinkageResult(
            candidates=pool, scored=scored,
            pool_before=len(pool), pool_after=len(pool),
            em=em_summary,
            method="no_linkage_signal",
            reasoning=(
                "No reference, cluster or cross-source signal found anywhere "
                "in the pool. Passing all candidates through unchanged — "
                "narrowing on no evidence would risk discarding the answer."
            ),
        )

    # ── Source scoping, BEFORE any capping ──
    # One payment appears in several feeds with the same amount; pooling both
    # lets the solver double-count or swap them. Scope first: capping first kept
    # 400 records with none from the member feed, and a confident 67-record wrong
    # set followed.
    member_source = batch.member_source
    scope_basis = "declared"

    if member_source is None:
        anchor_sources = {c.txn.source for c in linked if c.signals.settlement_id_match}
        if len(anchor_sources) == 1:
            member_source = next(iter(anchor_sources))
            scope_basis = "inferred from anchors"

    scope_note = ""
    if member_source is not None:
        if scope_basis == "declared":
            # A declaration is knowledge, so it is authoritative: records
            # outside the member feed are excluded even when they name the
            # settlement. Naming it means a record RELATES to the settlement,
            # not that it composes it — the settlement credit being the
            # standing example, since it carries the id because it IS the
            # settlement.
            in_scope = [c for c in linked if c.txn.source is member_source]
        else:
            # Inferred, so held loosely: keep anything naming the settlement
            # even from another feed, since the inference came from anchors.
            in_scope = [
                c for c in linked
                if c.txn.source is member_source or c.signals.settlement_id_match
            ]

        dropped = len(linked) - len(in_scope)
        # Note the empty case explicitly rather than falling back to the
        # unscoped list. If nothing in the member feed linked to this
        # settlement, that is a finding — "no member-feed candidate is
        # connected to this batch" — and quietly reinstating out-of-feed
        # records to avoid an empty result is how the wrong answer got
        # through before.
        linked = in_scope
        if dropped:
            scope_note = (
                f" Scoped to {member_source.value} records ({scope_basis}), "
                f"excluding {dropped} other-feed representation(s) that would "
                f"otherwise be substitutable or double-counted."
            )

    if not linked:
        return LinkageResult(
            candidates=[], scored=scored,
            pool_before=len(pool), pool_after=0,
            em=em_summary,
            method="no_in_scope_candidates",
            reasoning=(
                f"No {member_source.value if member_source else 'in-scope'} "
                f"candidate is linked to this settlement.{scope_note}"
            ),
        )

    # Anchors are the strongest evidence, so they are kept whole and padded
    # with the next-best links rather than truncated.
    anchors = [c for c in linked if c.signals.settlement_id_match]
    if anchors:
        others = [c for c in linked if not c.signals.settlement_id_match]
        keep = anchors + others[: max(0, max_candidates - len(anchors))]
        method = "settlement_id_anchor"
        reasoning = (
            f"{len(anchors)} transaction(s) reference settlement "
            f"{batch.batch_id} directly; kept those plus "
            f"{len(keep) - len(anchors)} next-strongest links.{scope_note}"
        )
    else:
        keep = linked[:max_candidates]
        method = "reference_cluster"
        reasoning = (
            f"No in-scope transaction names the settlement. Narrowed on "
            f"reference clusters and cross-source amount peers.{scope_note}"
        )

    candidates = [c.txn for c in keep]

    # Report only anchors that SURVIVED scoping. A record excluded from the
    # candidate pool is not an anchor for this settlement no matter what its
    # reference says — the settlement's own bank credit being the standing
    # example, since it names the settlement because it IS one. Leaving it in
    # this list made callers believe anchor evidence existed when the anchor
    # could never be a member: the pipeline took the linkage-first path on
    # the strength of it and burned the attempt for nothing.
    kept_keys = {txn_key(t) for t in candidates}
    surviving_keys = direct_hits & kept_keys
    surviving_anchors = sorted(
        t.source_txn_id for t in candidates if txn_key(t) in surviving_keys
    )

    return LinkageResult(
        candidates=candidates,
        scored=scored,
        pool_before=len(pool),
        pool_after=len(candidates),
        method=method,
        reasoning=(
            f"{reasoning} Pool {len(pool)} -> {len(candidates)} "
            f"({(len(pool) / max(1, len(candidates))):.1f}x reduction)."
        ),
        anchor_cluster_ids=surviving_anchors,
        anchor_keys=surviving_keys,
        learned_keys=learned_keys & kept_keys,
        em=em_summary,
    )


def _learned_posteriors(batch: SettlementBatch, pool: list[NormalizedTxn],
                        signals: dict) -> tuple[dict[str, float], dict | None, set[str]]:
    """
    Fit the Fellegi-Sunter model to this pool and score every record.

    Fitted on the member feed when it is declared: the other feeds hold the
    same payments again, and a model fitted across all of them would learn
    that "a member" is anything with a twin. LINKAGE_EM=0 switches it off,
    which is how the before/after benchmark runs the same code both ways.
    """
    if os.environ.get("LINKAGE_EM", "1").strip() == "0":
        return {}, None, set()
    scope = [t for t in pool
             if batch.member_source is None or t.source is batch.member_source]
    if len(scope) < 4:
        return {}, None, set()

    settled_day = batch.settled_at_utc.date()
    import settlement_cycle  # pylint: disable=import-outside-toplevel
    member_feed = (batch.member_source.value if batch.member_source else "gateway")
    cycle = settlement_cycle.profile(member_feed, batch.currency,
                                     merchant=getattr(batch, "merchant", ""))
    unit = (cycle or {}).get("unit", "calendar")

    def vector(t: NormalizedTxn) -> dict[str, str]:
        s = signals[txn_key(t)]
        return {
            "anchor": "yes" if s.settlement_id_match else "no",
            "ref_cluster": "yes" if s.shared_ref_token else "no",
            "ref_names_batch": "yes" if s.ref_prefix_cluster else "no",
            "amount_peer": "yes" if s.cross_source_amount_peer else "no",
            "currency": "yes" if t.currency == batch.currency else "no",
            "lag": linkage_em.lag_level(linkage_em.lag_days(t.timestamp_utc.date(), settled_day, unit)),
        }

    vectors = {txn_key(t): vector(t) for t in scope}
    amounts = sorted(abs(t.amount_cents) for t in scope if t.amount_cents)
    target = batch.net_amount_cents + (batch.declared_deductions_cents or 0)
    typical = amounts[len(amounts) // 2] if amounts else 0
    expected = (abs(target) / typical) if typical else None

    model = linkage_em.fit(list(vectors.values()), expected_members=expected,
                           lag_m=cycle["m"] if cycle else None)
    if model is None:
        return {}, None, set()
    posteriors = {k: model.posterior(v) for k, v in vectors.items()}
    cohort = model.cohort(vectors, expected if expected else model.expected_members)
    summary = model.summary()
    if cycle:
        summary["cycle_learned_from"] = {"settlements": cycle["settlements"],
                                         "members": cycle["members"], "unit": unit}
    summary["cohort_size"] = len(cohort)
    return posteriors, summary, cohort


def confidence_band(anchored: set[str], matched_keys: set[str],
                    mean_link: float, learned: set[str] | None = None) -> str:
    """
    WHICH claim the engine is making about a matched set — named, so it can
    be measured.

    These used to be five inline literals inside link_confidence. A number
    written beside a comment saying "observed 0.55" cannot be checked by
    anything, and when the observation moves the literal does not. Naming the
    band lets scripts/calibration.py bucket outcomes BY BAND and report what
    each one is actually worth, which is how the values below are now set.
    """
    if anchored and matched_keys <= anchored:
        return "fully_anchored"
    if anchored and anchored <= matched_keys:
        # Partially anchored, but the match USED EVERY ANCHOR AVAILABLE.
        #
        # "One record names the settlement and the match includes it" is a
        # different claim from "three records name it and the match includes
        # one". In the first the unanchored members are the rest of the
        # settlement; in the second, two pieces of evidence were ignored.
        return "anchors_all_used"
    if anchored & matched_keys:
        return "anchors_ignored"
    if learned and matched_keys <= learned:
        # Nothing names the settlement, but every member is in the cohort
        # the learned settlement cycle points at.
        return "learned_cycle"
    if mean_link >= 0.4:
        return "clustered"
    return "arithmetic_only"


# Set at or below measured accuracy: overconfidence costs money,
# underconfidence costs review time. Measured per band (exact set = truth):
#
#   band               benchmark (180)      edge sweep (962)
#   fully_anchored     n=135  obs 0.719     n=829  obs 0.837
#   anchors_all_used   n=15   obs 1.000     n=0    no data
#   arithmetic_only    n=30   obs 0.200     n=100  obs 0.000
#
# These are inputs: the reported figure is min(arithmetic, band), and every
# published prediction at or above the 0.85 gate was right (103/103
# in-sample, 42/42 out). anchors_ignored and clustered never fired; their
# values are reasoned and labelled unmeasured.
CONFIDENCE_BANDS = {
    "fully_anchored": 0.95,
    "anchors_all_used": 0.88,
    "anchors_ignored": 0.42,      # UNMEASURED — never fired in either corpus
    "clustered": 0.54,            # UNMEASURED — never fired in either corpus
    "arithmetic_only": 0.19,      # benchmark 0.200; the edge corpus, which is
                                  # 26% deliberately unrecoverable, says 0.000
    # Measured on ReconRiver with references stripped and the cycle learned
    # from the OTHER scenarios' clears (scripts/learned_linkage_benchmark.py):
    # where this band set the reported confidence, 7 of 7 proposals were the
    # exact set; over every match it touched, 13 of 25. Seven is too few to
    # release money on — the Wilson 95% lower bound for 7/7 is 0.65 — so the
    # band sits below the 0.85 gate even with the small-pool bonus: it
    # proposes, a person confirms. Raise it only on more measured cases.
    "learned_cycle": 0.80,
}


def link_confidence(result: LinkageResult, matched_keys: set[str]) -> float:
    """
    Confidence that the matched set is the true set, given how it was found.
    Capped below 1.0 and calibrated (see CONFIDENCE_BANDS). Takes txn_keys,
    never bare ids: a cross-feed collision would otherwise inflate it.
    """
    if not matched_keys:
        return 0.0

    by_key = {txn_key(c.txn): c for c in result.scored}
    scores = [by_key[k].score for k in matched_keys if k in by_key]
    if not scores:
        return 0.35

    mean_link = sum(scores) / len(scores)
    # anchor_keys, not anchor_cluster_ids: the latter is bare ids kept for
    # human-readable output (audit lines, reports), and comparing bare ids
    # here is the collision bug this function's docstring now warns about.
    anchored = result.anchor_keys

    # Calibrated, not chosen: the first measurement found the middle bands badly
    # overstated (0.80 was right 43%, 0.65 right 55%), and partial anchoring was
    # worse than clustering. Values sit at or below observed accuracy.
    base = CONFIDENCE_BANDS[confidence_band(anchored, matched_keys, mean_link,
                                            result.learned_keys)]

    # A narrowed pool makes an exact sum far less likely to be coincidence.
    if result.pool_after <= 25:
        base += 0.03
    elif result.pool_after > 200:
        base -= 0.08

    return round(max(0.05, min(0.97, base)), 3)
