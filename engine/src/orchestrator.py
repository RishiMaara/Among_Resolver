"""
The reconciliation pipeline for one settlement, and the N:M variant.

Order: fee decomposition (gross target), currency and status filters, the
settlement window, linkage, exact subset-sum in evidence tiers, the refusal
gates (recon_gates.py), tiebreak for ambiguous sets, fuzzy recovery when
nothing sums, exception diagnosis, report and tie-out. Every decision is
written to the audit trail. Nothing here writes to a ledger.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from schema import NormalizedTxn, SettlementBatch, MatchResult, MatchMethod, ExceptionRecord
from fee_decomposition import compute_fee_breakdown, FeeRateCard, DEFAULT_RATE_CARD
from subset_sum import (
    SubsetSumConfig,
    find_exact_subset,
    match_batch,
    filter_candidates_by_settlement_window,
    _anchored_negatives,
    UNPROVEN_UNIQUE_CONFIDENCE,
    UNPROVEN_UNIQUE_NOTE,
)
from subset_sum_nm import build_union_pool, exact_subset_sum_nm, probe_for_alternate_nm_assignment
from fuzzy_match import (
    match_batch_fuzzy, FuzzyMatchConfig,
    score_subset_plausibility, build_similarity_context, bulk_fuzzy_recover,
)
import compliance_agent
from exception_diagnosis import diagnose_batch_exceptions
from linkage import (
    LinkageResult, build_candidate_links, link_confidence, txn_key, members_of,
)
import audit
from fee_audit import run_fee_audit, MethodRateCard, FeeAuditFinding
from india_tax import ist_date
import calibration_map

# The refusal gates moved to recon_gates.py — one concern, and this file
# was not it. Re-exported because callers and tests reach for them here.
from recon_gates import (  # noqa: E402,F401
    MIN_AUTOCLEAR_CONFIDENCE, FUZZY_RECOVERY_CONFIDENCE,
    UNANCHORED_AUTOCLEAR_LIMIT,
    _withhold_if_unevidenced, _apply_confidence_gate,
    _withhold_cross_batch_double_claims,
)

# Split by responsibility; every name is still importable from here.
from recon_report import (  # noqa: E402,F401 - re-exported; callers import them here
    ReconciliationReport,
    _count_by_reason,
    _compute_false_positive_cost,
    _build_report_and_tie_out,
)
from candidate_filters import (  # noqa: E402,F401 - re-exported; callers import them here
    NON_SETTLING_STATUSES,
    _filter_to_settlement_currency,
    _filter_out_non_settling,
)
from tiered_solve import (  # noqa: E402,F401 - re-exported; callers import them here
    _tiebreak_ambiguous_match,
    _SolveOutcome,
    _solve_in_tiers,
    _collect_unmatched,
    _tiebreak_if_ambiguous,
)


@dataclass
class _NMBatchPrep:
    """One batch's own pre-solve state within a joint N:M group — the exact
    per-batch pipeline reconcile_batch runs before it ever calls the solver,
    computed once here and reused by both the joint solve and, if needed,
    the independent per-batch fallback below."""
    batch: SettlementBatch
    fee_breakdown: object
    gross_target: int
    batch_candidates: list[NormalizedTxn]   # currency/status filtered, NOT windowed
    windowed: list[NormalizedTxn]
    link_result: LinkageResult
    narrowed: list[NormalizedTxn]
    forced_keys: set[str]


def reconcile_many(
    batches: list[SettlementBatch],
    candidates: list[NormalizedTxn],
    settlement_window_days: int = 5,
    subset_config: SubsetSumConfig | None = None,
    rate_card: FeeRateCard = DEFAULT_RATE_CARD,
) -> list[ReconciliationReport]:
    """
    N:M: several settlements solved at once against one shared pool, so a
    payment two settlements could claim is assigned by the solver, not by order.

    Reused per batch from the 1:N path: linkage narrowing, forced anchored
    refunds, a per-target ambiguity probe, and the same refusal gates. Not
    reused: the evidence tiers and the substitutability guard, which have not
    been generalised to several targets. If the joint model is infeasible (one
    batch's leg missing makes the whole model infeasible), every batch falls
    back to reconcile_batch against the original pool, with siblings' anchored
    records withheld, and double claims across the group are then withheld.
    The joint solve prevents double claims by construction (AddAtMostOne).
    """
    if not batches:
        return []

    cfg = subset_config or SubsetSumConfig()

    safe_candidates, _ = compliance_agent.scan(candidates)
    audit.log_decision(
        batch_id="+".join(b.batch_id for b in batches),
        agent="orchestrator",
        detail=(
            f"Joint N:M solve requested for {len(batches)} batch(es) sharing "
            f"one pool of {len(candidates)} candidate(s) "
            f"({len(safe_candidates)} after compliance screening)."
        ),
    )

    prep: list[_NMBatchPrep] = []
    for batch in batches:
        fee_breakdown = compute_fee_breakdown(batch, rate_card, safe_candidates)
        gross_target = fee_breakdown.gross_target_cents(batch.net_amount_cents)

        batch_candidates = _filter_to_settlement_currency(batch, safe_candidates)
        batch_candidates = _filter_out_non_settling(batch, batch_candidates)
        windowed = filter_candidates_by_settlement_window(
            batch_candidates, batch.settled_at_utc, settlement_window_days
        )
        link_result = build_candidate_links(batch, windowed, settlement_window_days)
        narrowed = link_result.candidates
        forced_keys = _anchored_negatives(batch.batch_id, narrowed)

        audit.log_decision(
            batch_id=batch.batch_id, agent="linkage",
            detail=f"[joint] [{link_result.method}] {link_result.reasoning}",
        )

        prep.append(_NMBatchPrep(
            batch=batch, fee_breakdown=fee_breakdown, gross_target=gross_target,
            batch_candidates=batch_candidates, windowed=windowed,
            link_result=link_result, narrowed=narrowed, forced_keys=forced_keys,
        ))

    # Anchor evidence is passed in so a candidate that NAMES one settlement
    # cannot be assigned to a different one. Contention between equal-valued
    # legs is invisible to arithmetic, so without this the solver resolved it
    # by whichever assignment it reached first. See build_union_pool.
    union, eligible = build_union_pool(
        [p.narrowed for p in prep],
        [p.link_result.anchor_keys for p in prep],
    )
    target_cents_list = [p.gross_target for p in prep]
    forced_per_target = [p.forced_keys for p in prep]

    joint = exact_subset_sum_nm(
        union, eligible, target_cents_list,
        tolerance_cents=cfg.tolerance_cents,
        time_limit_s=cfg.solver_time_limit_s,
        num_search_workers=cfg.num_search_workers,
        forced_per_target=forced_per_target,
    ) if union else None

    reports: list[ReconciliationReport] = []

    if joint is not None:
        nm_probe: dict = {}
        ambiguous_flags = probe_for_alternate_nm_assignment(
            union, eligible, joint, target_cents_list,
            cfg.tolerance_cents, cfg.probe_time_limit_s, cfg.ambiguity_probe_limit,
            num_search_workers=cfg.num_search_workers,
            forced_per_target=forced_per_target,
            outcome=nm_probe,
        )
        probe_timed_out = bool(nm_probe.get("timed_out"))
        for i, p in enumerate(prep):
            matched_txns = joint.matched[i]
            achieved_sum = joint.achieved_sums[i]
            ambiguous = ambiguous_flags[i]
            diff = abs(achieved_sum - p.gross_target)

            result = MatchResult(
                batch_id=p.batch.batch_id,
                matched_txn_ids=[t.source_txn_id for t in matched_txns],
                matched_keys=[txn_key(t) for t in matched_txns],
                method=MatchMethod.EXACT_SUBSET_SUM,
                # Same 0.36/1.0 split as subset_sum.match_batch's arithmetic
                # confidence, and the same meaning: 1.0 is not a guess, the
                # probe found no alternate joint assignment where THIS
                # target's set differed, within the probed budget. It is
                # not independently calibrated for the joint case — no N:M
                # benchmark exists yet to measure it against, unlike the
                # 1:N bands above, so this borrows the 1:N number honestly
                # rather than inventing an untested one of its own.
                confidence=(0.36 if ambiguous else
                            UNPROVEN_UNIQUE_CONFIDENCE if probe_timed_out else 1.0),
                matched_sum_cents=achieved_sum,
                target_cents=p.gross_target,
                cleared=not ambiguous,
                ambiguous=ambiguous,
                withheld_reason="alternate_assignment" if ambiguous else None,
                reasoning=(
                    f"Joint N:M subset-sum match (CP-SAT, {len(batches)} "
                    f"batch(es) solved together): {len(matched_txns)} "
                    f"transactions sum to {achieved_sum} cents (target "
                    f"{p.gross_target} cents, diff {diff} cents)."
                    + (
                        " WARNING (bounded probe, not exhaustive): this "
                        "settlement's own matched set varied across at "
                        "least one alternate joint assignment found within "
                        "the probe budget — not uniquely determined by "
                        "arithmetic alone within the probed alternatives."
                        if ambiguous else ""
                    )
                ),
            )

            if probe_timed_out and not ambiguous:
                result.reasoning += UNPROVEN_UNIQUE_NOTE

            _withhold_if_unevidenced(
                p.batch, result, p.narrowed, p.link_result, p.link_result.anchor_keys
            )
            audit.log_decision(
                batch_id=p.batch.batch_id, agent="subset_sum_nm", detail=result.reasoning
            )
            _apply_confidence_gate(p.batch, result, p.narrowed, p.link_result)

            exceptions, unmatched = _collect_unmatched(
                p.batch, result, p.batch_candidates, p.windowed, p.gross_target,
                FuzzyMatchConfig(),
            )
            if unmatched:
                exceptions = diagnose_batch_exceptions(unmatched, p.windowed, p.batch.batch_id)
                for exc in exceptions:
                    audit.log_decision(
                        batch_id=p.batch.batch_id, agent="exception_diagnosis",
                        detail=f"{exc.reason.value}: {exc.diagnosis_note}",
                    )

            reports.append(_build_report_and_tie_out(
                p.batch, result, p.windowed, exceptions, p.fee_breakdown, p.gross_target
            ))
    else:
        audit.log_decision(
            batch_id="+".join(b.batch_id for b in batches), agent="orchestrator",
            detail=(
                "Joint CP-SAT solve found no assignment satisfying every "
                f"target across this group of {len(batches)} simultaneously "
                "(or linkage left no candidate eligible for any of them). "
                "Falling back to reconciling each batch independently "
                "through the full 1:N pipeline against the shared pool."
            ),
        )
        # Anchor evidence binds in the fallback too: a batch alone cannot see that a
        # record names its sibling, and once took a sibling's equal-valued leg at
        # 0.91. Records anchored to another settlement in the group are withheld
        # from this batch's pool.
        for i, p in enumerate(prep):
            foreign_anchors: set[str] = set()
            for j, sibling in enumerate(prep):
                if j != i:
                    foreign_anchors |= sibling.link_result.anchor_keys
            foreign_anchors -= p.link_result.anchor_keys

            batch_pool = (
                [t for t in safe_candidates if txn_key(t) not in foreign_anchors]
                if foreign_anchors else safe_candidates
            )
            if foreign_anchors:
                audit.log_decision(
                    batch_id=p.batch.batch_id, agent="linkage",
                    detail=(
                        f"[fallback] {len(foreign_anchors)} candidate(s) "
                        f"reference a different settlement in this group and "
                        f"were withheld from this batch's pool."
                    ),
                )
            reports.append(reconcile_batch(
                p.batch, batch_pool, subset_config=cfg,
                settlement_window_days=settlement_window_days, rate_card=rate_card,
            ))
        _withhold_cross_batch_double_claims(reports)

    return reports


def reconcile_batch(
    batch: SettlementBatch,
    candidates: list[NormalizedTxn],
    subset_config: SubsetSumConfig | None = None,
    fuzzy_config: FuzzyMatchConfig | None = None,
    settlement_window_days: int = 5,
    enable_tiebreak: bool = True,
    rate_card: FeeRateCard = DEFAULT_RATE_CARD,
) -> ReconciliationReport:
    """
    Runs one settlement batch through the full pipeline end to end.

    `rate_card` is a parameter because deductions are a property of the
    processor agreement, not of the engine. It was previously hardcoded to
    DEFAULT_RATE_CARD, which silently imposed one merchant's 2%+1% terms on
    every batch — wrong for any other processor, and wrong for feeds that are
    already net of fees, where the correct card is zero and adding phantom
    deductions moves the target off the answer entirely.
    """
    cfg = subset_config or SubsetSumConfig()
    fuzz_cfg = fuzzy_config or FuzzyMatchConfig()

    # Agent 2: reconstruct gross target from net settlement amount
    fee_breakdown = compute_fee_breakdown(batch, rate_card, candidates)
    gross_target = fee_breakdown.gross_target_cents(batch.net_amount_cents)
    audit.log_decision(
        batch_id=batch.batch_id,
        agent="fee_decomposition",
        detail=(
            f"net={batch.net_amount_cents}c -> gross_target={gross_target}c, "
            f"deductions={fee_breakdown.total_deductions_cents}c "
            f"[{fee_breakdown.basis}]"
            + ("" if fee_breakdown.basis == "declared" else
               f" via rate card {rate_card.gateway_fee_bps}bps gateway + "
               f"{rate_card.tax_withholding_bps}bps tax + "
               f"{rate_card.flat_fee_cents}c flat — an ESTIMATE; the target "
               f"moves if the processor's actual deductions differ")
        ),
    )

    candidates = _filter_to_settlement_currency(batch, candidates)
    candidates = _filter_out_non_settling(batch, candidates)

    windowed_candidates = filter_candidates_by_settlement_window(
        candidates, batch.settled_at_utc, settlement_window_days
    )
    audit.log_decision(
        batch_id=batch.batch_id,
        agent="settlement_window_filter",
        detail=f"{len(candidates)} candidates -> {len(windowed_candidates)} "
               f"within {settlement_window_days}-day window",
    )

    # Agent 2b: linkage — constrain candidates BEFORE the solver.
    #
    # Subset-sum cannot identify a settlement on its own: the solver may use
    # any subset size, so the space competing for one target is 2^n (1.2e18
    # for a pool of 60) against a target with only ~2e6 distinct paise
    # values. Measured auto-clear accuracy without this stage was 0.0% across
    # 120 benchmark scenarios. Linkage answers "which transactions are
    # plausibly connected to this settlement at all", so the arithmetic runs
    # over tens of candidates instead of tens of thousands and is actually
    # determined. See linkage.py for the full reasoning.
    link_result = build_candidate_links(
        batch, windowed_candidates, settlement_window_days
    )
    solver_candidates = link_result.candidates

    # AMONGRESOLVER_NO_LINKAGE=1 hands the solver the whole windowed pool.
    #
    # This exists so the project's central claim can be RUN rather than
    # believed. "Subset-sum alone scores 0.0%" sat in a comment and in the
    # README as a historical measurement, with no way for a reader to
    # reproduce it — which is the weakest possible form of the strongest
    # thing this engine has to say.
    #
    # Off by default and deliberately an environment variable rather than a
    # request field: this is a demonstration harness, not a mode anyone should
    # be able to reach through the API.
    if os.environ.get("AMONGRESOLVER_NO_LINKAGE", "").strip() == "1":
        # Discard the EVIDENCE, not just the narrowing: keeping anchors and tiers made
        # this flag measure 62.0%, identical to linkage on. Arithmetic alone means one
        # tier and nothing for the guards to weigh.
        solver_candidates = windowed_candidates
        link_result = LinkageResult(
            candidates=windowed_candidates,
            scored=[],
            pool_before=len(windowed_candidates),
            pool_after=len(windowed_candidates),
            method="no_linkage_signal",
            reasoning=(
                "Linkage bypassed (AMONGRESOLVER_NO_LINKAGE=1): the solver is "
                "given the whole settlement window with no identity evidence."
            ),
        )
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="linkage",
            detail=(
                f"BYPASSED (AMONGRESOLVER_NO_LINKAGE=1). Handing all "
                f"{len(windowed_candidates)} windowed candidates to the solver "
                f"with no anchors, scores or tiers, so arithmetic alone has to "
                f"identify the members. This is the comparison, not the product."
            ),
        )
    audit.log_decision(
        batch_id=batch.batch_id,
        agent="linkage",
        detail=f"[{link_result.method}] {link_result.reasoning}",
    )
    if link_result.em:
        em = link_result.em
        top = (em.get("strongest_evidence") or [{}])[0]
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="linkage",
            detail=(
                f"Learned linkage (Fellegi-Sunter, {'; '.join(em.get('notes') or [])}): "
                f"cohort of {em.get('cohort_size', 0)} candidate(s) for about "
                f"{em.get('expected_members')} expected member(s). Strongest evidence: "
                f"{top.get('comparison')}={top.get('level')} at "
                f"{top.get('weight_bits')} bits (m {top.get('m')}, u {top.get('u')})."
            ),
        )

    _solved = _solve_in_tiers(
        batch, solver_candidates, windowed_candidates, link_result, gross_target, cfg
    )
    result = _solved.result
    solver_candidates = _solved.solver_candidates
    scores = _solved.scores
    anchor_keys = _solved.anchor_keys
    anchor_ids = _solved.anchor_ids

    _withhold_if_unevidenced(
        batch, result, solver_candidates, link_result, anchor_keys
    )

    audit.log_decision(
        batch_id=batch.batch_id,
        agent="subset_sum",
        detail=result.reasoning,
    )

    _apply_confidence_gate(batch, result, solver_candidates, link_result)

    # Agent 3b: tiebreak ambiguous matches via fuzzy plausibility scoring
    _tiebreak_if_ambiguous(batch, result, windowed_candidates, gross_target, cfg,
                           enable_tiebreak, learned_keys=link_result.learned_keys)

    exceptions, unmatched = _collect_unmatched(
        batch, result, candidates, windowed_candidates, gross_target, fuzz_cfg
    )

    # Agent 5: diagnose what's left
    if unmatched:
        exceptions = diagnose_batch_exceptions(unmatched, windowed_candidates, batch.batch_id)
        for exc in exceptions:
            audit.log_decision(
                batch_id=batch.batch_id,
                agent="exception_diagnosis",
                detail=f"{exc.reason.value}: {exc.diagnosis_note}",
            )

    report = _build_report_and_tie_out(
        batch, result, windowed_candidates, exceptions, fee_breakdown, gross_target
    )
    report.learned_linkage = link_result.em
    return report
