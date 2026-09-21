"""
Agent 2b — Linkage / candidate constraint.

THE REFRAME THIS MODULE EXISTS FOR
----------------------------------
Subset-sum was being used to IDENTIFY which transactions make up a
settlement. Measured across 120 benchmark scenarios, that does not work and
cannot work: the solver may use any subset size, so the space competing for
a single target is 2^n — 1.2e18 for a pool of only 60 — against a target
that can take ~2e6 distinct paise values. Millions of subsets hit the same
rupee value. Baseline auto-clear accuracy was 0.0%.

The error was architectural, not algorithmic. A better solver does not fix
an under-determined problem.

Real reconciliation is entity resolution first and arithmetic second. What
actually identifies a settlement's members is LINKAGE — the settlement
reference carried on the payment, the same payment appearing as a bank
credit and a ledger entry, temporal clustering — and the sum is then used
to VERIFY the linked set, not to discover it.

So this module runs before the solver and answers a different question:
"which transactions are plausibly connected to this settlement at all?"
Subset-sum then runs over tens of candidates instead of tens of thousands,
where it is genuinely determined.

DESIGN NOTES
------------
Blocking is high-recall by intent. Dropping a true member here is
unrecoverable — the solver can never put back what blocking removed — while
admitting extra candidates only costs solver time. So every block key is
cheap and generous, and precision is recovered later by scoring and by the
sum constraint.

Confidence is deliberately conservative and is calibrated against observed
outcomes rather than asserted: see `link_confidence`. A reconciliation
engine that reports 0.95 and is right 60% of the time is worse than useless,
because the number is what a human uses to decide whether to look.
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
    A transaction's identity for linkage purposes.

    `source_txn_id` alone is NOT unique. Feeds mint their own sequences and
    they overlap constantly in real data — a gateway payment "1001" and an
    unrelated ERP journal line "1001" are different records with the same id.
    Keying signals by the bare id merges the two into one LinkSignals object,
    so an unrelated record inherits the other's anchor status and can be
    auto-cleared at 0.95 confidence on evidence that belongs to a different
    transaction. Measured directly: an ERP record whose reference had nothing
    to do with the settlement came back with settlement_id_match=True.
    """
    return f"{t.source.value}:{t.source_txn_id}"


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
    Is this token plausibly an IDENTIFIER rather than a descriptive word?

    Anchoring — treating a transaction as naming the settlement — is the
    strongest signal this module has, so it must not fire on boilerplate.
    Real payment references are identifiers and effectively always carry
    digits: "0042", "UTR20260818", "RZP88121". Batch IDs, by contrast,
    routinely carry descriptive words ("SETTLE", "BATCH", "MERCHANT",
    "DAILY") that collide with unrelated references and would anchor half
    the pool.

    This was a measured failure, not a hypothetical: a batch id of
    "BENCH_0001_near_collision_sparse" anchored eight decoy transactions
    whose references began "NEAR", because "near" appeared in both. The
    solver then had two arithmetically valid subsets and correctly refused
    to clear — losing a match it should have made.

    Pure-alphabetic tokens still contribute cluster signal; they just
    cannot anchor.
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
    Does `haystack` contain `needle` as a WHOLE identifier rather than as the
    truncated head of a longer one?

    Plain substring containment is wrong in a way synthetic data never shows.
    Settlement ids in the wild are sequential and unpadded — SETTLE-1,
    SETTLE-10, SETTLE-100 — and "SETTLE1" is a substring of both "SETTLE10"
    and "SETTLE100". Measured: batch SETTLE-1 anchored the members of
    SETTLE-10 and SETTLE-100, and anchoring is the strongest signal this
    module has, worth 0.55 on its own and 0.95 confidence once matched. A
    false anchor is therefore not a near miss, it is a confident wrong answer
    on someone else's money.

    Both reference datasets hide this: they use fixed-width zero-padded ids
    (SYNTH-BATCH-USD-2026-01-03-0000), where no id is a prefix of another.
    That is a property of those generators, not of settlement ids.

    The rule is a numeric boundary. A digit adjacent to the match on either
    end means the number continues, so the match is a prefix of a different
    identifier. A letter is fine: "SETTLE1" inside "SETTLE1ORDER001" is the
    settlement id followed by a different field.
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


def _canonical_anchor_hits(batch: SettlementBatch, pool: list[NormalizedTxn]) -> set[str]:
    """
    Transactions whose reference contains the settlement id as a whole
    identifier, compared in canonical form. Returns txn_key values.

    Token intersection alone is not enough, and the gap is not academic.
    Ingestion stores references with separators stripped, while `batch_id`
    keeps whatever punctuation the source used. A settlement called
    "SYNTH-BATCH-USD-2026-01-03-0000" therefore tokenises to fragments
    ("2026", "0000") while the transaction that names it canonicalises to one
    run — "SYNTHBATCHUSD202601030000SYNTHORDER000001" — whose tokens are
    "SYNTHBATCHUSD" and "202601030000". The two never intersect, so linkage
    found ZERO anchors on the ReconRiver dataset and every batch failed,
    including the clean ones that tie to the cent.

    Our own 50K dataset passed because its settlement id happened to contain
    no internal separators — an untested assumption rather than a decision,
    since nothing had compared the two sides' normalisation.

    Comparing canonical forms is separator-agnostic, which is what a reference
    field has to be: the same identifier is written "STL-2026-001",
    "STL_2026_001" and "STL2026001" by three different systems on the same
    payment. Containment is bounded at numeric edges so a short id cannot
    anchor a longer one — see _contains_identifier.
    """
    canon = canonical_key(batch.batch_id)
    if len(canon) < MIN_CANONICAL_ANCHOR_LEN:
        return set()
    return {
        txn_key(t) for t in pool
        if _contains_identifier(canonical_key(t.ref_id_canonical), canon)
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

    # ── source scoping, BEFORE any capping ────────────────────────────────
    #
    # The same payment appears in more than one feed — a gateway payment also
    # lands as an ERP ledger entry — carrying the SAME amount. Pool both
    # representations and subset-sum will happily select both, double-counting
    # one payment, or swap one for the other and leave the arithmetic
    # identical while the answer is wrong.
    #
    # This runs BEFORE the max_candidates cap, and the ordering is the whole
    # point. Capping first ranks the pool by linkage score and keeps the top
    # N — but score does not correlate with feed, so the cap can retain 400
    # records of which NONE are in the member feed. Scoping then has nothing
    # to filter, silently no-ops, and every out-of-feed record it was meant
    # to remove sails through. Measured on the 50K dataset with the
    # settlement reference stripped: the cap kept 400 candidates, scoped=0 of
    # them were gateway, and the bank settlement credit survived as an anchor
    # for a settlement it *is* rather than belongs to. The solver then
    # returned a confident 67-record set that was simply wrong.
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

    def vector(t: NormalizedTxn) -> dict[str, str]:
        s = signals[txn_key(t)]
        return {
            "anchor": "yes" if s.settlement_id_match else "no",
            "ref_cluster": "yes" if s.shared_ref_token else "no",
            "ref_names_batch": "yes" if s.ref_prefix_cluster else "no",
            "amount_peer": "yes" if s.cross_source_amount_peer else "no",
            "currency": "yes" if t.currency == batch.currency else "no",
            "lag": linkage_em.lag_level((settled_day - t.timestamp_utc.date()).days),
        }

    vectors = {txn_key(t): vector(t) for t in scope}
    amounts = sorted(abs(t.amount_cents) for t in scope if t.amount_cents)
    target = batch.net_amount_cents + (batch.declared_deductions_cents or 0)
    typical = amounts[len(amounts) // 2] if amounts else 0
    expected = (abs(target) / typical) if typical else None

    import settlement_cycle  # pylint: disable=import-outside-toplevel
    member_feed = (batch.member_source.value if batch.member_source else "gateway")
    cycle = settlement_cycle.profile(member_feed, batch.currency)
    model = linkage_em.fit(list(vectors.values()), expected_members=expected,
                           lag_m=cycle["m"] if cycle else None)
    if model is None:
        return {}, None, set()
    posteriors = {k: model.posterior(v) for k, v in vectors.items()}
    cohort = model.cohort(vectors, expected if expected else model.expected_members)
    summary = model.summary()
    if cycle:
        summary["cycle_learned_from"] = {"settlements": cycle["settlements"],
                                         "members": cycle["members"]}
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


# Set at or BELOW measured accuracy, because the two directions of error are
# not symmetric: overconfidence tells a reviewer to skip a batch that was
# wrong, underconfidence only wastes their time.
#
# Measured per band by instrumenting confidence_band across both corpora,
# counting a band right when the matched set is EXACTLY the true set:
#
#   band               benchmark (180)      edge sweep (962)
#   fully_anchored     n=135  obs 0.719     n=829  obs 0.837
#   anchors_all_used   n=15   obs 1.000     n=0    no data
#   anchors_ignored    n=0    no data       n=0    no data
#   clustered          n=0    no data       n=0    no data
#   arithmetic_only    n=30   obs 0.200     n=100  obs 0.000
#
# READ THAT TABLE CAREFULLY BEFORE CHANGING A VALUE. Two things in it are
# easy to get wrong, and both were got wrong once already.
#
# First: these are NOT the confidence the engine reports. The reported figure
# is min(arithmetic confidence, this), so a fully-anchored set whose
# arithmetic is ambiguous is reported at the ambiguity value, not at 0.95.
# Calibrating this table directly against "was the set right" therefore
# measures an input, not the output a human reads — which is why
# fully_anchored can observe 0.719 here while every prediction the engine
# actually published at or above the 0.85 gate was correct (103 of 103
# in-sample, 42 of 42 out-of-sample).
#
# Second: `anchors_ignored` and `clustered` never fired in either corpus.
# There is no measurement behind 0.42 and 0.54, and there is no measurement
# behind any replacement either. They are left at their reasoned values and
# labelled unmeasured rather than given fabricated ones — a number invented
# to look calibrated is worse than a number honestly marked as a guess.
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

    Reported alongside the arithmetic result so a human can tell a match
    backed by a settlement-ID anchor from one that is arithmetically valid
    but structurally unsupported. Deliberately capped below 1.0: an exact sum
    over a narrowed pool is strong evidence, never proof, and the calibration
    report in the benchmark is what justifies these numbers rather than the
    numbers being asserted here.

    Takes `txn_key` values, not bare `source_txn_id`s — see the `txn_key`
    docstring for why. This function used to key on the bare id (`by_id =
    {c.txn.source_txn_id: c ...}`, `anchored = set(result.anchor_cluster_ids)`,
    both bare-id collections), which is exactly the confidence number a
    cross-feed id collision would inflate, and inflating confidence is the
    direction that costs money. Callers must resolve their matched
    transactions to keys (`{txn_key(t) for t in ...}`) before calling this.
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

    # These are CALIBRATED against measured outcomes, not chosen by feel.
    # scripts/calibration.py buckets every prediction by the confidence
    # claimed and compares it with how often that band was actually right.
    #
    # The first measurement (ECE 0.17, MCE 0.38) found the original values —
    # 0.95 / 0.80 / 0.65 / 0.45 — badly overstated in the middle:
    #
    #   said 0.804  ->  actually right 42.9%   (partially anchored)
    #   said 0.650  ->  actually right 54.5%   (clustered)
    #   said 0.427  ->  actually right 19.5%   (arithmetic only)
    #   said 0.955  ->  actually right 100%    (fully anchored)
    #
    # Two things came out of that. The top band was already safe — it
    # understates, which costs review time rather than money. And PARTIAL
    # anchoring turned out to be WORSE than clustering (42.9% vs 54.5%),
    # the opposite of the ordering the original weights assumed: one
    # anchored member among several unanchored ones is weak evidence, not
    # most of the way to strong evidence.
    #
    # Values are set at or slightly below observed accuracy, because the two
    # directions of error are not symmetric. Overconfidence tells a reviewer
    # to skip a batch that was wrong; underconfidence wastes their time.
    base = CONFIDENCE_BANDS[confidence_band(anchored, matched_keys, mean_link,
                                            result.learned_keys)]

    # A narrowed pool makes an exact sum far less likely to be coincidence.
    if result.pool_after <= 25:
        base += 0.03
    elif result.pool_after > 200:
        base -= 0.08

    return round(max(0.05, min(0.97, base)), 3)
