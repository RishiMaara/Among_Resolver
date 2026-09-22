"""
Reconciliation pipeline, the single entry point above the orchestrator:
compliance screening (blocked records leave the pool), then reconcile_batch
(linkage, fee decomposition, subset-sum, tiebreak, fuzzy, exceptions).

It replaced time-based sharding, which guessed that members cluster in time:
it missed members across chunk boundaries, stopped at the first ambiguous
chunk, and took 25-72 s where linkage takes 2.4-3.0 s on the 50K dataset
(archive/time-based-sharding). Linkage narrows 50,000 candidates to 55
there; where nothing links, the unanchored guard keeps a large solve from
clearing on a coincidence.
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
