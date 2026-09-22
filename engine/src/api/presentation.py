"""
Turning a ReconciliationReport into what a controller reads.

None of this is HTTP. It decides what a matched set looks like on a
screen, which payments are interchangeable with which, what the
compliance review says, and which settlements are contesting the
same payment. It lived in main.py between the endpoints, which is
why main.py was two thousand lines.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import dateutil.parser as dp
from fastapi import HTTPException
from pydantic import BaseModel, Field

from schema import SourceType, SettlementBatch, NormalizedTxn, TzConfidence
from ingestion import normalize_amount_to_cents
from fee_decomposition import DEFAULT_RATE_CARD, FeeRateCard
from cash_position import build_cash_position
from linkage import members_of
from plain_summary import plain_summary
import compliance_agent
import auto_disposition
import settled_ledger
import audit
import exception_ranking
import exception_taxonomy
import erp_sync
import settlement_qa



def _format_report(report, audit_trail=None, batch=None, candidates=None) -> dict:
    # Closing the finance-ops loop: a matched set is not what a controller
    # consumes. Where we have the batch and the candidate pool, turn the
    # reconciliation into a cash position and a balanced posting proposal —
    # the two artifacts the track statement's "run the books and the cash
    # position" actually asks for. Nothing is ever posted; proposals await
    # human approval.
    cash = None
    if batch is not None and candidates is not None:
        cash_obj = build_cash_position(
            batch,
            candidates,
            matched_txn_ids=report.match_result.matched_txn_ids,
            matched_keys=report.match_result.matched_keys,
            exceptions=report.exceptions,
            cleared=report.match_result.cleared,
        )
        
        if report.match_result.cleared:
            erp_sync.push_to_erp(cash_obj)
            
        cash = cash_obj.to_dict()
        # Agent 8 reports itself. Without this the stage runs but leaves no
        # trace, so the flow visualiser marks it "not needed" — which is a
        # lie about work that actually happened. Any agent absent from the
        # trail is invisible to the reviewer as well as to the UI.
        journal = cash.get("journal")
        audit.log_decision(
            batch_id=batch.batch_id, agent="cash_position",
            detail=(
                f"{len(cash.get('buckets', []))} bucket(s) computed. "
                + (
                    f"Journal proposal {'balanced' if journal.get('balanced') else 'REJECTED as unbalanced'}"
                    f" ({journal.get('status')})."
                    if journal else
                    "No journal proposal: the batch did not clear, so there is "
                    "nothing to post."
                )
            ),
        )

    formatted = {
        "summary": report.summary(),
        "cash_position": cash,
        "matched_txn_ids": report.match_result.matched_txn_ids,
        "matched_transactions": matched_rows(report.match_result, candidates),
        "interchangeable": interchangeable_note(report.match_result, candidates),
        "compliance_review": compliance_review(candidates, report.batch_id),
        "exceptions": [
            {
                "reason": e.reason.value,
                "candidate_txn_ids": e.candidate_txn_ids,
                "diagnosis_note": e.diagnosis_note,
                "requires_human_approval": e.requires_human_approval,
                # Present for compliance blocks: the specific rule, whether it
                # is law or internal policy, and an official source the
                # reviewer can open. Empty for ordinary reconciliation
                # exceptions.
                "findings": [
                    {
                        "rule_id": f.rule_id,
                        "title": f.title,
                        "severity": f.severity,
                        "action": f.action,
                        "basis": f.basis.value,
                        "authority": f.authority,
                        "source_name": f.source_name,
                        "citation": f.citation,
                        "reference_url": f.reference_url,
                        "rule_text": f.rule_text,
                        "threshold_applied": f.threshold_applied,
                        "why": f.why,
                        "observed": f.observed,
                        "remediation": f.remediation,
                    }
                    for f in e.findings
                ],
            }
            for e in report.exceptions
        ],
        "audit_trail": audit_trail or audit.get_audit_trail(report.batch_id),
        # Computed on every cleared match since the fee audit was written, and
        # returned by none of them until now — a check nobody could see.
        "fee_audit": fee_audit_view(report),
    }
    # Ranked by the money waiting on each one, largest first — the screen
    # shows ten, history keeps five hundred, Q&A grounds on a few, so the top
    # of this list is what actually gets reviewed. See exception_ranking.
    formatted["exceptions"], formatted["exceptions_summary"] = exception_ranking.rank(
        formatted["exceptions"], candidates, report.match_result.target_cents,
    )
    # Filed under the categories a finance team routes by, each with an owner
    # and a first step — see exception_taxonomy.
    formatted["exceptions"], by_category = exception_taxonomy.annotate(
        formatted["exceptions"], candidates)
    formatted["exceptions_summary"].update(by_category)
    # The receipt: the hash at the head of this batch's audit chain when the
    # result was produced. Whoever keeps it can later prove, through
    # GET /audit/{batch_id}/verify?receipt=..., that nothing up to that point
    # was altered or removed. A chain cannot see its own tail being cut off;
    # a head hash held somewhere else can.
    trail = formatted["audit_trail"]
    formatted["audit_head"] = next((e.get("hash") for e in reversed(trail) if e.get("hash")), None)
    # Kept so a question can be answered from what the engine actually
    # recorded, rather than by re-running the match at question time.
    settlement_qa.store_result(report.batch_id, formatted)
    return formatted




def fee_audit_view(report) -> dict | None:
    """Fee, GST, TDS and TCS findings on the matched set, largest first."""
    summary = getattr(report, "fee_audit_summary", None)
    if summary is None:
        return None
    return {
        "summary": summary,
        "findings": [
            {
                "category": f.category.value,
                "severity": f.severity.value,
                "txn_id": f.txn_id,
                "expected_cents": f.expected_cents,
                "actual_cents": f.actual_cents,
                "difference_cents": f.difference_cents,
                "payment_method": f.payment_method,
                "rule_basis": f.rule_basis,
                "citation": f.citation,
                "description": f.description,
            }
            for f in report.fee_audit_findings
        ],
    }


def _check_then_record(batch_id: str, summary: dict, matched_ids: list[str]) -> dict:
    """
    Order matters: check BEFORE recording, or a batch reports itself.

    The queue reconciles many settlements in one pass, so a payment cleared by
    the first can legitimately be seen by the second — and the second needs to
    be told. Recording happens only on a clear, for the same reason as the
    single path: a withheld batch has consumed nothing.
    """
    prior = settled_ledger.check_claims(batch_id, matched_ids)
    if prior.get("count") and summary.get("cleared"):
        # A payment an earlier settlement already paid out cannot fund this
        # one as well, so the clear is withheld for a person rather than
        # recorded beside a warning (FAILURE_LOG 40).
        summary["cleared"] = False
        summary["ambiguous"] = True
        summary["withheld_reason"] = "already_settled_elsewhere"
        summary["requires_human_approval"] = True
    if summary.get("cleared") and matched_ids:
        settled_ledger.record_settled(batch_id, matched_ids)
    return prior


def contested_payments(results: list[dict]) -> dict:
    """
    Payments claimed by more than one settlement in the same run.

    Only a run holding every settlement can see this, and it matters most where
    payments are fungible: which payments compose a settlement is arbitrary
    there, but counting one payment twice is not. Reported, never auto-resolved.
    """
    from collections import defaultdict
    owners: dict[str, list[str]] = defaultdict(list)
    cleared_owners: dict[str, list[str]] = defaultdict(list)
    for r in results:
        bid = r.get("batch_id") or "?"
        is_cleared = r.get("status") == "cleared"
        for tid in (r.get("matched_keys") or r.get("matched_txn_ids") or []):
            owners[tid].append(bid)
            if is_cleared:
                cleared_owners[tid].append(bid)

    # Two very different situations, and collapsing them makes the signal
    # useless. A payment in two CLEARED settlements has genuinely been
    # counted twice and the books are wrong. A payment shared with a
    # WITHHELD settlement is not a claim at all — that batch did not clear,
    # its set is a proposal, and overlapping a real settlement is one of the
    # reasons it should not be believed. Reporting the second as though it
    # were the first would put ten alarms on a healthy week.
    contested = {t: b for t, b in cleared_owners.items() if len(b) > 1}
    proposed_overlap = sum(
        1 for t, b in owners.items()
        if len(b) > 1 and len(cleared_owners.get(t, [])) <= 1
    )

    if not contested:
        note = "No payment was claimed by more than one CLEARED settlement."
        if proposed_overlap:
            note += (f" {proposed_overlap} payment(s) appear in an unresolved "
                     f"batch's proposed set as well as elsewhere, which is "
                     f"one of the reasons those batches were not cleared.")
        return {"count": 0, "proposed_overlap": proposed_overlap,
                "batches": [], "sample": [], "summary": note}

    pairs: dict[tuple, int] = defaultdict(int)
    for batches in contested.values():
        for i in range(len(batches)):
            for j in range(i + 1, len(batches)):
                pairs[tuple(sorted((batches[i], batches[j])))] += 1

    return {
        "count": len(contested),
        "proposed_overlap": proposed_overlap,
        "batches": [{"between": list(k), "shared_payments": v}
                    for k, v in sorted(pairs.items(), key=lambda kv: -kv[1])],
        "sample": sorted(contested)[:20],
        "summary": (
            f"{len(contested)} payment(s) were counted in more than one "
            f"CLEARED settlement. A payment can only belong to one, so at "
            f"least one of these settlements is wrong and the books do not "
            f"balance until it is resolved."
        ),
    }


def compliance_review(candidates, batch_id_for_audit: str = "") -> dict:
    """
    What compliance found, grouped by rule for the reviewer.

    FLAGGED findings used to reach no screen at all; only BLOCKED ones did.
    Grouped by rule because a reviewer decides per pattern, not per row.
    """
    from collections import defaultdict
    by_rule: dict[str, dict] = {}
    members: dict[str, list] = defaultdict(list)

    for t in candidates or []:
        for f in (getattr(t, "compliance_findings", None) or []):
            rid = getattr(f, "rule_id", "") or "UNKNOWN"
            if rid not in by_rule:
                by_rule[rid] = {
                    "rule_id": rid,
                    "title": getattr(f, "title", ""),
                    "severity": getattr(f, "severity", ""),
                    "action": getattr(f, "action", ""),
                    "basis": getattr(getattr(f, "basis", None), "value",
                                     str(getattr(f, "basis", ""))),
                    "authority": getattr(f, "authority", ""),
                    "source_name": getattr(f, "source_name", ""),
                    "citation": getattr(f, "citation", ""),
                    "reference_url": getattr(f, "reference_url", ""),
                    "rule_text": getattr(f, "rule_text", ""),
                    "threshold_applied": getattr(f, "threshold_applied", ""),
                    "why": getattr(f, "why", ""),
                    "observed": getattr(f, "observed", ""),
                    "remediation": getattr(f, "remediation", ""),
                }
            members[rid].append({
                "txn_id": t.source_txn_id,
                "amount_cents": t.amount_cents,
                "currency": t.currency,
                "timestamp_utc": t.timestamp_utc.isoformat(),
                "payer_id": getattr(t, "payer_id", ""),
                "memo": (getattr(t, "memo_raw", "") or "")[:80],
            })

    out = []
    for rid, rule in by_rule.items():
        rows = sorted(members[rid], key=lambda r: r["amount_cents"], reverse=True)
        rule["transactions"] = rows
        rule["transaction_count"] = len(rows)
        rule["total_amount_cents"] = sum(r["amount_cents"] for r in rows)
        out.append(rule)
    # Most serious first; a reviewer works down from the top.
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    out.sort(key=lambda r: (r["action"] != "BLOCKED",
                            order.get(r["severity"], 9),
                            -r["total_amount_cents"]))

    # Triage before returning. A controller with two hundred settlements does
    # not want two hundred decisions, and most findings are the same finding.
    # What the engine can defensibly close, it closes; what it cannot, it
    # hands over with the reason it is a person's call.
    triaged = auto_disposition.triage(out)

    # An auto-closure is recorded under its OWN agent name, never as a human
    # decision. A trail that implied a person reviewed something nobody
    # reviewed would undo the attribution work this sits next to.
    for f in triaged["findings"]:
        d = f.get("auto_disposition") or {}
        if d.get("disposition") != "auto":
            continue
        try:
            audit.log_decision(
                batch_id=batch_id_for_audit,
                agent="auto_disposition",
                detail=(f"Closed compliance finding {f.get('rule_id')} without "
                        f"human review. {d.get('reason', '')} "
                        f"{d.get('residual', '')}".strip()),
            )
        except Exception:
            # Never let a bookkeeping write fail a completed reconciliation.
            pass
    return triaged


def interchangeable_note(match_result, pool) -> dict | None:
    """
    Is the solver's choice actually a choice?

    When the picked payments are indistinguishable in every field a reviewer can
    see, any N of them are the same answer; asking a person to pick is busywork.
    The real risk there is the same payment being claimed by another settlement,
    and that is what this reports. None when the set is not interchangeable.
    """
    wanted = set(match_result.matched_txn_ids or [])
    if not wanted or not pool:
        return None

    def signature(t):
        """
        What makes two payments substitutable for THIS settlement. No timestamp:
        the pool is already inside the settlement window, and real payments never
        share a timestamp to the second.
        """
        return (t.amount_cents, t.currency,
                t.ref_id_canonical or "", (t.memo_normalized or ""))

    from collections import Counter
    pool_sigs = Counter(signature(t) for t in pool)
    picked = members_of(match_result, pool)
    picked_sigs = Counter(signature(t) for t in picked)

    groups = []
    for sig, n_picked in picked_sigs.items():
        available = pool_sigs.get(sig, 0)
        if available > n_picked:
            groups.append({
                "amount_cents": sig[0],
                "currency": sig[1],
                "picked": n_picked,
                "identical_available": available,
                "txn_ids": sorted(
                    t.source_txn_id for t in pool if signature(t) == sig
                )[:20],
            })
    if not groups:
        return None

    wholly = sum(g["picked"] for g in groups) == len(picked)

    # A reproducible proposal when the choice is arbitrary: the OLDEST N (FIFO, the
    # accounting convention for fungible units), so the same file always gives the
    # same ids. It only proposes; a person accepts it via
    # /settlement/{id}/accept-fifo, and that acceptance records consumption.
    fifo = None
    if wholly:
        sigs = {(g["amount_cents"], g["currency"]) for g in groups}
        eligible = [
            t for t in pool
            if (t.amount_cents, t.currency) in sigs
            and signature(t) in picked_sigs
        ]
        eligible.sort(key=lambda t: (t.timestamp_utc, t.source_txn_id))
        chosen = eligible[: len(picked)]
        if len(chosen) == len(picked):
            fifo = {
                "basis": "oldest_first",
                "count": len(chosen),
                "txn_ids": [t.source_txn_id for t in chosen],
                "earliest": str(chosen[0].timestamp_utc),
                "latest": str(chosen[-1].timestamp_utc),
                "pool_size": len(eligible),
            }

    return {
        "groups": groups,
        # True when EVERY picked payment came from an interchangeable group —
        # the whole selection is arbitrary, not just part of it.
        "wholly_interchangeable": wholly,
        "fifo_proposal": fifo,
    }


def matched_rows(match_result, pool) -> list[dict]:
    """
    The payments the engine picked, with amount, date and reference, largest
    first, so a reviewer can agree or disagree without opening the source file.
    """
    wanted = set(match_result.matched_txn_ids or [])
    # `candidates` is optional on _format_report's signature even though every
    # call site passes it. The reconciliation is already done by the time this
    # runs, and the caller is entitled to the result — a presentation helper
    # must not be able to turn a finished answer into a 500.
    if not wanted or not pool:
        return []
    rows = [
        {
            "txn_id": t.source_txn_id,
            "source": t.source.value,
            "amount_cents": t.amount_cents,
            "currency": t.currency,
            "timestamp_utc": t.timestamp_utc.isoformat(),
            "reference": t.ref_id_canonical or "",
            "memo": (t.memo_raw or "")[:80],
        }
        for t in members_of(match_result, pool)
    ]
    rows.sort(key=lambda r: r["amount_cents"], reverse=True)
    return rows


def _settlement_instant(value) -> datetime:
    """
    Read a settlement timestamp without letting the server's timezone decide.

    A bare date parsed naive and was then read in the server's local zone, so
    the same file reconciled differently in Mumbai and Virginia and true members
    fell outside the window. A bare date is never read in the server's zone.
    """
    parsed = dp.parse(str(value))
    if parsed.tzinfo is None:
        # Same convention ingestion falls back to, so both sides of the
        # comparison are on one clock.
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _rate_card(gateway_bps, tax_bps, flat) -> FeeRateCard:
    """
    The processor's terms for this run. The default card (2% + 1%) is nobody's
    contract, and subset-sum is exact: a 1.5 bp error takes auto-clear to 0%.
    Versioned per-merchant cards are not built; per-run terms are.
    """
    if gateway_bps is None and tax_bps is None and flat is None:
        return DEFAULT_RATE_CARD
    return FeeRateCard(
        gateway_fee_bps=gateway_bps if gateway_bps is not None
        else DEFAULT_RATE_CARD.gateway_fee_bps,
        tax_withholding_bps=tax_bps if tax_bps is not None
        else DEFAULT_RATE_CARD.tax_withholding_bps,
        flat_fee_cents=flat if flat is not None
        else DEFAULT_RATE_CARD.flat_fee_cents,
    )


def _rate_card_is_assumed(gateway_bps, tax_bps, flat) -> bool:
    """
    True when no term was supplied and the guessed card is in use.

    A wrong card cannot cause a FALSE clear — subset-sum is exact, so it
    eliminates the match rather than faking one — but that is precisely why
    it needs saying. The operator sees "withheld", concludes the engine could
    not find their settlement, and never learns the cause was a fee guess
    they were never told about.
    """
    return gateway_bps is None and tax_bps is None and flat is None


