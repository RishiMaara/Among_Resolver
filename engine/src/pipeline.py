"""
Reconciliation pipeline — the single entry point above the orchestrator.

Runs, in order:
  Agent 7  Compliance screening       (blocked records leave the pool)
  Agent 2b Linkage                    (which records belong to this batch)
  Agents 2-5 via reconcile_batch      (fee decomposition, subset-sum,
                                       tiebreak, fuzzy fallback, exceptions)

WHAT REPLACED WHAT, AND WHY
---------------------------
This module replaces the time-based sharding agent. That agent split a large
pool into 48-hour chunks, ranked them by proximity to the settlement date,
and reconciled each in turn.

Chunking by time is what you do when you have no idea which records belong
to a settlement: it makes an intractable search tractable by *guessing* that
members cluster in time. Linkage removes the need for the guess — records
that name the settlement identify themselves — and the guess was not free:

  * Members can straddle a chunk boundary or land in a chunk the ranking
    never reaches. On the 50K dataset the members span the first 48 hours
    while noise starts two days earlier, so the top-ranked chunk held almost
    none of them and the run returned 71 unrelated ERP journal records that
    happened to sum to the target.

  * It halted on the first ambiguous chunk, ending the search before the
    chunk holding the real members was ever examined.

  * It was slow. Measured on the same dataset: 25-72s chunking, versus 5.3s
    once linkage identifies the 55 members directly out of 50,000.

  * Its ranking heuristic — chunk end-time nearest the settlement date — had
    no evidence behind it.

The old implementation is preserved on the `archive/time-based-sharding`
branch rather than deleted outright, because the fallback question it was
trying to answer is real: what do you do when NOTHING names the settlement?
The answer this pipeline gives is "decline and route to a human", which is
correct but unsatisfying, and a future non-arithmetic signal (timestamp
precedence between a payment and its ledger entry, amount+window pairing
across feeds) may do better than either approach.

SCALE
-----
No chunking is needed because linkage narrows before the solver ever runs —
50,000 candidates to 55 on the reference dataset. Where linkage finds
nothing, the pool is passed through whole and the orchestrator's unanchored
guard prevents a large unconstrained solve from auto-clearing on a
coincidence.
"""

from __future__ import annotations

import logging
from typing import List

from schema import (
    NormalizedTxn,
    SettlementBatch,
    ExceptionRecord,
    ExceptionReason,
)
from orchestrator import reconcile_batch
from subset_sum import SubsetSumConfig
from fee_decomposition import FeeRateCard, DEFAULT_RATE_CARD
import audit
import compliance_agent

logger = logging.getLogger(__name__)


def _compliance_exceptions(batch: SettlementBatch, blocked: List[NormalizedTxn]):
    """
    One exception PER blocked transaction, carrying the rule findings that
    stopped it. Collapsing them into a single "N transactions blocked" record
    tells a reviewer nothing: not which rule fired, not what value tripped
    it, not whether the rule is statutory or this firm's own policy, and not
    what to do next. A compliance stop that cannot be explained is not
    auditable.
    """
    out: list[ExceptionRecord] = []
    for t in blocked:
        blocking = [f for f in t.compliance_findings if f.action == "BLOCKED"]

        if blocking:
            note = (
                f"{t.source_txn_id}: "
                + "; ".join(f"{f.title} — {f.observed}" for f in blocking)
            )
        else:
            # A transaction marked BLOCKED with no registered rule means a
            # rule fired that is not in COMPLIANCE_RULES. Say so plainly
            # rather than inventing a justification for it.
            note = (
                f"Transaction {t.source_txn_id} was blocked, but no registered "
                f"rule definition was recorded for it. Treat as unexplained "
                f"and escalate — do not release on this record alone."
            )

        out.append(ExceptionRecord(
            batch_id=batch.batch_id,
            candidate_txn_ids=[t.source_txn_id],
            reason=ExceptionReason.COMPLIANCE_BLOCK,
            diagnosis_note=note,
            requires_human_approval=True,
            findings=blocking,
        ))
    return out


def reconcile_settlement(
    batch: SettlementBatch,
    candidates: List[NormalizedTxn],
    settlement_window_days: int = 5,
    rate_card: FeeRateCard = DEFAULT_RATE_CARD,
    subset_config: SubsetSumConfig | None = None,
):
    """
    Reconcile one settlement batch against a candidate pool of any size.

    Compliance runs first and its blocks are removed from the pool before
    matching — a transaction the firm may not touch must not be able to
    become part of a cleared settlement.
    """
    audit.log_decision(
        batch_id=batch.batch_id, agent="compliance_agent",
        detail=(
            f"Screening {len(candidates)} record(s) against "
            f"{len(compliance_agent.COMPLIANCE_RULES)} published rules."
        ),
    )
    safe, blocked = compliance_agent.scan(candidates)
    audit.log_decision(
        batch_id=batch.batch_id, agent="compliance_agent",
        detail=(
            f"{len(blocked)} transaction(s) blocked, {len(safe)} remain in the "
            f"pool. Sanctions list: {compliance_agent.SANCTIONS_LIST_SOURCE}."
        ),
    )
    blocked_exceptions = _compliance_exceptions(batch, blocked)

    if blocked:
        logger.info(
            f"Compliance blocked {len(blocked)} transaction(s); "
            f"{len(safe)} remain in the pool."
        )

    if not safe:
        report = reconcile_batch(batch, [], settlement_window_days=settlement_window_days,
                                 rate_card=rate_card, subset_config=subset_config)
        report.exceptions.extend(blocked_exceptions)
        return report

    # Linkage is NOT run here. reconcile_batch runs it itself, over the
    # window-filtered pool, and uses the result. Calling it here as well
    # tokenised every reference in the pool a second time and threw the answer
    # away — on 50,000 candidates that is the single most expensive stage in
    # the run, paid twice for one settlement.
    report = reconcile_batch(
        batch, safe, settlement_window_days=settlement_window_days,
        rate_card=rate_card, subset_config=subset_config,
    )
    report.exceptions.extend(blocked_exceptions)
    return report


# Backwards-compatible alias. The old name described the strategy (sharding)
# rather than the job, which is part of why the strategy went unquestioned
# for so long.
shard_and_reconcile = reconcile_settlement
