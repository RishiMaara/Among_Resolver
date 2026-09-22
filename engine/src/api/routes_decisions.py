"""
What a human decided, and the record that they decided it.

Every endpoint here writes to the audit trail, because a decision
nobody can reconstruct later is the thing an auditor asks about
first. Accepting a FIFO split, approving a journal, clearing a
compliance finding: each is a person overriding or confirming the
engine, and each is recorded as such.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from schema import SourceType
import audit
import four_eyes
import auto_disposition
import compliance_agent
import compliance_rulebook as compliance_rulebook_mod
import history
import settled_ledger
import open_items
import calibration_map
import settlement_qa
from api.presentation import _check_then_record

router = APIRouter()

class AcceptFifo(BaseModel):
    """A reviewer accepting the FIFO convention on a fungible settlement."""
    reviewer: str                      # who is accountable for it
    note: str = ""                     # why, in their own words


@router.post("/settlement/{batch_id}/accept-fifo",
          summary="Accept the oldest-first convention on a fungible settlement")
def accept_fifo(batch_id: str, body: AcceptFifo):
    """
    Accept oldest-first (FIFO) for a settlement nobody can identify.

    When payments are identical in every field, naming N of them is not an
    identification, so the engine refuses to. The reviewer can accept the FIFO
    convention instead: payments are marked consumed and the trail says this
    rests on a convention, not evidence. The engine's verdict is unchanged, and
    it is refused unless the settlement is wholly interchangeable.
    """
    reviewer = (body.reviewer or "").strip()
    if not reviewer:
        raise HTTPException(status_code=422, detail={
            "message": "reviewer is required",
            "plain": ("Accepting a convention has to be attributed to someone. "
                      "Sign in first."),
        })

    report = settlement_qa.get_result(batch_id)
    if not report:
        raise HTTPException(status_code=404, detail={
            "message": "no recorded run for this batch",
            "plain": ("There is no recorded run for this settlement in this "
                      "session, so there is nothing to accept. Reconcile it "
                      "again and accept from the result."),
        })

    inter = report.get("interchangeable") or {}
    proposal = inter.get("fifo_proposal")
    if not inter.get("wholly_interchangeable") or not proposal:
        raise HTTPException(status_code=422, detail={
            "message": "settlement is not wholly interchangeable",
            "plain": ("This settlement was not withheld because its payments "
                      "are indistinguishable, so the oldest-first convention "
                      "does not apply. Whatever is unresolved here is a real "
                      "question about which payments belong, and needs an "
                      "answer rather than a convention."),
        })

    txn_ids = list(proposal.get("txn_ids") or [])
    claims = settled_ledger.check_claims(batch_id, txn_ids)
    if claims.get("count"):
        raise HTTPException(status_code=409, detail={
            "message": "payments already claimed elsewhere",
            "plain": ("Some of these payments are already recorded against "
                      f"another settlement. {claims.get('summary', '')}"),
        })

    written = settled_ledger.record_settled(batch_id, txn_ids)
    open_items.close(batch_id, txn_ids)
    note = f" Note: {body.note.strip()}" if (body.note or "").strip() else ""
    entry = audit.log_decision(
        batch_id=batch_id,
        agent="human_reviewer",
        detail=(
            four_eyes.marker(reviewer, four_eyes.ACT_FIFO_ACCEPTANCE).strip() + " "
            f"{reviewer} ACCEPTED the oldest-first convention covering "
            f"{len(txn_ids)} payment(s), drawn from {proposal.get('pool_size')} "
            f"indistinguishable ones between {proposal.get('earliest')} and "
            f"{proposal.get('latest')}.{note} These payments are now recorded "
            f"as consumed so no other settlement can claim them. This rests on "
            f"a stated convention, NOT on evidence that these specific payments "
            f"are the members — any set of the same size would have been "
            f"equally correct. The engine's own verdict is unchanged."
        ),
    )
    return {
        "batch_id": batch_id,
        "accepted": True,
        "basis": proposal.get("basis"),
        "reviewer": reviewer,
        "consumed_count": written,
        "txn_ids": txn_ids,
        "clears_the_batch": False,
        "audit_entry": entry,
    }


class ReviewDecision(BaseModel):
    """A person's verdict on a batch the engine would not decide alone."""
    decision: str                      # confirmed | rejected
    reviewer: str                      # who is accountable for it
    note: str = ""                     # why, in their own words
    txn_ids: list[str] = []            # the payments the decision covers


@router.post("/settlement/{batch_id}/decision",
          summary="Record a reviewer's decision on a batch")
def record_decision(batch_id: str, body: ReviewDecision):
    """
    Record a reviewer's decision on a batch.

    The decision is recorded ALONGSIDE the engine's verdict, never over it: a
    withheld batch stays withheld. Re-deciding appends, so the sequence of
    decisions is part of the record.
    """
    decision = (body.decision or "").strip().lower()
    if decision not in ("confirmed", "rejected"):
        raise HTTPException(status_code=422, detail={
            "message": "decision must be 'confirmed' or 'rejected'",
            "plain": ("A decision has to be either 'confirmed' or 'rejected'. "
                      f"Received {body.decision!r}."),
        })
    reviewer = (body.reviewer or "").strip()
    if not reviewer:
        # An unattributed decision is the thing this endpoint exists to stop.
        raise HTTPException(status_code=422, detail={
            "message": "reviewer is required",
            "plain": ("A decision has to be attributed to someone. Sign in "
                      "before confirming or rejecting a batch."),
        })

    # A person's verdict on a proposed set is an outcome the calibration can
    # be checked against (calibration_map.report, GET /calibration).
    from api.routes_exports import latest_result  # pylint: disable=import-outside-toplevel
    recorded = latest_result(batch_id) or {}
    summary = recorded.get("summary") or {}
    if summary.get("matched_count"):
        calibration_map.record_outcome(batch_id, float(summary.get("confidence") or 0.0),
                                       decision == "confirmed", reviewer)

    covered = f" covering {len(body.txn_ids)} payment(s)" if body.txn_ids else ""
    note = f" Note: {body.note.strip()}" if (body.note or "").strip() else ""
    entry = audit.log_decision(
        batch_id=batch_id,
        agent="human_reviewer",
        detail=(four_eyes.marker(reviewer, four_eyes.ACT_BATCH_DECISION).strip() + " "
                f"{reviewer} {decision.upper()} this batch{covered}."
                f"{note} This is the reviewer's decision and does not alter "
                f"the engine's own verdict."),
    )
    return {
        "batch_id": batch_id,
        "decision": decision,
        "reviewer": reviewer,
        "note": body.note.strip(),
        "txn_ids": body.txn_ids,
        "recorded_at": entry.get("timestamp") or entry.get("ts"),
        "audit_entry": entry,
    }


class JournalDecision(BaseModel):
    """A person's verdict on the posting proposal."""
    decision: str                      # approved | rejected
    reviewer: str
    entry_id: str = ""
    note: str = ""


@router.post("/settlement/{batch_id}/journal/decision",
          summary="Approve or reject the posting proposal")
def record_journal_decision(batch_id: str, body: JournalDecision):
    """
    Approve (or reject) a balanced posting proposal. Approving still posts
    nothing: it records that a named person approved it; the posting happens in
    the accounting system.
    """
    decision = (body.decision or "").strip().lower()
    if decision not in ("approved", "rejected"):
        raise HTTPException(status_code=422, detail={
            "message": "decision must be 'approved' or 'rejected'",
            "plain": ("A posting proposal can be 'approved' or 'rejected'. "
                      f"Received {body.decision!r}."),
        })
    reviewer = (body.reviewer or "").strip()
    if not reviewer:
        raise HTTPException(status_code=422, detail={
            "message": "reviewer is required",
            "plain": ("An approval has to be attributed to someone. Sign in "
                      "before approving a posting proposal."),
        })

    # Separation of duties. Only approvals are gated: refusing your own
    # proposal needs no second pair of eyes, and blocking that would just
    # teach people to route rejections through someone else.
    if decision == "approved":
        clash = four_eyes.conflict(batch_id, reviewer)
        if clash:
            audit.log_decision(
                batch_id=batch_id, agent="human_reviewer",
                detail=(f"REFUSED {reviewer}'s posting approval: separation of "
                        f"duties — the same person accepted the match."),
            )
            raise HTTPException(status_code=409, detail={
                "message": "separation of duties: approver accepted the match",
                "plain": clash,
            })

    ref = f" (entry {body.entry_id})" if (body.entry_id or "").strip() else ""
    note = f" Note: {body.note.strip()}" if (body.note or "").strip() else ""
    entry = audit.log_decision(
        batch_id=batch_id,
        agent="human_reviewer",
        detail=(four_eyes.marker(reviewer, four_eyes.ACT_JOURNAL_APPROVAL).strip() + " "
                f"{reviewer} {decision.upper()} the posting proposal{ref}."
                f"{note} Nothing has been posted to any ledger — this records "
                f"the approval, not the posting."),
    )
    return {
        "batch_id": batch_id, "decision": decision, "reviewer": reviewer,
        "entry_id": body.entry_id, "note": body.note.strip(),
        "posted": False,
        "audit_entry": entry,
    }


class ComplianceDecision(BaseModel):
    """A reviewer's verdict on one compliance finding."""
    rule_id: str
    decision: str                      # cleared | escalated
    reviewer: str
    basis: str = ""                    # statutory | regulatory_guidance | internal_policy
    note: str = ""
    txn_ids: list[str] = []


@router.post("/settlement/{batch_id}/compliance/decision",
          summary="Record a reviewer's decision on a compliance finding")
def record_compliance_decision(batch_id: str, body: ComplianceDecision):
    """
    Record what a reviewer did with a compliance finding: cleared as benign, or
    escalated. Clearing a statutory finding does not discharge a reporting duty,
    and the trail says so.
    """
    decision = (body.decision or "").strip().lower()
    if decision not in ("cleared", "escalated"):
        raise HTTPException(status_code=422, detail={
            "message": "decision must be 'cleared' or 'escalated'",
            "plain": ("A compliance finding is either 'cleared' (reviewed, no "
                      f"concern) or 'escalated'. Received {body.decision!r}."),
        })
    reviewer = (body.reviewer or "").strip()
    if not reviewer:
        raise HTTPException(status_code=422, detail={
            "message": "reviewer is required",
            "plain": ("A compliance decision has to be attributed to someone. "
                      "Sign in before clearing or escalating a finding."),
        })
    rule_id = (body.rule_id or "").strip() or "UNSPECIFIED_RULE"

    covered = f" covering {len(body.txn_ids)} payment(s)" if body.txn_ids else ""
    note = f" Note: {body.note.strip()}" if (body.note or "").strip() else ""
    caveat = ""
    if decision == "cleared" and body.basis.strip().lower() == "statutory":
        caveat = (" This finding rests on a STATUTORY obligation: clearing the "
                  "review records a judgement about the transaction and does "
                  "NOT discharge any reporting duty.")

    entry = audit.log_decision(
        batch_id=batch_id,
        agent="human_reviewer",
        detail=(f"{reviewer} {decision.upper()} compliance finding "
                f"{rule_id}{covered}.{note}{caveat}"),
    )
    return {
        "batch_id": batch_id, "rule_id": rule_id, "decision": decision,
        "reviewer": reviewer, "note": body.note.strip(),
        "txn_ids": body.txn_ids,
        "discharges_reporting_obligation": False,
        "audit_entry": entry,
    }


@router.get("/settlement/{batch_id}/decisions",
         summary="Reviewer decisions recorded against a batch")
def list_decisions(batch_id: str):
    """Every human decision on this batch, oldest first."""
    trail = audit.get_audit_trail(batch_id) or []
    out = [e for e in trail if e.get("agent") == "human_reviewer"]
    return {"batch_id": batch_id, "count": len(out), "decisions": out}


