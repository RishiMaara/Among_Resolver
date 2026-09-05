"""
Agent 5 — Exception Diagnostics & Routing.

Anything that Agent 3 and Agent 4 couldn't clear lands here. The job is
NOT to dump these into an undifferentiated error queue — it's to diagnose
a root cause category so a human reviewer can act fast instead of
re-investigating from scratch.

Rule-based classification first (cheap, deterministic, auditable).
LLM only writes the human-readable narrative on top of an already-decided
category — it does not choose the category itself.

PERFORMANCE NOTE — same lesson as subset_sum.py and fuzzy_match.py:

  The original version of classify_exception scanned the ENTIRE pool for
  every single unmatched transaction (ref_id match scan + timestamp
  window scan). Fine at hackathon scale (62 transactions), catastrophic
  at real scale: measured at ~91 SECONDS for a single 10,173-transaction
  chunk (~103 million comparisons) from the actual 50k stress dataset.
  This is the third instance of the same trap in this project — naive
  DP, naive per-pair fuzzy scoring, and now naive per-transaction
  exception scanning. The pattern is always the same: an approach that
  looks correct and fast on a 60-item demo silently becomes unusable
  the moment real transaction volume shows up.

  Fix: build an index ONCE per batch (dicts keyed by ref_id and by
  amount, both O(1) lookup) and reuse it across every unmatched
  transaction. O(n^2) -> O(n).

CLASSIFICATION NOTE — why timing_lag requires a real counterpart:

  timing_lag originally fired whenever ANY record existed within
  +/- 3 days. That is vacuous at real scale: the 50K stress dataset
  spans 4.9998 days total against a 3-day window, so the test was true
  for every transaction and timing_lag captured 100% of unmatched
  records. Every exception carried the identical "20082 record(s)"
  note — it was reporting pool density, not diagnosing a root cause.
  With all ref_ids unique in that dataset, the duplicate and
  partial_payment branches could never fire and missing_entry was
  unreachable, so the whole cascade collapsed to a single meaningless
  label.

  It now requires a *plausible counterpart*: the same amount, in a
  DIFFERENT source, within the window — i.e. the same payment's other
  leg. See _find_plausible_counterparts.
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
    A *plausible counterpart* is the same payment appearing in a DIFFERENT
    source (gateway payment <-> bank credit <-> ERP entry): same amount,
    different source, within the settlement-lag window.

    This replaces the previous test, which asked only "does any record at
    all exist within +/- window_days?". That question is vacuous at real
    scale: on the 50K stress dataset the entire pool spans 4.9998 days
    against a 3-day window, so it returned thousands of records for EVERY
    transaction and timing_lag fired 100% of the time. The tell was that
    every exception carried the identical "20082 record(s)" note — it was
    reporting pool density, not diagnosing a root cause.

    Requiring a same-amount, cross-source counterpart makes the signal
    actually discriminating, and keeps the rule deterministic and
    auditable (no fuzzy scoring in the exception path).

    NOTE: matches on EXACT amount. A counterpart leg whose amount differs
    by gateway fees or FX will not be detected here and will classify as
    missing_entry instead. That's the conservative direction — better to
    under-claim "awaiting its leg" than to assert a counterpart exists
    when the amounts don't actually tie out.
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
