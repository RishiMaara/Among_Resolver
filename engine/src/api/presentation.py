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
from plain_summary import plain_summary
import compliance_agent
import auto_disposition
import settled_ledger
import audit
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
    }
    # Kept so a question can be answered from what the engine actually
    # recorded, rather than by re-running the match at question time.
    settlement_qa.store_result(report.batch_id, formatted)
    return formatted




def _check_then_record(batch_id: str, summary: dict, matched_ids: list[str]) -> dict:
    """
    Order matters: check BEFORE recording, or a batch reports itself.

    The queue reconciles many settlements in one pass, so a payment cleared by
    the first can legitimately be seen by the second — and the second needs to
    be told. Recording happens only on a clear, for the same reason as the
    single path: a withheld batch has consumed nothing.
    """
    prior = settled_ledger.check_claims(batch_id, matched_ids)
    if summary.get("cleared") and matched_ids:
        settled_ledger.record_settled(batch_id, matched_ids)
    return prior


def contested_payments(results: list[dict]) -> dict:
    """
    Payments claimed by more than one settlement in the same run.

    This is the only check that REQUIRES the queue. A single settlement can
    never see it: the question is not "are these the right payments for this
    deposit" but "has this payment already been spent on a different one",
    and only a run that holds every settlement at once can answer it.

    It matters most exactly where the engine is otherwise least useful. When
    payments are fungible — a merchant selling one item at one price, 2,000
    apples at Rs 5 — WHICH payments compose a settlement is arbitrary and the
    engine says so. What is not arbitrary is whether the same payment has
    been counted twice, and that is the real exposure for that business.

    Measured before this existed: two weekly settlements over one pool of
    identical payments, 1,200 and 700, and 700 payments appeared in both
    matched sets with nothing in the response mentioning it. The plain-English
    text was already telling reviewers to "check these are not also being
    claimed by another settlement" while the only system able to check it
    stayed silent.

    Reported, never auto-resolved. Deciding which settlement legitimately owns
    a contested payment needs facts this engine does not have.
    """
    from collections import defaultdict
    owners: dict[str, list[str]] = defaultdict(list)
    cleared_owners: dict[str, list[str]] = defaultdict(list)
    for r in results:
        bid = r.get("batch_id") or "?"
        is_cleared = r.get("status") == "cleared"
        for tid in (r.get("matched_txn_ids") or []):
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
    What compliance found, for the person who has to review it.

    Findings were being computed and then thrown away. compliance_agent
    attaches a full ComplianceFinding to every transaction a rule hits —
    rule, severity, authority, citation, what was observed, what to do — and
    the API only ever built exceptions from the BLOCKED list. Everything
    FLAGGED, which is most of what a reviewer actually needs to look at,
    reached no screen at all.

    Measured on one realistic merchant day: 41 payments, 6 of them flagged by
    two rules — a customer who paid twice in the same second, and a reseller
    whose four sub-Rs 50,000 orders in a day match the structuring pattern.
    The engine found both. The reviewer was shown neither.

    Grouped by rule rather than by transaction, because a reviewer decides
    per pattern, not per row: "these four payments are one wholesale customer
    restocking" is a single judgement covering four transactions.
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
    Is the choice the solver made actually a choice?

    A settlement of three Rs 500 payments, drawn from ten Rs 500 payments that
    share a timestamp, a reference and a memo, is reported as ambiguous — and
    it is, arithmetically. But the advice that followed, "someone needs to
    look at the candidates and choose", is wrong here in a way worth catching:
    there is nothing to choose between. The ten are indistinguishable in every
    field a reviewer can see, so any three of them are the same answer, and
    asking a person to pick produces an arbitrary result wearing the costume
    of a decision.

    That is a different situation from two genuinely different sets of
    payments that happen to sum alike, where the choice is real and matters.
    Collapsing the two into one message tells the reviewer to do busywork in
    the first case and under-warns them in the second.

    What actually carries risk when payments are fungible is not WHICH three
    were picked — the settlement total is right either way — but whether the
    same payment is claimed again by another settlement. That is the thing to
    say, and it is what this reports.

    Returns None when the matched set is not interchangeable with anything.
    """
    wanted = set(match_result.matched_txn_ids or [])
    if not wanted or not pool:
        return None

    def signature(t):
        """
        What makes two payments substitutable FOR THIS SETTLEMENT.

        The timestamp used to be part of this, which made the check almost
        useless: real payments never share a timestamp to the second, so it
        only ever fired on genuinely duplicated rows. A merchant selling one
        item at one price — 2,000 apples at Rs 5 — got told "more than one
        set adds up, someone needs to choose" when every payment in the pool
        was interchangeable with every other and there was nothing to choose
        between. That is the exact advice this check exists to prevent.

        Dropping the timestamp is safe because the pool reaching here has
        already been filtered to the settlement window. Everything in it is
        in-window by construction, so the timestamp was not distinguishing
        candidates, only preventing them from being recognised as alike.
        """
        return (t.amount_cents, t.currency,
                t.ref_id_canonical or "", (t.memo_normalized or ""))

    from collections import Counter
    pool_sigs = Counter(signature(t) for t in pool)
    picked = [t for t in pool if t.source_txn_id in wanted]
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

    # A REPRODUCIBLE proposal for the case where the choice is arbitrary.
    #
    # When every picked payment is interchangeable, the solver returned one
    # subset out of an enormous number of equally correct ones, and which one
    # depends on solver internals. Run the same file twice and you can get a
    # different set of ids for the same settlement — which is indefensible in
    # an audit even though every set is arithmetically right.
    #
    # So propose the OLDEST N instead. FIFO is the convention accounting has
    # used for fungible units for a century: you do not identify which unit
    # left, you consume in a stated order and track the balance. It makes the
    # answer stable, explains itself, and is honest that it is a convention
    # rather than an identification.
    #
    # Note this proposes only. Nothing here clears a batch — a person accepts
    # the convention through /settlement/{id}/accept-fifo, and that acceptance
    # is what records consumption.
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
    The payments the engine picked, with enough detail to judge them.

    matched_txn_ids was already returned on both paths and no screen rendered
    it, so a reviewer was told "confirm these are the right payments" and
    shown nothing — sent back to the source file to find them by hand. That
    inverts the point of the tool. Summing a column is the easy half; naming
    WHICH payments sum, so a person can agree or disagree in seconds, is the
    half worth building.

    Ids alone are not enough to decide on. A reviewer needs the amount, the
    date and the reference to recognise a payment, so those travel with it,
    ordered by amount because that is how a person scans a list like this.
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
        for t in pool
        if t.source_txn_id in wanted
    ]
    rows.sort(key=lambda r: r["amount_cents"], reverse=True)
    return rows


def _settlement_instant(value) -> datetime:
    """
    Read a settlement timestamp without letting the SERVER's timezone decide.

    This was `dp.parse(x).astimezone(timezone.utc)`. When the string carries an
    offset that is correct, but a settlements file routinely holds a bare date
    — "2026-03-03" — and dp.parse returns a NAIVE datetime. astimezone() then
    interprets naive as the machine's local zone, so the anchor moved by the
    server's offset: on an IST host "2026-03-03" became 2026-03-02T18:30Z.

    Two things follow, and both are worse than the shift itself. The same file
    reconciles differently in Mumbai and in Virginia, which makes a result
    unreproducible. And the anchor is compared against transaction timestamps
    that ingestion normalises by a different rule — it never uses server-local,
    falling back to UTC with TzConfidence.LOW — so the settlement and its own
    members were being placed on two different clocks.

    Measured on a queue of 12 real settlements: the window kept 20 of 126
    candidates and excluded true members dated after 18:30 on the anchor day,
    so nothing could sum and every batch came back unmatched.
    """
    parsed = dp.parse(str(value))
    if parsed.tzinfo is None:
        # Same convention ingestion falls back to, so both sides of the
        # comparison are on one clock.
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _rate_card(gateway_bps, tax_bps, flat) -> FeeRateCard:
    """
    The processor's terms for THIS run.

    DEFAULT_RATE_CARD is 2% + 1%, which is a plausible guess and nobody's
    actual contract. Where deductions are not declared outright, the engine
    reconstructs the gross target from this card — and subset-sum is exact, so
    a card that is wrong by a fraction of a percent does not degrade the match,
    it eliminates it. Measured: a 1.5 basis point error takes auto-clear to 0%.

    Versioning by merchant and effective date is the real answer and is not
    built. Accepting the numbers per run is the difference between "this tool
    works for the one merchant we hardcoded" and "tell it your terms".
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


