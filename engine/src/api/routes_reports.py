"""
Read-only views for a regulator, an auditor, or a controller asking
what happened: escalations, the published rulebook, run history,
per-batch compliance attestation, and the natural-language Q&A.

Nothing here changes state.
"""

from __future__ import annotations

import re
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

@router.get("/escalations", summary="Compliance findings escalated for follow-up")
def list_escalations(limit: int = 200):
    """
    Where an escalation goes.

    "Escalate" is a verb that names a destination, and there was not one. The
    button wrote a line to the batch's own audit trail and stopped, so a
    reviewer who escalated a finding had no way to see it again, and nobody
    downstream had any way to find it. The word promised a handoff the system
    never performed.

    This is that list: every finding escalated across every batch, newest
    first, with what was escalated, by whom and against which payments. It
    reads the audit trail rather than a second store, so an escalation cannot
    exist in one place and not the other.

    Escalations do not auto-resolve. Clearing one is a decision, and it goes
    through the same compliance decision endpoint that raised it — an item
    that disappeared on its own would be worse than one that never moved.
    """
    entries = audit.find_entries("human_reviewer", "ESCALATED", limit=limit)

    out = []
    for e in entries:
        detail = e.get("detail") or ""
        m = re.search(r"^(\S+)\s+ESCALATED compliance finding\s+(\S+?)\.?(?:\s|$)", detail)
        note = ""
        nm = re.search(r"Note:\s*(.*?)(?:\s+This finding rests|$)", detail)
        if nm:
            note = nm.group(1).strip()
        cm = re.search(r"covering (\d+) payment", detail)
        out.append({
            "batch_id": e.get("batch_id"),
            "escalated_by": m.group(1) if m else "",
            "rule_id": m.group(2).rstrip(".") if m else "",
            "payment_count": int(cm.group(1)) if cm else 0,
            "note": note,
            "raised_at": e.get("timestamp_utc"),
            "detail": detail,
        })

    return {
        "count": len(out),
        "escalations": out,
        "summary": (
            f"{len(out)} compliance finding(s) escalated and awaiting "
            f"follow-up." if out else
            "Nothing has been escalated."
        ),
    }


@router.get("/compliance/rulebook", summary="Published compliance rulebook")
def compliance_rulebook():
    """
    The full set of controls this engine enforces: for each rule, what the
    underlying authority requires, what threshold this system actually applies,
    whether the rule is statutory / supervisory guidance / internal policy, and
    a link to the official source.

    The basis field matters: several thresholds here are this system's own risk
    appetite rather than law, and are labelled as such so nobody mistakes an
    internal ceiling for a statutory requirement.
    """
    return {
        "scope_note": compliance_rulebook_mod.scope_note(),
        "summary": compliance_rulebook_mod.rulebook_summary(),
        # SANCTIONS_HIT is the only rule here that blocks funds on a statutory
        # basis, and it is only as good as the list behind it. Publishing that
        # list's origin and age alongside the rule keeps a demo screening four
        # names from reading like one screening the UN Consolidated List.
        "sanctions_list": compliance_agent.sanctions_provenance(),
        "rules": [
            r.to_dict()
            for r in sorted(
                compliance_rulebook_mod.COMPLIANCE_RULES.values(),
                key=lambda x: (x.action != "BLOCKED", x.basis.value, x.rule_id),
            )
        ],
    }


@router.get("/calibration", summary="What confidence has been worth, and what reviewers say")
def calibration():
    import calibration_map  # pylint: disable=import-outside-toplevel
    return calibration_map.report()


@router.get("/history", summary="Previously recorded reconciliation runs")
def list_history(limit: int = 100, batch_id: str | None = None):
    """
    Every run this engine has recorded, newest first.

    record_run has written one of these on every reconciliation since it was
    built, and until now nothing could read them back — the files accumulated
    in data/history with no endpoint and no screen. A record nobody can
    retrieve is not a record.
    """
    runs = history.list_runs(limit=max(1, min(limit, 500)), batch_id=batch_id)
    return {"count": len(runs), "runs": runs}


@router.get("/history/{record}", summary="One recorded run in full")
def get_history_record(record: str):
    rec = history.run_detail(record)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"No such record: {record!r}")
    return rec


@router.get("/compliance/attestation/{batch_id}", summary="Per-batch compliance attestation")
def compliance_attestation(batch_id: str):
    """
    Everything a reviewer needs to understand one batch's compliance outcome:
    the decision trail, which rules fired, and the authority behind each.

    Derived from the recorded audit trail — this endpoint does not re-run the
    scan, so it reflects what the engine actually decided at the time rather
    than what it would decide now.
    """
    trail = audit.get_audit_trail(batch_id)

    # The batch's OWN trail is what makes this an attestation. The guard used
    # to accept the global scan trail as sufficient, so a batch that had never
    # been reconciled — including the literal string "undefined", which the UI
    # sent for a while — came back 200 with a full document attesting nothing.
    # A compliance record must not exist for a batch the engine never saw.
    if not trail:
        raise HTTPException(
            status_code=404,
            detail=f"No audit trail found for batch_id='{batch_id}'. "
                   f"Reconcile the batch before requesting its attestation.",
        )

    # GLOBAL_COMPLIANCE_SCAN is a single key that every run appends to, so it
    # is the whole history of every scan the engine has ever done — measured
    # at 26 MB on this machine, embedded in full into every per-batch
    # attestation. The document is meant to be handed to a reviewer; nobody
    # can read 26 MB, and almost none of it concerns this batch.
    #
    # What the reviewer actually needs from it is provenance: which sanctions
    # list was in force when this batch was screened. That is already carried
    # separately by sanctions_provenance(). A bounded, most recent slice is
    # kept for context, and the true total is reported so the trim is visible
    # rather than silent.
    SCAN_CONTEXT_LIMIT = 50
    full_scan = audit.get_audit_trail("GLOBAL_COMPLIANCE_SCAN")
    scan_trail = full_scan[-SCAN_CONTEXT_LIMIT:]

    compliance_events = [e for e in trail if e.get("agent") == "compliance_agent"]

    return {
        "batch_id": batch_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope_note": compliance_rulebook_mod.scope_note(),
        "rulebook_summary": compliance_rulebook_mod.rulebook_summary(),
        "sanctions_list": compliance_agent.sanctions_provenance(),
        "compliance_scan_events": scan_trail,
        "compliance_scan_events_total": len(full_scan),
        "compliance_scan_events_truncated": len(full_scan) > SCAN_CONTEXT_LIMIT,
        "batch_compliance_events": compliance_events,
        "decision_trail": trail,
        "event_count": len(trail),
    }


class SettlementQuestion(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000)


@router.post("/settlement/{batch_id}/ask", summary="Ask a question about a reconciliation")
def ask_settlement(batch_id: str, req: SettlementQuestion):
    """
    Plain-language Q&A over a completed reconciliation.

    Read-only and grounded: every figure in an answer comes from the engine's
    recorded results — the audit trail, match result, cash position and
    compliance findings. The model explains those facts; it does not compute,
    match or decide anything, and it has no write path.
    """
    return settlement_qa.answer_question(batch_id, req.question)


