"""
What happens around one reconciliation, apart from HTTP.

The route handlers read files and parse form fields; everything a run DOES
lives here, so it can be called and tested without a web request:

  take_chargebacks      pending reversals this settlement's window can absorb
  finish_upload_run     the single-settlement run after the solve: the
                        paid-out check, what a clear teaches and records, the
                        investigator, open items, history
  run_queue             a day's settlements against one pool

The order inside finish_upload_run is load-bearing. The paid-out check runs
before anything records or learns from a clear, so a clear it withdraws
teaches nothing and closes nothing (FAILURE_LOG 40).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import audit
import chargeback_engine
import history
import fx_reference
import investigation_agent
import llm_provider
import model_budget
import open_items
import settled_ledger
import settlement_cycle
from api.presentation import (
    _check_then_record, _format_report, compliance_review, contested_payments,
    interchangeable_note, matched_rows, _settlement_instant,
)
from fee_decomposition import DEFAULT_RATE_CARD, FeeRateCard
from ingestion import normalize_amount_to_cents
from pipeline import reconcile_settlement
from plain_summary import plain_summary
from schema import NormalizedTxn, SettlementBatch, SourceType

logger = logging.getLogger(__name__)


def parse_member_source(value: str | None) -> SourceType | None:
    """The declared member feed, or None. Raises ValueError naming the options."""
    if not value:
        return None
    try:
        return SourceType(value.strip().lower())
    except ValueError:
        raise ValueError(f"member_source must be one of {[s.value for s in SourceType]}, "
                         f"got {value!r}.") from None


def take_chargebacks(batch_id: str, settled: datetime, window_days: int) -> list[NormalizedTxn]:
    """
    Pending reversals filed inside this settlement's window. Only those: one
    outside it would be dropped by the window filter and then marked taken,
    losing money owed back from every later batch (test_chargebacks.py).
    """
    window_start = settled - timedelta(days=window_days)
    waiting = chargeback_engine.pending()
    reversals = [t for t in waiting if window_start <= t.timestamp_utc <= settled]
    audit.log_decision(
        batch_id=batch_id, agent="chargeback_engine",
        detail=(f"{len(reversals)} of {len(waiting)} pending chargeback "
                f"reversal(s) fall inside this settlement's window and were "
                f"added to its pool, totalling "
                f"{sum(t.amount_cents for t in reversals)}c. The rest stay "
                f"pending. The settlements the originals cleared in are "
                f"untouched."),
    )
    return reversals


def _withhold_if_already_settled(formatted: dict, report, ledger_keys: list[str]) -> None:
    """
    A payment an earlier settlement already paid out withholds this clear.
    Any payment, not most of them (FAILURE_LOG 40). The payments stay in the
    pool and the arithmetic is untouched: the earlier record could itself be
    the wrong one.
    """
    matched_ids = formatted.get("matched_txn_ids") or []
    prior = settled_ledger.check_claims(report.batch_id, ledger_keys)
    if not prior.get("count"):
        return
    formatted["already_settled_elsewhere"] = prior
    audit.log_decision(
        batch_id=report.batch_id, agent="settled_ledger",
        detail=(f"{prior['count']} matched payment(s) were already cleared "
                f"into an earlier settlement. {prior['summary']}"),
    )
    summ = formatted.get("summary") or {}
    if summ.get("cleared"):
        summ["requires_human_approval"] = True
        summ["cleared"] = False
        summ["ambiguous"] = True
        summ["withheld_reason"] = "already_settled_elsewhere"
        formatted["cleared"] = False
        audit.log_decision(
            batch_id=report.batch_id, agent="settled_ledger",
            detail=(f"Withheld from auto-clear: {prior['count']} of "
                    f"{len(matched_ids)} matched payments were already "
                    f"paid out by an earlier settlement. The arithmetic is "
                    f"unchanged; the clear is withheld for a person."),
        )


def _investigate(formatted: dict, batch, candidates, report, use_model: bool) -> None:
    found = investigation_agent.investigate(batch, candidates, report, use_model=use_model)
    if not found:
        return
    formatted["investigation"] = found
    prop, ver = found["proposal"], found["verification"]
    audit.log_decision(
        batch_id=report.batch_id, agent="investigator",
        detail=(f"Proposed {prop['action']} ({prop['proposer']}): {prop['reason']} "
                + ("Verified in code; still a proposal for a reviewer."
                   if ver["valid"] else "REJECTED by the verifier: "
                   + "; ".join(ver["failed"]))))


def _note_earlier_runs(batch_id: str, notes: list[str]) -> None:
    """Say when this batch was reconciled before, so two verdicts are connected."""
    try:
        prior = list(history.list_runs(limit=50, batch_id=batch_id))
    except Exception as exc:  # a history read must never fail a finished run
        logger.warning("Could not read run history for %s: %s", batch_id, exc)
        return
    if prior:
        last = prior[0]
        notes.append(
            f"This batch has been reconciled {len(prior)} time"
            f"{'' if len(prior) == 1 else 's'} before — most recently "
            f"{last.get('timestamp_utc')} with the verdict "
            f"'{last.get('status')}'. This run does not replace that "
            f"record; both are in the history."
        )


def finish_upload_run(
    batch: SettlementBatch,
    candidates: list[NormalizedTxn],
    report,
    *,
    notes: list[str],
    inputs: dict,
    taken_reversals: list[str],
    investigate: bool,
    investigate_with_model: bool,
    reviewer: str | None,
    rate_card_assumed: bool,
    deductions_declared: bool,
) -> dict:
    """Everything a single-settlement run does after the solve. Returns the response body."""
    formatted = _format_report(report, batch=batch, candidates=candidates)

    # Taken only now that the batch reconciled with them in its pool.
    if taken_reversals:
        chargeback_engine.mark_taken(taken_reversals, batch.batch_id)
        formatted["chargeback_reversals_included"] = taken_reversals

    if rate_card_assumed and not deductions_declared:
        notes.append(
            "No deductions were declared and no rate card was supplied, so "
            f"the gross target was reconstructed from the DEFAULT card "
            f"({DEFAULT_RATE_CARD.gateway_fee_bps}bps + "
            f"{DEFAULT_RATE_CARD.tax_withholding_bps}bps). That is a plausible "
            "guess, not your contract. Subset-sum is exact, so if these terms "
            "are wrong the batch will be withheld rather than mismatched — "
            "supply your real terms if this batch does not clear."
        )

    matched_ids = formatted.get("matched_txn_ids") or []
    # The ledger is keyed by txn_key: two feeds' "1001" are two payments.
    ledger_keys = report.match_result.matched_keys or matched_ids
    _withhold_if_already_settled(formatted, report, ledger_keys)
    cleared = bool((formatted.get("summary") or {}).get("cleared"))

    # A clear, and only a clear, is recorded and teaches the payout cycle.
    if cleared and matched_ids:
        settled_ledger.record_settled(
            report.batch_id, ledger_keys,
            when=(formatted.get("summary") or {}).get("as_of_utc") or "")
        settlement_cycle.learn_from(batch, candidates, matched_ids)

    if investigate and not cleared:
        _investigate(formatted, batch, candidates, report,
                     use_model=investigate_with_model and llm_provider.is_configured())

    formatted["open_items"] = open_items.update_from_run(
        batch, candidates, matched_ids, cleared=cleared)
    formatted["plain_summary"] = plain_summary(
        formatted.get("summary") or {}, formatted.get("reasoning") or "",
        formatted.get("interchangeable"))
    # Which answers came from a model, and if one was skipped, why.
    formatted["ai"] = model_budget.report()

    who = (reviewer or "").strip()
    if who:
        audit.log_decision(batch_id=batch.batch_id, agent="attribution",
                           detail=f"Run requested by {who}.")
        formatted["reviewer"] = who

    # Foreign payments: which were left out for want of a rate, and a declared
    # rate far from the ECB reference. Advisory; the rate that converts is the
    # declared one (fx.py).
    notes.extend(fx_reference.notes_for(batch, candidates))
    _note_earlier_runs(batch.batch_id, notes)
    if notes:
        formatted["ingestion_notes"] = notes

    # Recording history must never fail a finished reconciliation.
    try:
        history.record_run(batch.batch_id, inputs, formatted)
    except Exception as exc:
        logger.warning("Could not record run history for %s (%s: %s). The "
                       "reconciliation result is unaffected.",
                       batch.batch_id, type(exc).__name__, exc)
    return formatted


def _queue_batch(row: dict, member_source, merchant_id: str) -> SettlementBatch:
    return SettlementBatch(
        batch_id=str(row.get("batch_id") or "").strip(),
        net_amount_cents=normalize_amount_to_cents(row["net_amount"]),
        currency=str(row.get("currency") or "INR"),
        settled_at_utc=_settlement_instant(str(row["settled_at"])),
        member_source=member_source,
        merchant=merchant_id,
        declared_deductions_cents=(
            normalize_amount_to_cents(row["declared_deductions"])
            if row.get("declared_deductions") not in (None, "") else None
        ),
    )


def run_queue(
    settlement_rows: list[dict],
    candidates: list[NormalizedTxn],
    *,
    member_source: SourceType | None,
    merchant_id: str,
    window_days: int,
    rate_card: FeeRateCard,
) -> dict:
    """
    A day's settlements against one pool, one after another. Each is contained:
    a batch that raises is reported as an error beside the ones that succeeded.
    The cycle is learned as the queue goes, so a payout with references that
    clears teaches the payouts after it that have none.
    """
    results = []
    queue_outcomes = []
    for row in settlement_rows:
        bid = str(row.get("batch_id") or "").strip()
        try:
            batch = _queue_batch(row, member_source, merchant_id)
            report = reconcile_settlement(batch, candidates, settlement_window_days=window_days,
                                          rate_card=rate_card)
            summary = report.summary()
            m = report.match_result
            matched_keys = m.matched_keys or m.matched_txn_ids
            # Before the status is read: a payment an earlier settlement paid
            # out withholds this one (FAILURE_LOG 40).
            already = _check_then_record(bid, summary, matched_keys)
            queue_outcomes.append((batch, m.matched_txn_ids, bool(summary["cleared"])))
            if summary["cleared"]:
                settlement_cycle.learn_from(batch, candidates, m.matched_txn_ids)
            note = interchangeable_note(m, candidates)
            results.append({
                "batch_id": bid,
                "status": ("cleared" if summary["cleared"]
                           else "withheld" if summary.get("ambiguous")
                           else "unmatched"),
                "summary": summary,
                "matched_txn_ids": m.matched_txn_ids,
                "matched_transactions": matched_rows(m, candidates),
                "interchangeable": note,
                "compliance_review": compliance_review(candidates, bid),
                "already_settled_elsewhere": already,
                "matched_keys": matched_keys,
                "exception_count": len(report.exceptions),
                # Plain first; the engine's own wording stays for the audit trail.
                "plain": plain_summary(summary, m.reasoning, note),
                "reasoning": m.reasoning,
            })
        except Exception as exc:  # contained per batch, by design
            logger.exception("Queue: %s failed", bid)
            results.append({"batch_id": bid or "(unnamed)", "status": "error",
                            "error": f"{type(exc).__name__}: {exc}"})

    tally = {k: sum(1 for r in results if r["status"] == k)
             for k in ("cleared", "withheld", "unmatched", "error")}
    return {
        "open_items": open_items.update_from_runs(queue_outcomes, candidates),
        "queued": len(results),
        "candidates_pooled": len(candidates),
        "tally": tally,
        "needs_review": tally["withheld"] + tally["unmatched"] + tally["error"],
        # Only answerable across a whole run: has one payment been spent twice?
        "contested_payments": contested_payments(results),
        "results": results,
    }
