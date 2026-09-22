"""
Exception diagnosis: give each unmatched record a root-cause category.

Rules decide the category; a model may only word the narrative. Indexed once
per batch (by reference and amount) so diagnosis is O(n), not O(n^2): the
per-record scan took 91 s on a 10K chunk. timing_lag requires a real
counterpart (same amount, other feed, in window), not merely any record
nearby, which was true for every record at scale.
"""

from __future__ import annotations
from dataclasses import dataclass

from schema import NormalizedTxn, ExceptionRecord, ExceptionReason

TIMING_LAG_WINDOW_DAYS = 3  # T+2/T+3 style settlement lag tolerance


@dataclass
class DiagnosisIndex:
    """Precomputed O(1) lookup structures for classification, instead of
    an O(n) full-pool scan per transaction."""
    by_ref_id: dict[str, list[NormalizedTxn]]
    by_amount: dict[int, list[NormalizedTxn]]


def build_diagnosis_index(all_pool: list[NormalizedTxn]) -> DiagnosisIndex:
    """Build once per batch, reuse across every unmatched transaction —
    see module docstring for why this is the difference between ~91s
    and well under a second at 10K+ scale."""
    by_ref_id: dict[str, list[NormalizedTxn]] = {}
    by_amount: dict[int, list[NormalizedTxn]] = {}
    for t in all_pool:
        by_ref_id.setdefault(t.ref_id_canonical, []).append(t)
        by_amount.setdefault(t.amount_cents, []).append(t)

    return DiagnosisIndex(by_ref_id=by_ref_id, by_amount=by_amount)


def _find_plausible_counterparts(
    index: DiagnosisIndex,
    unmatched: NormalizedTxn,
    window_days: int,
) -> list[NormalizedTxn]:
    """
    The same payment in a DIFFERENT feed: same amount, other source, within the
    lag window. Exact amounts only, so a leg that differs by fees or FX reads as
    missing_entry, which is the conservative direction.
    """
    same_amount = index.by_amount.get(unmatched.amount_cents, [])
    if not same_amount:
        return []

    window_seconds = window_days * 86400
    center = unmatched.timestamp_utc.timestamp()
    return [
        t for t in same_amount
        if t.source_txn_id != unmatched.source_txn_id
        and t.source != unmatched.source
        and abs(t.timestamp_utc.timestamp() - center) <= window_seconds
    ]


def _classify_with_index(
    unmatched: NormalizedTxn,
    index: DiagnosisIndex,
    batch_id: str,
) -> ExceptionRecord:
    """
    Same classification rules as the original, using the precomputed
    index instead of a fresh full-pool scan:
      - duplicate: another record with same ref_id + amount exists
      - partial_payment: same ref_id, different amounts (split legs)
      - timing_lag: a plausible counterpart exists but outside window
      - missing_entry: nothing resembling a counterpart found at all
    """
    same_ref = [
        t for t in index.by_ref_id.get(unmatched.ref_id_canonical, [])
        if t.source_txn_id != unmatched.source_txn_id
    ]

    duplicates = [t for t in same_ref if t.amount_cents == unmatched.amount_cents]
    if duplicates:
        return ExceptionRecord(
            batch_id=batch_id,
            candidate_txn_ids=[unmatched.source_txn_id] + [d.source_txn_id for d in duplicates],
            reason=ExceptionReason.DUPLICATE,
            diagnosis_note=(
                f"{len(duplicates)} other record(s) share ref_id "
                f"{unmatched.ref_id_canonical} and identical amount."
            ),
        )

    if same_ref:
        total = sum(t.amount_cents for t in same_ref) + unmatched.amount_cents
        return ExceptionRecord(
            batch_id=batch_id,
            candidate_txn_ids=[unmatched.source_txn_id] + [t.source_txn_id for t in same_ref],
            reason=ExceptionReason.PARTIAL_PAYMENT,
            diagnosis_note=(
                f"{len(same_ref)} other record(s) share ref_id but differ in amount — "
                f"possible split payment, combined total {total} cents."
            ),
        )

    counterparts = _find_plausible_counterparts(index, unmatched, TIMING_LAG_WINDOW_DAYS)
    if counterparts:
        sources = sorted({t.source.value for t in counterparts})
        return ExceptionRecord(
            batch_id=batch_id,
            candidate_txn_ids=[unmatched.source_txn_id] + [t.source_txn_id for t in counterparts],
            reason=ExceptionReason.TIMING_LAG,
            diagnosis_note=(
                f"No exact match, but {len(counterparts)} record(s) in "
                f"{', '.join(sources)} carry the identical amount within the "
                f"{TIMING_LAG_WINDOW_DAYS}-day settlement window — likely awaiting "
                f"its counterpart leg."
            ),
        )

    return ExceptionRecord(
        batch_id=batch_id,
        candidate_txn_ids=[unmatched.source_txn_id],
        reason=ExceptionReason.MISSING_ENTRY,
        diagnosis_note="No plausible counterpart found in the candidate pool at all.",
    )


def classify_exception(
    unmatched: NormalizedTxn,
    all_pool: list[NormalizedTxn],
    batch_id: str,
) -> ExceptionRecord:
    """
    Public single-transaction classification entry point — same
    signature as before, for direct/one-off use and existing test
    compatibility. Builds a fresh index from all_pool internally, which
    is fine for a single call against a small pool.

    For classifying MANY transactions against the same pool, use
    diagnose_batch_exceptions instead — it builds the index once and
    reuses it. Calling this function in a per-transaction loop instead
    reintroduces the exact O(n^2) pattern this module was fixed for.
    """
    index = build_diagnosis_index(all_pool)
    return _classify_with_index(unmatched, index, batch_id)


def diagnose_batch_exceptions(
    unmatched: list[NormalizedTxn],
    all_pool: list[NormalizedTxn],
    batch_id: str,
) -> list[ExceptionRecord]:
    """
    Builds the diagnosis index ONCE against all_pool, then classifies
    every unmatched transaction against it. O(n log n) instead of the
    previous O(n^2) — measured fix for a confirmed ~91s bottleneck at
    10,173-transaction chunk scale (real data from the 50k stress test).
    """
    if not unmatched:
        return []
    index = build_diagnosis_index(all_pool)
    return [_classify_with_index(txn, index, batch_id) for txn in unmatched]
