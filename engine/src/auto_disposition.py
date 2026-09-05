"""
Decide what a person actually needs to look at.

WHY
---
A controller with two hundred settlements does not want two hundred
decisions. Most findings are the same finding: a customer double-clicked
checkout, a regular wholesale account placed its usual orders. Asking a human
to click through those is not oversight, it is data entry, and a reviewer who
clears forty identical items in a row stops reading the forty-first.

So the engine dispositions what it defensibly can and reports the rest.

WHERE THE LINE IS, AND WHY IT IS THERE
--------------------------------------
Auto-clearing is allowed only where the rule is this firm's own, the severity
is low, and the pattern is a data artifact rather than a judgement. Three
categories are never auto-cleared, and each for a different reason:

  STATUTORY findings. Clearing one asserts that a reporting obligation did
  not apply. That is a legal determination and this engine has no standing to
  make it — the whole reason the rulebook separates statutory from internal is
  that the two carry different authority.

  ANYTHING BLOCKED, or HIGH severity. The severity is the engine's own
  estimate that a person should look. Auto-clearing on the strength of that
  same estimate is the system marking its own homework.

  REGULATORY GUIDANCE where the pattern is genuinely ambiguous. A structuring
  pattern and a wholesale customer's ordinary restocking are indistinguishable
  from the arithmetic — that is what the rule's own text says — so the
  decision is a judgement, and judgements go to people.

AN AUTO-DISPOSITION IS NOT A HUMAN DECISION, and is never recorded as one.
It is written to the audit trail under its own agent name so the trail never
implies a person looked at something nobody looked at. That distinction is
the entire value of the attribution work it sits next to.
"""

from __future__ import annotations

# Rules whose findings may be closed without a person, when the other
# conditions below also hold. Deliberately short.
AUTO_CLEARABLE = {
    "DUPLICATE_TX": (
        "Two records identical in amount, timestamp, payer and payee. That is "
        "the signature of the same payment ingested twice, not of financial "
        "crime — the rule's own text says so. Closed as a data-quality "
        "observation."
    ),
}

# What an auto-clear does NOT settle, stated on the record so nobody reads
# more into it than it says.
RESIDUAL_NOTE = {
    "DUPLICATE_TX": (
        "This closes the COMPLIANCE question only. If the customer was charged "
        "twice it is still a billing issue and needs a refund check."
    ),
}


def disposition(finding: dict) -> dict:
    """
    Decide one finding. Returns the disposition and the reasoning for it.

    `auto` means the engine closed it. `human` means a person is required,
    and `reason` says what makes it their call rather than the engine's —
    which is the part a reviewer actually needs, because it tells them what
    question they are being asked.
    """
    rule_id = (finding.get("rule_id") or "").strip()
    basis = (finding.get("basis") or "").strip().lower()
    severity = (finding.get("severity") or "").strip().upper()
    action = (finding.get("action") or "").strip().upper()

    if action == "BLOCKED":
        return {"disposition": "human", "reason": (
            "Blocked transactions are excluded from matching and released only "
            "by a person. The engine does not unblock its own blocks.")}

    if basis == "statutory":
        return {"disposition": "human", "reason": (
            "Statutory. Clearing this would assert that a reporting obligation "
            "does not apply, which is a legal determination and not the "
            "engine's to make.")}

    if severity == "HIGH":
        return {"disposition": "human", "reason": (
            "High severity is the engine's own estimate that a person should "
            "look. Closing it on the strength of that same estimate would be "
            "the system marking its own homework.")}

    if basis == "internal_policy" and severity == "LOW" and rule_id in AUTO_CLEARABLE:
        return {
            "disposition": "auto",
            "reason": AUTO_CLEARABLE[rule_id],
            "residual": RESIDUAL_NOTE.get(rule_id, ""),
        }

    if basis == "regulatory_guidance":
        return {"disposition": "human", "reason": (
            "Supervisory guidance, and the pattern is ambiguous by nature — "
            "the rule's own text says the benign and the suspicious case look "
            "identical in the arithmetic. That makes it a judgement.")}

    return {"disposition": "human", "reason": (
        "No auto-clear rule covers this finding, so it goes to a person by "
        "default. Silence is not consent.")}


def triage(findings: list[dict]) -> dict:
    """
    Disposition a batch's findings and report what is left for a human.

    The summary is the point: a reviewer opening this should learn in one
    line how much of it is theirs.
    """
    out = []
    for f in findings or []:
        d = disposition(f)
        out.append({**f, "auto_disposition": d})
    auto = [f for f in out if f["auto_disposition"]["disposition"] == "auto"]
    human = [f for f in out if f["auto_disposition"]["disposition"] == "human"]
    return {
        "findings": out,
        "auto_closed": len(auto),
        "needs_human": len(human),
        "summary": _summarise(len(auto), len(human)),
    }


def _summarise(auto: int, human: int) -> str:
    total = auto + human
    if total == 0:
        return "Nothing was flagged."
    if human == 0:
        return (f"All {total} finding(s) closed automatically — none needed a "
                f"person. Each one is recorded with its reason.")
    verb = "needs" if human == 1 else "need"
    if auto == 0:
        return (f"{human} finding(s) {verb} a person. None could be closed "
                f"automatically.")
    return (f"{auto} of {total} closed automatically; {human} {verb} you. "
            f"The reason each one is yours to decide is on the card.")
