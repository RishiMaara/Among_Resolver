"""
The gates that refuse to auto-clear.

Three independent refusals, extracted from orchestrator.py because they are
one concern and it was not: given a set the arithmetic likes, each asks a
different question about whether releasing it is defensible.

  _withhold_if_unevidenced          does anything tie these records to THIS
                                    settlement, or does the sum just happen
                                    to work?
  _apply_confidence_gate            is the structural confidence high enough
                                    to release without a human?
  _withhold_cross_batch_double_claims  are two settlements claiming the same
                                    payment?

Each mutates the MatchResult in place, as the inline blocks they replace did,
and each appends its reasoning — the caller reads every field straight
afterwards. The thresholds live here too, beside the guards that read them,
rather than three hundred lines away from them.

Every comment in this file records a measured failure rather than an
intention. That is deliberate: these are the rules standing between a
plausible sum and someone else's money, and "why is this here" is the
question a reviewer will ask about each one.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Import only for the annotation. orchestrator imports this module, so a
    # runtime import here would be circular; `from __future__ import
    # annotations` keeps the reference a string at runtime.
    from orchestrator import ReconciliationReport

from schema import MatchResult, SettlementBatch, NormalizedTxn, ExceptionRecord
from linkage import LinkageResult, link_confidence, txn_key
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

# Minimum structural confidence required to auto-clear without review.
#
# Set where the measured reliability actually begins, not at a round number.
# calibration.py buckets every prediction against its outcome:
#
#     [0.93, 1.01)   n=62   said 0.955   actual 1.000
#     [0.85, 0.93)   n=41   said 0.876   actual 1.000
#     [0.70, 0.85)   n=5    said 0.800   actual 1.000
#     [0.50, 0.70)   n=11   said 0.540   actual 0.364   <- overconfident
#     [0.00, 0.50)   n=61   said 0.176   actual 0.098   <- overconfident
#
# Everything at or above 0.85 was correct in all 103 observations; below 0.70
# it is a coin flip or worse. 0.85 is therefore the boundary the data draws.
#
# These counts were n=31 and "93 observations" until the benchmark was pinned
# to a single CP-SAT worker. They were not wrong then, they were one sample of
# a measurement that moved: the parallel solver picks differently between runs
# on scenarios where more than one subset is valid. See benchmark.run_scenario.
#
# This was 0.90 briefly, which is the kind of round number that looks careful
# and is not: the 50K stress run scores 0.87 — fully anchored, penalised for a
# large pool — finds all 55 members with precision and recall of 1.0, and was
# withheld for being three hundredths under an arbitrary line. Override with
# AUTOCLEAR_MIN_CONFIDENCE to re-measure the trade-off.
MIN_AUTOCLEAR_CONFIDENCE = float(
    os.environ.get("AUTOCLEAR_MIN_CONFIDENCE", "0.85")
)

# What the fuzzy recovery bundle is worth, as a number rather than as a
# configuration constant that happened to be in scope.
#
# This field used to be set to `fuzz_cfg.confidence_threshold` — 0.90, the
# value that decides whether a fuzzy PAIR is good enough to act on. Assigning
# a threshold to a confidence is a category error, and the number it produced
# was the single worst-calibrated thing this engine reported:
#
#     scripts/edge_case_suite_1000.py, 1,050 scored batches
#     167 fuzzy_semantic results, every one of them saying 0.900
#       exact set == truth        0.0%      <- said 0.90
#       mean precision            0.041     <- 4% of the bundle is the answer
#       mean recall               0.692
#       truth fully inside set    39.5%
#       median bundle size        63 transactions
#
# It also sat ABOVE MIN_AUTOCLEAR_CONFIDENCE. Nothing cleared on it, because
# `cleared` is already False by the time this path runs — but the only thing
# standing between a 63-record dragnet and an auto-clear was that no code read
# the two fields in the other order. That is not a safety property, it is an
# accident, and the assertion below turns it into one.
#
# The floor mirrors link_confidence's own 0.05: a set the arithmetic could not
# reach is a place to start looking, not a claim. What the bundle is actually
# good for — the truth is somewhere inside it 39.5% of the time — is reported
# in the reasoning, where a reviewer can act on it, instead of being flattened
# into a score that means something else.
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
    # Unanchored auto-clear guard.
    #
    # Auto-clearing needs either evidence that these records belong to this
    # settlement, or a pool small enough that the arithmetic is genuinely
    # determined. With neither, an exact sum is not a match — it is a
    # coincidence, and there are astronomically many available: the space
    # competing for one target is 2^n against ~2e6 distinct paise values, so
    # uniqueness stops being plausible somewhere around n=20-25 and is gone
    # entirely beyond that. The solver's ambiguity probe only samples a
    # couple of alternates, so "not ambiguous" over a large pool is weak
    # evidence, not proof.
    #
    # Measured: with the settlement reference stripped from every true
    # member, linkage still narrowed 50,000 -> 400 on cluster signal alone
    # while every anchor was scoped away, and the solver returned a
    # confident 67-record set that was simply wrong. That is a false clear —
    # the one outcome this engine is built to never produce.
    #
    # Small pools are exempt because there the subset-sum really is
    # determined, which is why a 5-candidate batch with no references still
    # clears correctly.
    # The test is whether the MATCHED SET is anchored, not whether the batch
    # has anchors anywhere.
    #
    # Those are different questions and the difference is a false clear. A
    # settlement whose members DO name it, but where one leg has not arrived
    # yet, has anchors in the pool and no reachable correct answer. The old
    # condition saw the anchors, concluded the batch was well-evidenced, and
    # stood aside while the solver cleared five unrelated noise records that
    # happened to sum to the target. Measured on the realistic benchmark:
    # anchors ['S7_TRUE_0','S7_TRUE_1','S7_TRUE_2'] present, matched set
    # ['S7_N_20','S7_N_24','S7_N_44','S7_N_51','S7_N_52'], intersection empty,
    # cleared=True. Pure noise, auto-cleared, confidently.
    #
    # Evidence does not transfer between records. An anchor vouches for the
    # transaction carrying it and for nothing else, so what matters is whether
    # the records being cleared are themselves evidenced.
    #
    # Neither existing corpus could show this. benchmark.py's batch ids share
    # no canonical form with its references, so anchors were never found and
    # the guard always fired; ReconRiver's ids do match, but its data is clean
    # enough that the true set is always reachable. It needs both at once —
    # anchors present AND the true answer absent from the pool — which is what
    # a late leg does in production every day.
    matched_id_set = set(result.matched_txn_ids)
    matched_keys = {
        txn_key(t) for t in solver_candidates
        if t.source_txn_id in matched_id_set
    }
    matched_anchored = bool(anchor_keys & matched_keys)

    # Two distinct situations, and the small-pool exemption is only sound in
    # one of them:
    #
    #   no anchors anywhere      the settlement is simply not referenced. Over
    #                            a small pool the arithmetic really is
    #                            determined, and this clears correctly.
    #
    #   anchors exist, but NONE  the settlement IS referenced, and the solver
    #   are in the matched set   chose a set containing none of the records
    #                            that reference it. The evidence points
    #                            somewhere other than the answer. Pool size
    #                            does not rescue that, because the problem is
    #                            not degeneracy — it is that the one signal
    #                            available was ignored.
    #
    # Measured: the second case cleared five unrelated noise records over a
    # pool of 21 while three anchored members sat outside the matched set,
    # because 21 was under the small-pool limit. It is the only false clear
    # the realistic benchmark produced.
    evidence_ignored = bool(anchor_keys) and not matched_anchored
    pool_too_large = len(solver_candidates) > UNANCHORED_AUTOCLEAR_LIMIT

    # Linkage saying it found NOTHING is itself a finding, and it must not be
    # overridden by a small pool.
    #
    # `no_linkage_signal` is not "weak evidence" — it is linkage reporting
    # that no reference, no cluster and no cross-source peer exists anywhere
    # in the pool. The only thing left is the arithmetic, and the arithmetic
    # is what this engine exists to say is insufficient. Calibration puts that
    # band at 27.6% accurate.
    #
    # The small-pool exemption assumed a unique sum over few candidates means
    # the answer is determined. That holds only if the answer is IN the pool.
    # Give the engine a window of unrelated traffic and a unique sum is a
    # coincidence, not a determination.
    #
    # Found on a real SBI statement: eight genuine UPI debits, no settlement
    # reference among them because a UPI RRN identifies the payment and not
    # any settlement. Four of them summed to a Rs 500 credit to the paisa and
    # the engine cleared it at 0.22 confidence. Those four payments went to
    # four unrelated people and have nothing to do with that credit. Across
    # the same statement 69 of 189 credits have such a subset, 57 of them
    # have more than one, and one debit is claimed by ten different "matches"
    # — so the coincidence rate is not incidental, it is the norm for retail
    # payment data where amounts are round and repeat.
    no_evidence_at_all = link_result.method == "no_linkage_signal"

    # PARTIAL anchoring is its own case, and the measured worst one.
    #
    # A matched set where some members name the settlement and others do not
    # sits in the 0.42 confidence band, which calibration measures at 43%
    # correct — worse than the unanchored-but-clustered band. The instinct
    # that "at least one member is anchored, so the set is probably right" is
    # exactly backwards: an anchor vouches for the record carrying it and for
    # nothing else, so the unanchored members are unevidenced regardless of
    # the company they keep.
    #
    # Measured: with the fee target perturbed by 1.5bps, a set of five was
    # cleared on the strength of one anchored member and four that were simply
    # wrong. Pool size did not save it — 23 candidates, under the small-pool
    # limit — because the problem is not degeneracy, it is that four of the
    # five records had no evidence at all.
    # Partial anchoring only blocks when the match LEFT ANCHORS UNUSED.
    #
    # Blocking every partially-anchored match cost ten correct answers to
    # prevent two wrong ones. The two wrong ones had a property the ten did
    # not: anchors sat in the pool that the matched set did not include. A
    # match that uses every available anchor and adds unanchored members is
    # reading the evidence; one that ignores anchors is contradicting it.
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
    matched_id_set = set(result.matched_txn_ids)
    matched_keys = {
        txn_key(t) for t in solver_candidates if t.source_txn_id in matched_id_set
    }
    structural = link_confidence(link_result, matched_keys)
    result.confidence = min(result.confidence, structural)
    result.reasoning += (
        f" Linkage: {link_result.method}, structural confidence "
        f"{structural:.2f} over {link_result.pool_after} linked candidate(s)."
    )

    # The gate used to apply only above UNANCHORED_AUTOCLEAR_LIMIT (20)
    # candidates, on the reasoning that an exact sum over a small pool is
    # unlikely to be coincidence. That is true of the ARITHMETIC, but the
    # gate reads `result.confidence` — the LINKAGE confidence — and a small
    # pool with a weak-but-nonzero signal (a two-record shared token, method
    # "reference_cluster") still produces a low structural confidence that
    # this bypass let straight through uncontested. `_withhold_if_unevidenced`
    # only catches the zero-evidence case (`method == "no_linkage_signal"`);
    # a two-record cluster is not zero evidence, so it slipped past both
    # guards and could auto-clear at a structural confidence around 0.22 —
    # the exact shape of the SBI-statement scenario referenced elsewhere in
    # this file. The gate now applies unconditionally; pool size no longer
    # exempts a match from it.
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
    Refuse to auto-clear two settlements that both claim the same payment.

    The joint solve prevents this BY CONSTRUCTION (AddAtMostOne per
    candidate). The independent fallback above does not: each batch is
    reconciled against the whole shared pool with no knowledge of what its
    siblings took, so one payment can satisfy two targets at once and both
    can clear. Measured on 88 joint batches: 28 transactions claimed twice.

    A payment cannot fund two settlements. When it happens, at least one of
    the two answers is wrong and nothing here can say which — so neither is
    released. Both are demoted to review with the conflict named, which is
    the same principle the rest of this file applies to ambiguity: an
    answer the evidence does not determine is a proposal, not a clearance.

    Only CLEARED reports are considered. A batch already withheld is
    already going to a human, and listing a conflict against a proposal
    nobody is about to act on would bury the real one.
    """
    claimed_by: dict[str, list[MatchResult]] = {}
    for report in reports:
        result = report.match_result
        if not result.cleared:
            continue
        for txn_id in result.matched_txn_ids:
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


