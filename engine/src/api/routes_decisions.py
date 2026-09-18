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
import auto_disposition
import compliance_agent
import compliance_rulebook as compliance_rulebook_mod
import history
import settled_ledger
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
    The way out of a settlement nobody can identify.

    A merchant selling one item at one price produces payments that are
    identical in every recorded field. Any N of them make the settlement's
    total, so naming N specific ids is not an identification — it is an
    arbitrary pick wearing the costume of one. The engine correctly refuses to
    make that pick and call it a match, which leaves that whole class of
    merchant unable to close their books.

    Accounting solved this long before software: for fungible units you do not
    say WHICH unit left, you consume in a stated order and track the balance.
    That is what this records. The reviewer accepts the oldest-first
    convention; the payments are marked consumed so no other settlement can
    claim them; and the trail says plainly that this rests on a convention and
    not on evidence.

    Deliberately NOT an automatic clear. The engine's verdict is untouched and
    the batch stays withheld, exactly as with every other decision endpoint —
    a person accepted a convention, which is a different fact from the engine
    having identified something, and the record has to keep them apart.

    Refused unless the settlement is WHOLLY interchangeable. FIFO is only
    defensible when the choice is genuinely arbitrary; using it on a batch
    withheld for any other reason would launder a real problem into a
    convention.
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
    note = f" Note: {body.note.strip()}" if (body.note or "").strip() else ""
    entry = audit.log_decision(
        batch_id=batch_id,
        agent="human_reviewer",
        detail=(
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
    Close the loop the rest of this engine is built around.

    Every other endpoint here reads or computes. The product's whole claim is
    that the engine declines when it cannot be certain and a person decides —
    and until now there was nowhere for that decision to go. It happened in
    someone's head and evaporated. The audit trail recorded what the engine
    did and never what the human did, which makes it a log of half the
    process.

    It also left the sign-in screen making a promise the system did not keep:
    "reconciliation decisions are attributed to whoever is signed in". They
    were not attributed to anyone, because they were not recorded.

    This does NOT change the engine's verdict, and deliberately so. A cleared
    batch stays cleared and a withheld batch stays withheld; the human
    decision is a separate fact recorded ALONGSIDE the machine's, not an
    overwrite of it. An audit trail where a person can retroactively make the
    engine look right is worth nothing.

    Re-deciding is allowed and appends rather than replaces. People change
    their minds with new information, and the sequence of decisions is itself
    part of the record.
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

    covered = f" covering {len(body.txn_ids)} payment(s)" if body.txn_ids else ""
    note = f" Note: {body.note.strip()}" if (body.note or "").strip() else ""
    entry = audit.log_decision(
        batch_id=batch_id,
        agent="human_reviewer",
        detail=(f"{reviewer} {decision.upper()} this batch{covered}."
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
    The cash panel said the posting proposal was "ready for human approval",
    and there was nowhere to approve it. Same fault as the batch decision:
    the app asserted a human step and provided no way to take it, so the
    sentence was decoration.

    APPROVING STILL POSTS NOTHING. This engine does not write to a ledger and
    this endpoint does not change that — it records that a named person
    reviewed a balanced proposal and approved it for posting. The posting
    itself happens in the accounting system, by whoever has that authority.
    Recording an approval and calling it a posting would be the same class of
    lie as clearing a settlement nobody checked.
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

    ref = f" (entry {body.entry_id})" if (body.entry_id or "").strip() else ""
    note = f" Note: {body.note.strip()}" if (body.note or "").strip() else ""
    entry = audit.log_decision(
        batch_id=batch_id,
        agent="human_reviewer",
        detail=(f"{reviewer} {decision.upper()} the posting proposal{ref}."
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
    Each compliance card told a reviewer what to do next and gave them no way
    to record that they had done it.

    The verbs here are deliberately not approve/reject. A reviewer looking at
    a flagged pattern either decides it is benign — the reseller really is
    restocking — or escalates it to someone who can act. "Approving" a
    suspected structuring pattern is not a thing anyone does.

    THE STATUTORY CARVE-OUT. Clearing a review is not the same as discharging
    a legal duty, and on a statutory finding the two are dangerously easy to
    confuse. A CTR obligation is a duty to REPORT; deciding the transaction is
    legitimate does not remove it. So a cleared statutory finding records, in
    the trail itself, that the reporting obligation is unaffected. A reviewer
    who clicks a button and believes they have filed something is worse off
    than one with no button at all.
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


