"""
Separation of duties: whoever accepted a match cannot approve its posting.

Read from the audit trail, where every human decision is recorded: approving
a posting is refused when the same person recorded the batch decision or
accepted FIFO for it. Rejections are never blocked. Identity is the name a
reviewer types, so the rule is exactly as strong as that name (the engine has
no user model): it stops the accident
and the habit, not a determined person; with real identity the same rule
keys on a user id.
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
    # Brackets and bars are the marker's own syntax. A name carrying them
    # could close one marker and open a forged one inside the same line.
    return " ".join(re.sub(r"[\[\]|]", " ", name or "").split()).lower()


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
