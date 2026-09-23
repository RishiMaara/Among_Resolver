"""
The gates that refuse to auto-clear a set the arithmetic likes:

  _withhold_if_unevidenced            does anything tie these records to
                                      THIS settlement?
  _apply_confidence_gate              is structural confidence high enough?
  _withhold_cross_batch_double_claims do two settlements claim one payment?

Each mutates the MatchResult in place and appends its reasoning. Every rule
answers a measured failure (FAILURE_LOG.md).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Import only for the annotation. orchestrator imports this module, so a
    # runtime import here would be circular; `from __future__ import
    # annotations` keeps the reference a string at runtime.
    from recon_report import ReconciliationReport

from schema import MatchResult, SettlementBatch, NormalizedTxn, ExceptionRecord
from linkage import LinkageResult, link_confidence, txn_key, members_of
import audit


# Above this candidate count, an exact subset sum with no anchor evidence is
# not credible as an identification. Derived from the density argument in
# linkage.py: 2^n subsets compete for a target with ~2e6 distinct paise
# values, so uniqueness stops being plausible around n=20-25.
#
# Set at the BOTTOM of that range, not the top. It was 25 — the generous end —
# and the realistic benchmark then produced false clears at pools of 22 and 23:
# with an estimated fee target and a 10-paise tolerance band, C(22,5) is 26,334
# subsets competing for a 20-paise-wide window, and the arithmetic is simply
# not determined there. The two errors are not symmetric, so the conservative
# end of a range this uncertain is the defensible one.
UNANCHORED_AUTOCLEAR_LIMIT = int(
    os.environ.get("UNANCHORED_AUTOCLEAR_LIMIT", "20")
)

# Minimum structural confidence to auto-clear, set where measured reliability
# begins: every prediction at or above 0.85 was right (103/103), while the
# 0.50-0.70 band was right 36%. 0.90 withheld a fully anchored 50K set scored
# 0.87 that was exactly right. Override: AUTOCLEAR_MIN_CONFIDENCE.
MIN_AUTOCLEAR_CONFIDENCE = float(
    os.environ.get("AUTOCLEAR_MIN_CONFIDENCE", "0.85")
)

# What a fuzzy recovery bundle is worth. It once reported the pair threshold
# (0.90); over 167 bundles the exact set was never right and 4% of a bundle
# was the answer. A bundle is a place to look; the assertion keeps it below
# the gate by construction.
FUZZY_RECOVERY_CONFIDENCE = 0.05
assert FUZZY_RECOVERY_CONFIDENCE < MIN_AUTOCLEAR_CONFIDENCE, (
    "A fuzzy recovery bundle must never be reportable as clearable; "
    "similarity is evidence of association, not of arithmetic."
)


def _withhold_if_unevidenced(
    batch: SettlementBatch,
    result: MatchResult,
    solver_candidates: list[NormalizedTxn],
    link_result,
    anchor_keys: set,
) -> None:
    """Refuse to auto-clear a sum that no evidence ties to this settlement.

    Mutates `result` in place, as the inline block it replaces did: this is
    the point where an arithmetically valid answer is demoted to a withheld
    one, and every field it sets - cleared, ambiguous, withheld_reason and
    the appended reasoning - is read by the caller straight afterwards.
    """
    # Clearing needs evidence that the MATCHED records belong to this settlement,
    # or a pool small enough for the sum to be determined (2^n subsets against
    # ~2e6 paise values stops being unique around n=20-25). An anchor vouches only
    # for the record carrying it: a late leg leaves anchors in the pool and no
    # reachable answer, and noise that sums was once cleared beside them.
    matched_id_set = set(result.matched_txn_ids)
    matched_keys = {txn_key(t) for t in members_of(result, solver_candidates)}
    matched_anchored = bool(anchor_keys & matched_keys)

    # The small-pool exemption holds only when nothing is referenced. If anchors
    # exist and the matched set uses none, the evidence points elsewhere and pool
    # size does not rescue it (21 records of noise, cleared).
    evidence_ignored = bool(anchor_keys) and not matched_anchored
    pool_too_large = len(solver_candidates) > UNANCHORED_AUTOCLEAR_LIMIT

    # No linkage signal at all is a finding a small pool cannot override: a unique
    # sum is only a determination if the answer is in the pool. On a real SBI
    # statement 69 of 189 credits had some coincidental subset of UPI debits.
    no_evidence_at_all = link_result.method == "no_linkage_signal"

    # Partial anchoring blocks only when the match LEFT ANCHORS UNUSED: blocking
    # every partly anchored match cost ten right answers to stop two wrong ones,
    # and the wrong ones were those that ignored anchors.
    anchors_in_pool = anchor_keys & {txn_key(t) for t in solver_candidates}
    partially_anchored = (
        bool(anchor_keys)
        and matched_anchored
        and not (matched_keys <= anchor_keys)
        and bool(anchors_in_pool - matched_keys)
    )

    if result.cleared and (
        partially_anchored
        or (not matched_anchored
            and (evidence_ignored or pool_too_large or no_evidence_at_all))
    ):
        result.cleared = False
        result.ambiguous = True
        result.withheld_reason = "no_corroborating_evidence"
        anchor_note = (
            "linkage found no reference, cluster or cross-source evidence "
            "anywhere in this pool, so the match rests on the arithmetic alone"
            if no_evidence_at_all and not anchor_keys else
            f"only {len(anchor_keys & matched_keys)} of the "
            f"{len(matched_id_set)} matched record(s) reference this "
            f"settlement, so the rest are unevidenced"
            if partially_anchored else
            "no candidate references this settlement"
            if not anchor_keys else
            f"none of the {len(matched_id_set)} matched record(s) references "
            f"this settlement (the {len(anchor_keys)} record(s) that do were "
            f"not selected)"
        )
        # The second clause only applies when pool size is the reason. Saying
        # "the pool of 8 is too large" when the actual finding is "no evidence
        # exists" tells the reviewer the wrong thing to go and fix.
        size_clause = (
            f", and the pool of {len(solver_candidates)} is too large for an "
            f"exact sum to establish uniqueness on its own"
            if pool_too_large else ""
        )
        result.reasoning += (
            f" Withheld from auto-clear: {anchor_note}{size_clause}. "
            f"Routed for human review."
        )
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="linkage",
            detail=(
                f"Subset summed to target over {len(solver_candidates)} "
                f"candidates with no anchored member in the matched set. "
                f"Arithmetic alone does not identify a settlement at this pool "
                f"size — refusing to auto-clear."
            ),
        )


def _apply_confidence_gate(
    batch: SettlementBatch,
    result: MatchResult,
    solver_candidates: list[NormalizedTxn],
    link_result: LinkageResult
) -> None:
    """Report how the match was FOUND, and gate auto-clear on structural confidence."""
    if not result.matched_txn_ids:
        return

    # Keys, not bare ids: a matched id that collides across feeds must not
    # borrow another transaction's linkage score. See linkage.txn_key and
    # link_confidence's docstring.
    matched_keys = {txn_key(t) for t in members_of(result, solver_candidates)}
    structural = link_confidence(link_result, matched_keys)
    result.confidence = min(result.confidence, structural)
    result.reasoning += (
        f" Linkage: {link_result.method}, structural confidence "
        f"{structural:.2f} over {link_result.pool_after} linked candidate(s)."
    )

    # Applies at every pool size: a small pool with a weak signal (a two-record
    # shared token) once cleared at 0.22 past both guards.
    if (
        result.cleared
        and result.confidence < MIN_AUTOCLEAR_CONFIDENCE
    ):
        result.cleared = False
        result.ambiguous = True
        result.withheld_reason = result.withheld_reason or "below_confidence_gate"
        result.reasoning += (
            f" Withheld from auto-clear: structural confidence "
            f"{result.confidence:.2f} is below the {MIN_AUTOCLEAR_CONFIDENCE:.2f} "
            f"required to release without review. The matched set is "
            f"reported as a proposal."
        )
        audit.log_decision(
            batch_id=batch.batch_id,
            agent="orchestrator",
            detail=(
                f"Auto-clear withheld: confidence {result.confidence:.2f} < "
                f"{MIN_AUTOCLEAR_CONFIDENCE:.2f}. Matched set surfaced for "
                f"human review rather than released."
            ),
        )


def _withhold_cross_batch_double_claims(
    reports: list[ReconciliationReport],
) -> None:
    """
    Refuse to auto-clear two settlements that claim the same payment. The joint
    solve prevents this by construction; the independent fallback does not (28
    double claims in 88 joint batches). Neither side is released, since nothing
    here can say which is wrong. Only cleared reports are considered.
    """
    claimed_by: dict[str, list[MatchResult]] = {}
    for report in reports:
        result = report.match_result
        if not result.cleared:
            continue
        # Keys where the result carries them: two feeds' "1001" are two
        # payments, and only the same payment claimed twice is a conflict.
        for txn_id in (result.matched_keys or result.matched_txn_ids):
            claimed_by.setdefault(txn_id, []).append(result)

    conflicted: dict[int, set[str]] = {}
    for txn_id, holders in claimed_by.items():
        if len(holders) > 1:
            for result in holders:
                conflicted.setdefault(id(result), set()).add(txn_id)

    if not conflicted:
        return

    for report in reports:
        result = report.match_result
        shared = conflicted.get(id(result))
        if not shared:
            continue
        rivals = sorted(
            other.batch_id
            for other in {
                id(h): h for txn in shared for h in claimed_by[txn]
            }.values()
            if other.batch_id != result.batch_id
        )
        result.cleared = False
        result.ambiguous = True
        result.withheld_reason = "cross_batch_double_claim"
        result.reasoning += (
            f" Withheld from auto-clear: {len(shared)} matched transaction(s) "
            f"are also claimed by {', '.join(rivals)}, and a payment cannot "
            f"fund more than one settlement. Routed for human review."
        )
        audit.log_decision(
            batch_id=result.batch_id,
            agent="orchestrator",
            detail=(
                f"Cross-batch double claim on {sorted(shared)} with "
                f"{', '.join(rivals)} — refusing to auto-clear either side."
            ),
        )


