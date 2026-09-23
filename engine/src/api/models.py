"""
Request models for the HTTP surface, and the one conversion that
turns a validated request into the engine's own SettlementBatch.

Kept apart from the routes so the shape of a request can be read
without reading the handler that serves it.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import dateutil.parser as dp
from fastapi import HTTPException
import fx
from pydantic import BaseModel, Field

from schema import SourceType, SettlementBatch, NormalizedTxn, TzConfidence
from ingestion import normalize_amount_to_cents
from fee_decomposition import DEFAULT_RATE_CARD, FeeRateCard
from cash_position import build_cash_position
from plain_summary import plain_summary
import compliance_agent
import auto_disposition
import settled_ledger
import audit

# _settlement_instant parses the many shapes a settled_at arrives in;
# presentation owns it because the report rendering needs it too.
from api.presentation import _settlement_instant

# ── Request/Response models ────────────────────────────────────────────────────

class TxnIn(BaseModel):
    txn_id: str
    ref_id: str
    amount: float
    currency: str = "INR"
    timestamp: str
    memo: str = ""


class SettlementBatchIn(BaseModel):
    batch_id: str
    net_amount: float
    currency: str = "INR"
    settled_at: str
    # Declared rates into `currency`, e.g. {"USD": "83.1250"} (fx.py).
    fx_rates: dict[str, str] = Field(default_factory=dict)


class ReconcileRequest(BaseModel):
    gateway_txns: list[TxnIn] = Field(default_factory=list)
    settlement_batch: SettlementBatchIn
    settlement_window_days: int = Field(default=5, ge=1, le=30)


class MultiSourceReconcileRequest(BaseModel):
    """
    Full multi-source reconciliation. Pass any combination of gateway,
    bank, and ERP transactions — all 3 source types are normalized into
    the unified schema before the pipeline runs.
    """
    gateway_txns: list[TxnIn] = Field(default_factory=list)
    bank_txns: list[TxnIn] = Field(default_factory=list)
    erp_txns: list[TxnIn] = Field(default_factory=list)
    settlement_batch: SettlementBatchIn
    settlement_window_days: int = Field(default=5, ge=1, le=30)


class JointReconcileRequest(BaseModel):
    """
    N:M reconciliation: several settlement batches solved at once against one
    shared pool, so a transaction two settlements could claim is assigned by the
    solver, not by processing order. Unlike /reconcile/queue, which is
    order-dependent. See orchestrator.reconcile_many.
    """
    gateway_txns: list[TxnIn] = Field(default_factory=list)
    bank_txns: list[TxnIn] = Field(default_factory=list)
    erp_txns: list[TxnIn] = Field(default_factory=list)
    settlement_batches: list[SettlementBatchIn] = Field(..., min_length=2)
    settlement_window_days: int = Field(default=5, ge=1, le=30)


def _build_settlement_batch(b: SettlementBatchIn) -> SettlementBatch:
    return SettlementBatch(
        batch_id=b.batch_id,
        net_amount_cents=normalize_amount_to_cents(b.net_amount),
        currency=b.currency,
        settled_at_utc=_settlement_instant(b.settled_at),
        fx_rates=fx.parse_rates(",".join(f"{k}={v}" for k, v in b.fx_rates.items())),
    )
