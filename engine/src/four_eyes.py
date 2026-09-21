"""
Separation of duties: the person who accepted a match cannot approve its posting.

WHY THIS AND NOT A ROLE SYSTEM
------------------------------
The first attempt at this shipped as an RBAC module with MAKER/CHECKER roles
and a module-level `_current_user` global. Two things were wrong with it, and
both are the kind that look fine until the thing runs:

  * a global "current user" in a web server is a race. Two approvals arriving
    at once read whichever identity was set last, so an approval can be
    recorded against someone who did not make it. In a control whose entire
    job is attributing a decision to a person, that is the one bug that
    matters.
  * it was never wired to anything. The endpoints a reviewer actually uses —
    accept-fifo, the batch decision, the journal approval — did not consult
    it, so the "SOX 4-eyes enforcement" was a class nobody called.

What is enforced instead uses what the engine already has: every human
decision is written to the audit trail, and the trail is durable and shared
across instances. The rule reads that trail.

THE RULE
--------
Approving a posting proposal is refused when the same person already accepted
the match it rests on — either by recording the batch decision or by
accepting the oldest-first convention. Those are the two acts that say "this
set of payments is right"; approving the posting says "and it may be booked".
One person doing both is the self-approval that separation of duties exists
to prevent.

Rejections are never blocked. Refusing your own proposal needs no second
pair of eyes, and blocking it would only teach people to route rejections
through someone else.

WHAT THIS IS NOT
----------------
Identity here is the name a reviewer types, so it is exactly as strong as
that name — this engine has no session, and `API_KEY` authenticates a caller
rather than a person. Someone who wants to approve their own work can type a
different name. That is worth stating plainly rather than describing this as
SOX compliance: it stops the accident and the habit, not a determined person.
With real user identity the same rule keys on a user id instead, and nothing
else about it changes.
"""

from __future__ import annotations

import re

import audit

# Actions written into the audit detail, so an actor can be read back exactly
# rather than parsed out of a sentence. The prose stays for humans; this is
# for the check.
ACT_BATCH_DECISION = "batch_decision"
ACT_FIFO_ACCEPTANCE = "fifo_acceptance"
ACT_JOURNAL_APPROVAL = "journal_approval"

# Acts that say "this match is right". Any of them disqualifies the same
# person from approving the posting that follows.
MAKER_ACTS = (ACT_BATCH_DECISION, ACT_FIFO_ACCEPTANCE)

_MARKER_RE = re.compile(r"\[sod actor=(?P<actor>[^|\]]*)\|act=(?P<act>[a-z_]+)\]")


def marker(actor: str, act: str) -> str:
    """The machine-readable tail appended to a human decision's audit line."""
    return f" [sod actor={_normalise(actor)}|act={act}]"


def _normalise(name: str) -> str:
    # Case and surrounding space are not identity. "Rishi " and "rishi" are
    # one person typing the same name twice.
    return " ".join((name or "").split()).lower()


def actors(batch_id: str, acts: tuple[str, ...]) -> set[str]:
    """Who already performed any of `acts` on this batch, by normalised name."""
    found: set[str] = set()
    for entry in audit.get_audit_trail(batch_id) or []:
        for m in _MARKER_RE.finditer(str(entry.get("detail") or "")):
            if m.group("act") in acts and m.group("actor"):
                found.add(m.group("actor"))
    return found


def conflict(batch_id: str, approver: str) -> str | None:
    """
    The reason this person may not approve this posting, or None.

    Returns the message a reviewer should read, not a boolean, because the
    refusal is only useful if it says which earlier act it collides with.
    """
    who = _normalise(approver)
    if not who:
        return None
    if who in actors(batch_id, MAKER_ACTS):
        return (
            f"{approver.strip()} already accepted the match on this settlement, "
            f"so the same person cannot approve the posting that rests on it. "
            f"Separation of duties: one person confirms the payments are right, "
            f"a different person approves booking them. Ask a second reviewer "
            f"to approve, or record the approval under the name of whoever "
            f"actually made it."
        )
    return None
