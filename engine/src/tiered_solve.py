"""
Solving in tiers of decreasing evidence, and what follows a solve: the
substitutability guard, the tiebreak for an ambiguous set, and the fuzzy
recovery when nothing sums. Called by orchestrator.reconcile_batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from schema import NormalizedTxn, SettlementBatch, MatchResult, MatchMethod, ExceptionRecord
from subset_sum import SubsetSumConfig, match_batch, _anchored_negatives
from fuzzy_match import FuzzyMatchConfig, score_subset_plausibility, build_similarity_context, bulk_fuzzy_recover
from linkage import txn_key, members_of
from recon_gates import FUZZY_RECOVERY_CONFIDENCE
import audit


def _tiebreak_ambiguous_match(
    primary_txns: list[NormalizedTxn],
    candidates: list[NormalizedTxn],
    target_cents: int,
    tolerance_cents: int,
    primary_keys: set[str],
    batch_id: str,
    forced_ids: set[str] | None = None,
    num_search_workers: int = 1,
    time_limit_s: float = 5.0,
) -> Optional[list[NormalizedTxn]]:
    """
    Break an ambiguous tie: find one alternate subset (forbidding the primary)
    and keep whichever scores higher on reference/memo plausibility.
    `forced_ids` are passed through so the alternate cannot simply drop an
    anchored refund; keys are txn_keys, since the pool mixes feeds.
    """
    from subset_sum import _solve_cpsat

    # find an alternate subset
    forbidden = [primary_keys]
    alt_result = _solve_cpsat(
        candidates, target_cents, tolerance_cents, time_limit_s,
        forbidden_solutions=forbidden,
        num_search_workers=num_search_workers,
        forced_ids=forced_ids,
    )
    if alt_result is None:
        # no genuine alternate found (probe budget exhausted), keep primary
        audit.log_decision(
            batch_id=batch_id,
            agent="tiebreak",
            detail="Ambiguous flag set but no alternate subset found during tiebreak — keeping primary."
        )
        return primary_txns

    alt_txns, _ = alt_result
    alt_keys = {txn_key(t) for t in alt_txns}

    if alt_keys == primary_keys:
        return primary_txns  # same subset, no tiebreak needed

    # Score both subsets via fuzzy plausibility. Build ONE similarity
    # context over the UNION of both subsets (bounded, small — typically
    # far smaller than the full candidate pool) and reuse it for both
    # scores, rather than a full pool x pool matrix. See fuzzy_match.py's
    # module docstring for why a full square matrix over the whole pool
    # OOM'd on the real 50K stress dataset (~19.6GB at 35K candidates).
    combined_ids_seen: set[str] = set()
    query_union: list[NormalizedTxn] = []
    for t in primary_txns + alt_txns:
        if t.source_txn_id not in combined_ids_seen:
            combined_ids_seen.add(t.source_txn_id)
            query_union.append(t)

    context = build_similarity_context(query_union, candidates)
    primary_score = score_subset_plausibility(primary_txns, context)
    alt_score = score_subset_plausibility(alt_txns, context)

    winner = "primary" if primary_score >= alt_score else "alternate"
    chosen = primary_txns if primary_score >= alt_score else alt_txns
    audit.log_decision(
        batch_id=batch_id,
        agent="tiebreak",
        detail=(
            f"Tiebreak between primary ({len(primary_txns)} txns, score={primary_score:.3f}) "
            f"and alternate ({len(alt_txns)} txns, score={alt_score:.3f}). "
            f"Chose {winner}."
        )
    )
    return chosen


@dataclass
class _SolveOutcome:
    """What solving produced, and the pool it was actually solved over.

    `solver_candidates` is returned rather than assumed because the safety
    net can widen it back to the full windowed pool, and every guard after
    this point reasons about pool size. Reading a stale pool there would let
    a large-pool batch pass the unanchored limit on the narrowed count.
    """
    result: MatchResult
    solver_candidates: list[NormalizedTxn]
    scores: dict
    anchor_keys: set
    anchor_ids: set


def _withhold_if_substitutable(batch, result, pool_txns, windowed_candidates, anchor_keys,
                               label: str, outside: str = "this tier") -> None:
    """
    Substitutability guard. A narrowed pool (a tier, or one settlement's pool
    in a joint solve) can hide one copy of a payment that exists in another
    feed with the same amount, so a solve looks unique when it is not. This
    produced the first false clears ever recorded ("_MIRROR" records), so a
    matched member with an equal-amount twin in another feed, outside the
    pool it was solved over, withholds the clear.

    Exempt: anchored members (they name the settlement) and members of the
    declared member feed (the other copy was never a candidate). Scoring the
    two copies against each other was tried and endorsed the wrong one (30
    false clears). Keys, not bare ids, throughout: ids repeat across feeds.
    """
    if not (result.cleared and result.matched_txn_ids):
        return
    pool_ids = {txn_key(t) for t in pool_txns}
    matched_txns = members_of(result, pool_txns)
    substitutable = [
        t for t in matched_txns
        if txn_key(t) not in anchor_keys
        and not (batch.member_source is not None and t.source is batch.member_source)
        and any(o.amount_cents == t.amount_cents and o.source is not t.source
                and txn_key(o) not in pool_ids for o in windowed_candidates)
    ]
    if not substitutable:
        return
    result.cleared = False
    result.ambiguous = True
    result.reasoning += (
        f" Withheld from auto-clear: {len(substitutable)} matched "
        f"record(s) have an equal-amount counterpart in another feed "
        f"outside {outside}, so the set is substitutable and the "
        f"system of record is not determined by the arithmetic."
    )
    audit.log_decision(
        batch_id=batch.batch_id,
        agent="linkage",
        detail=(f"'{label}' summed to target but {len(substitutable)} member(s) are "
                f"substitutable with another feed's copy. Refusing to auto-clear."),
    )


def _solve_in_tiers(
    batch: SettlementBatch,
    solver_candidates: list[NormalizedTxn],
    windowed_candidates: list[NormalizedTxn],
    link_result,
    gross_target: int,
    cfg: SubsetSumConfig,
) -> _SolveOutcome:
    """Agent 3 end to end: tiered solve, empty-linkage fallback, safety net."""
    # Exact subset-sum in tiers of decreasing evidence, first tier to CLEAR wins:
    # records naming the settlement, then the learned cohort, then strong links,
    # then everything linked. Weak signals admitted beside strong ones add
    # degeneracy, not information (solving anchors first took near-collision from
    # 0% to solved; one pooled tier took ref_partial from 100% to 13%).
    STRONG_LINK = 0.25

    # Keyed by txn_key, not by the bare id. Feeds mint overlapping id
    # sequences, so a bare-id lookup lets an unrelated ERP record inherit a
    # gateway record's anchor status and be solved in the anchor tier.
    scores = {txn_key(c.txn): c.score for c in link_result.scored}
    anchor_keys = link_result.anchor_keys
    anchor_ids = set(link_result.anchor_cluster_ids)   # bare ids, for reporting

    anchor_txns = [t for t in solver_candidates if txn_key(t) in anchor_keys]
    strong_txns = [
        t for t in solver_candidates
        if scores.get(txn_key(t), 0.0) >= STRONG_LINK
        or txn_key(t) in anchor_keys
    ]

    # Records the Fellegi-Sunter model rates likely members (linkage_em.py),
    # with the anchors. Where references are gone this is the capture-day
    # cohort the learned settlement cycle points at; where some survive it
    # is the anchors plus records that look like them but lost their id.
    learned_keys = getattr(link_result, "learned_keys", None) or set()
    learned_txns = [t for t in solver_candidates
                    if txn_key(t) in learned_keys or txn_key(t) in anchor_keys]

    tiers: list[tuple[str, list[NormalizedTxn]]] = []
    if anchor_txns:
        tiers.append(("anchor", anchor_txns))
    if len(anchor_txns) < len(learned_txns) < len(solver_candidates):
        tiers.append(("learned", learned_txns))
    if len(strong_txns) > len(anchor_txns):
        tiers.append(("strong_link", strong_txns))
    tiers.append(("all_linked", solver_candidates))

    result = None
    seen_sizes: set[int] = set()
    for tier_name, tier_txns in tiers:
        if not tier_txns or len(tier_txns) in seen_sizes:
            continue
        seen_sizes.add(len(tier_txns))

        tier_result = match_batch(batch.batch_id, tier_txns, gross_target, cfg)

        _withhold_if_substitutable(batch, tier_result, tier_txns, windowed_candidates,
                                   anchor_keys, tier_name)

        if tier_result.cleared:
            audit.log_decision(
                batch_id=batch.batch_id,
                agent="linkage",
                detail=(
                    f"Cleared on the '{tier_name}' tier: {len(tier_txns)} of "
                    f"{len(solver_candidates)} linked candidate(s) sum to target "
                    f"without needing weaker-linked records."
                ),
            )
            result = tier_result
            solver_candidates = tier_txns
            break
        if result is None:
            result = tier_result  # keep the strongest tier's answer as fallback

    # Linkage can legitimately return NOTHING: a declared member_source with
    # no in-scope candidate leaves every tier empty, so the loop above never
    # assigns a result. That is a real finding, not an error — "no record in
    # the declared member feed is connected to this settlement" — but it
    # reached the code below as None and raised AttributeError, surfacing to
    # the API as a 500. Reachable straight from the upload form the moment
    # someone picks the wrong feed, which is exactly when a user needs the
    # diagnosis rather than a stack trace.
    if result is None:
        result = MatchResult(
            batch_id=batch.batch_id,
            matched_txn_ids=[],
            matched_sum_cents=0,
            target_cents=gross_target,
            cleared=False,
            confidence=0.0,
            method=MatchMethod.MANUAL_REVIEW,
            reasoning=(
                f"No candidate survived linkage for this settlement. "
                f"{link_result.reasoning}"
            ),
            ambiguous=False,
        )
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="linkage",
            detail=(
                "No candidate survived linkage; nothing was solved. "
                f"{link_result.reasoning}"
            ),
        )

    # Safety net: linkage must never make the outcome WORSE than solving the
    # unconstrained pool. If it narrowed and that solve produced nothing,
    # fall back to the full windowed pool so a bad block key can only cost
    # solver time, never the answer. (Blocking is high-recall by design, but
    # "by design" is not a guarantee, and this is money.)
    narrowed = link_result.pool_after < link_result.pool_before
    if narrowed and not result.cleared:
        fallback = match_batch(batch.batch_id, windowed_candidates, gross_target, cfg)
        if fallback.cleared:
            # Check if the fallback match left anchors unused. If it did, the
            # fallback is evidence-contradicting and should be withheld.
            if anchor_keys:
                fallback_matched_keys = {txn_key(t) for t in
                                         members_of(fallback, windowed_candidates)}
                anchors_in_pool = anchor_keys & {txn_key(t) for t in windowed_candidates}
                unused_anchors = anchors_in_pool - fallback_matched_keys
                
                if unused_anchors:
                    # Fallback left anchors unused - don't accept it
                    audit.log_decision(
                        batch_id=batch.batch_id,
                        agent="linkage",
                        detail=(
                            f"Constrained solve found nothing; unconstrained solve over "
                            f"{link_result.pool_before} candidates found a match but left "
                            f"{len(unused_anchors)} anchor(s) unused. Rejecting fallback - "
                            f"an ignored anchor is evidence pointing elsewhere."
                        ),
                    )
                    # Keep the constrained result (no match), don't accept fallback
                else:
                    # Fallback used all available anchors - accept it
                    audit.log_decision(
                        batch_id=batch.batch_id,
                        agent="linkage",
                        detail=(
                            f"Constrained solve found nothing over {link_result.pool_after} "
                            f"linked candidates; retried unconstrained over "
                            f"{link_result.pool_before} candidates and found a match using "
                            f"all {len(anchors_in_pool)} available anchor(s). Accepting fallback."
                        ),
                    )
                    result = fallback
                    solver_candidates = windowed_candidates
            else:
                # No anchors - original fallback behavior applies
                audit.log_decision(
                    batch_id=batch.batch_id,
                    agent="linkage",
                    detail=(
                        f"Constrained solve found nothing over {link_result.pool_after} "
                        f"linked candidates; retried unconstrained over "
                        f"{link_result.pool_before} candidates and CLEARED. "
                        "Linkage narrowed too aggressively."
                    ),
                )
                result = fallback
                solver_candidates = windowed_candidates

    return _SolveOutcome(
        result=result,
        solver_candidates=solver_candidates,
        scores=scores,
        anchor_keys=anchor_keys,
        anchor_ids=anchor_ids,
    )


def _collect_unmatched(
    batch: SettlementBatch,
    result: MatchResult,
    candidates: list[NormalizedTxn],
    windowed_candidates: list[NormalizedTxn],
    gross_target: int,
    fuzz_cfg: FuzzyMatchConfig,
) -> tuple[list[ExceptionRecord], list[NormalizedTxn]]:
    """Everything the solve did not account for, and the fuzzy recovery pass.

    Returns (exceptions, unmatched) rather than setting them on the caller,
    so the two are always produced together - they describe the same residual
    and reading one without the other has never been meaningful.
    """

    exceptions: list[ExceptionRecord] = []
    unmatched: list[NormalizedTxn] = []

    if result.cleared:
        # Exact, unambiguous clear. The rest of the window belongs to other
        # settlements and is NOT an exception: routing it produced 20,078 exceptions
        # on a perfect 5-transaction match and demanded review of a certain answer.
        matched_ids = set(result.matched_txn_ids)
        residual_count = len(windowed_candidates) - len(matched_ids)
        unmatched = []
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="orchestrator",
            detail=(
                f"Exact match cleared with {len(matched_ids)} txns. "
                f"{residual_count} residual in-window candidates left unreconciled "
                f"by design — they belong to other settlements, not this batch, "
                f"and are not exceptions."
            ),
        )

    elif result.ambiguous and result.matched_txn_ids:
        # Ambiguous: CP-SAT found A valid subset but not THE unique one.
        # Tiebreak already ran above. Do NOT run fuzzy — that would be adding
        # semantic noise on top of arithmetic ambiguity. Route directly to human.
        # The matched txns are the tiebreak's best guess; the rest are unmatched.
        matched_ids = set(result.matched_txn_ids)
        member_keys = {txn_key(t) for t in members_of(result, windowed_candidates)}
        unmatched = [t for t in windowed_candidates if txn_key(t) not in member_keys]
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="orchestrator",
            detail=(
                f"Ambiguous exact match — skipping fuzzy fallback. "
                f"Tiebreak chose {len(matched_ids)} txns; "
                f"{len(unmatched)} routed to exception diagnosis."
            ),
        )

    else:
        # No arithmetic match at all — now fuzzy fallback (Agent 4) earns its keep.
        # Try to partially recover unmatched records via ref_id/memo similarity.
        #
        # Uses bulk_fuzzy_recover, NOT a per-transaction loop calling
        # match_batch_fuzzy. The original per-txn loop rebuilt an O(n)
        # pool copy AND ran per-pair similarity scoring for every single
        # unmatched transaction -- measured at ~112 MINUTES projected for
        # a single ~20,000-item unmatched chunk from the real 50K stress
        # dataset. bulk_fuzzy_recover builds one similarity matrix for
        # the whole pool and does vectorized lookups instead.
        unmatched = windowed_candidates
        fuzzy_results = bulk_fuzzy_recover(batch.batch_id, unmatched, windowed_candidates, fuzz_cfg)

        fuzzy_recovered: list[str] = []
        for txn, fuzzy_result in zip(unmatched, fuzzy_results):
            if fuzzy_result.cleared and fuzzy_result.matched_txn_ids:
                fuzzy_recovered.extend([txn.source_txn_id] + fuzzy_result.matched_txn_ids)
                audit.log_decision(
                    batch_id=batch.batch_id,
                    agent="fuzzy_fallback",
                    detail=(
                        f"Fuzzy match: {txn.source_txn_id} -> "
                        f"{fuzzy_result.matched_txn_ids[0]} "
                        f"(confidence={fuzzy_result.confidence:.2f}): {fuzzy_result.reasoning}"
                    ),
                )

        if fuzzy_recovered:
            recovered_set = set(fuzzy_recovered)
            unmatched = [t for t in unmatched if t.source_txn_id not in recovered_set]
            # Update result to reflect fuzzy recovery
            result.matched_txn_ids = list(recovered_set)
            result.matched_keys = [txn_key(t) for t in windowed_candidates
                                   if t.source_txn_id in recovered_set]
            result.matched_sum_cents = sum(
                t.amount_cents for t in windowed_candidates
                if t.source_txn_id in recovered_set
            )
            result.method = MatchMethod.FUZZY_SEMANTIC
            result.confidence = FUZZY_RECOVERY_CONFIDENCE
            # Append to the reasoning rather than replace it: the earlier text says why
            # subset-sum found nothing, and a fuzzy set need not sum to the target.
            shortfall = result.matched_sum_cents - gross_target
            result.reasoning = (
                f"{result.reasoning.rstrip()} "
                f"The fuzzy pass recovered {len(recovered_set)} transaction"
                f"{'' if len(recovered_set) == 1 else 's'} on reference and "
                f"memo similarity, totalling {result.matched_sum_cents}c "
                f"against a target of {gross_target}c — "
                f"{'over' if shortfall > 0 else 'short'} by {abs(shortfall)}c. "
                f"Similarity is evidence of association, not of arithmetic, so "
                f"this set is a starting point for review and is never cleared "
                f"on its own. Measured over 167 such bundles: the exact set is "
                f"never right, about 4% of what it contains belongs to the "
                f"settlement, and the true members are somewhere inside it "
                f"roughly 40% of the time. Read it as a shortlist to search, "
                f"not as a proposed answer — which is why it is reported at "
                f"{FUZZY_RECOVERY_CONFIDENCE:.2f} rather than at the "
                f"similarity threshold that selected it."
            )
            audit.log_decision(
                batch_id=batch.batch_id,
                agent="fuzzy_fallback",
                detail=f"Recovered {len(fuzzy_recovered)} txns via fuzzy pass. "
                       f"{len(unmatched)} still unresolved.",
            )

    return exceptions, unmatched


def _tiebreak_if_ambiguous(
    batch: SettlementBatch,
    result: MatchResult,
    windowed_candidates: list[NormalizedTxn],
    gross_target: int,
    cfg: SubsetSumConfig,
    enable_tiebreak: bool,
    learned_keys: set | None = None,
) -> None:
    """Agent 3b: tiebreak ambiguous matches via fuzzy plausibility scoring."""
    if result.ambiguous and enable_tiebreak and result.matched_txn_ids:
        matched_pool = members_of(result, windowed_candidates)
        # A proposal drawn entirely from the learned cohort is withheld because
        # its confidence is below the gate — "a person should confirm this" —
        # not because a better set exists. The tiebreak scores reference and
        # memo similarity, which is noise in a pool whose references are gone,
        # and it once swapped an exact, cohort-backed 15-payment set for a
        # wrong 20-payment one on exactly such a pool. Evidence beats a
        # preference between equal sums, so the evidenced proposal stands.
        if learned_keys and {txn_key(t) for t in matched_pool} <= learned_keys:
            return
        # txn_key, not the bare matched_txn_ids: candidates here is fed by
        # _solve_cpsat, which compares by txn_key (see its docstring), and the
        # chosen set below is compared in the same form.
        primary_keys = {txn_key(t) for t in matched_pool}
        # Recomputed rather than threaded through MatchResult: it's a cheap,
        # pure filter over windowed_candidates (same inputs match_batch used
        # internally), and re-deriving it here is far less invasive than
        # widening the MatchResult schema to carry it.
        forced_ids = _anchored_negatives(batch.batch_id, windowed_candidates)
        # A declared member feed holds for the tiebreak too. Linkage scoped the
        # solve to it; this probe searched every feed and so could "prefer" a
        # set of ledger copies — on the demo sample, 11 ERP records of which 7
        # doubled a gateway payment already in the set. Withheld either way,
        # but a reviewer was shown a set that could never be right.
        # FAILURE_LOG 31.
        pool = ([t for t in windowed_candidates if t.source is batch.member_source]
                if batch.member_source is not None else windowed_candidates)
        chosen = _tiebreak_ambiguous_match(
            matched_pool, pool, gross_target,
            cfg.tolerance_cents, primary_keys, batch.batch_id,
            forced_ids=forced_ids,
            num_search_workers=cfg.num_search_workers,
            time_limit_s=cfg.probe_time_limit_s,
        )
        if chosen is not None:
            chosen_keys = {txn_key(t) for t in chosen}
            if chosen_keys != primary_keys:
                result.matched_txn_ids = [t.source_txn_id for t in chosen]
                result.matched_keys = sorted(chosen_keys)
                result.matched_sum_cents = sum(t.amount_cents for t in chosen)
                result.confidence = min(result.confidence, 0.25)
                result.reasoning += (
                    " Tiebreak: alternate subset chosen via ref_id/memo "
                    "plausibility scoring — a preference between equally valid "
                    "arithmetic, not evidence. Confidence reflects that."
                )
