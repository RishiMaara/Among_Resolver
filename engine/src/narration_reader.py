"""
Read a bank narration: UTR, settlement reference, counterparty, payment rail.

WHY
---
A bank statement line is a date, an amount and a narration a bank wrote for
itself: "NEFT CR-YESB0000001-RAZORPAY SOFTWARE PVT LTD-SETTL setl_Kx92...-
YESBN12026090112345". Everything that links the credit to a payout is in that
string — the UTR the gateway quotes, its settlement id, who paid — and every
bank formats it differently, then truncates it at forty-odd characters.

TWO READERS, ONE RULE
---------------------
`regex_read` is deterministic and always runs: known rails, UTR shapes (NEFT
16, RTGS 22, IMPS/UPI 12-digit RRN), settlement-id shapes, and the payer name
between the separators. It is fast, auditable and brittle: a format it was
not written for defeats it.

`llm_read` asks a model to read the same narrations, in batches. It is held
to one rule, checked in code rather than requested in a prompt: every value
it returns must appear in the narration it came from (compared without case,
spaces or separators). A value that does not is dropped and counted as
ungrounded — a UTR the model made up is worse than none, because it looks
like evidence.

WHICH GOES FIRST WAS MEASURED, AND IT WAS NOT THE ORDER FIRST WRITTEN
--------------------------------------------------------------------
`scripts/narration_eval.py`, 240 narrations, damaged as statements damage
them, in 4 formats the regex was written against and 5 it never saw:

                                  written-for   held-out
    regex only                        88.5%       82.7%
    regex first, model fills gaps     96.9%       94.0%
    model first, regex fills gaps     97.7%       97.5%

Regex-first was the design; it loses on formats the regex does not know,
because a regex that returns the WRONG counterparty leaves no gap for the
model to fill. So with a model configured, its grounded answer comes first
and the regex covers what it left empty. The grounding rule dropped 14
values the model returned that were not in the narration. Two model passes
at temperature 0 differed by about half a point.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass

import llm_provider

logger = logging.getLogger(__name__)

FIELDS = ("utr", "settlement_ref", "counterparty", "rail")
RAILS = ("NEFT", "RTGS", "IMPS", "UPI", "NACH", "ACH", "CHQ")


@dataclass
class Narration:
    utr: str | None = None
    settlement_ref: str | None = None
    counterparty: str | None = None
    rail: str | None = None


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def grounded(value: str | None, text: str) -> bool:
    """Does this value appear in the narration, ignoring case and separators?"""
    return bool(value) and _norm(value) != "" and _norm(value) in _norm(text)


# ── the deterministic reader ──────────────────────────────────────────────

_RAIL = re.compile(r"\b(NEFT|RTGS|IMPS|UPI|NACH|ACH|CHQ|CLG)\b", re.I)
_RTGS_UTR = re.compile(r"\b([A-Z]{4}R[A-Z0-9]{17})\b")
_NEFT_UTR = re.compile(r"\b([A-Z]{4}[NH0-9][A-Z0-9]{11})\b")
_RRN = re.compile(r"(?<!\d)(\d{12})(?!\d)")
_UTR_WORD = re.compile(r"\bUTR[:\s-]*([A-Z0-9]{8,22})\b", re.I)
_SETL = re.compile(r"\b(setl_[A-Za-z0-9]{8,20}|SETTLE-?\d{3,}|SETL[A-Z0-9-]{4,20})\b", re.I)
_IFSC = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
_NAME = re.compile(r"\b([A-Z][A-Z&.]+(?:\s+[A-Z][A-Z&.]+){0,5}\s+(?:PVT\.?\s+LTD|PRIVATE\s+LIMITED|"
                   r"LIMITED|LTD|LLP|TRADERS|ENTERPRISES|PAYMENTS|SOLUTIONS))\b")


def regex_read(text: str) -> Narration:
    t = text or ""
    up = t.upper()
    out = Narration()
    m = _RAIL.search(up)
    if m:
        out.rail = {"CLG": "CHQ", "ACH": "NACH"}.get(m.group(1).upper(), m.group(1).upper())
    for pat in (_UTR_WORD, _RTGS_UTR, _NEFT_UTR):
        m = pat.search(up)
        if m and not _IFSC.fullmatch(m.group(1)):
            out.utr = m.group(1)
            break
    if out.utr is None:
        m = _RRN.search(up)
        if m:
            out.utr = m.group(1)
    m = _SETL.search(t)
    if m:
        out.settlement_ref = m.group(1)
    m = _NAME.search(up)
    if m:
        out.counterparty = " ".join(m.group(1).split())
    return out


# ── the model reader ──────────────────────────────────────────────────────

_SYSTEM = (
    "You read Indian bank statement narrations. For each narration return the UTR "
    "or RRN (the bank transaction reference), the payment gateway settlement "
    "reference if any (for example setl_..., SETTLE-..., a payout id), the "
    "counterparty (who paid, as written), and the rail (NEFT, RTGS, IMPS, UPI, NACH "
    "or CHQ). Copy values exactly as they appear in the narration. If a field is not "
    "present, return null for it. Never infer, complete or correct a value."
)
_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "i": {"type": "INTEGER"},
            "utr": {"type": "STRING", "nullable": True},
            "settlement_ref": {"type": "STRING", "nullable": True},
            "counterparty": {"type": "STRING", "nullable": True},
            "rail": {"type": "STRING", "nullable": True},
        },
        "required": ["i"],
    },
}
BATCH = 25


def _rail_in(rail: str, text: str) -> bool:
    """A rail is grounded if the narration names it (cheques as CHQ, CLG or CHEQUE)."""
    words = {"CHQ": r"CHQ|CLG|CHEQUE", "NACH": r"N?ACH"}.get(rail, rail)
    return bool(re.search(rf"(?<![A-Z])({words})(?![A-Z])", text.upper()))


def llm_read(texts: list[str], stats: dict | None = None) -> list[Narration] | None:
    """Model reading of many narrations, grounded. None if no model is available."""
    if not llm_provider.is_configured():
        return None
    stats = stats if stats is not None else {}
    stats.setdefault("ungrounded", 0)
    stats.setdefault("calls", 0)
    out: list[Narration] = [Narration() for _ in texts]
    for start in range(0, len(texts), BATCH):
        chunk = texts[start:start + BATCH]
        prompt = "\n".join(f"{i}: {t}" for i, t in enumerate(chunk))
        raw = llm_provider.generate(prompt, system=_SYSTEM, schema=_SCHEMA,
                                    max_output_tokens=4000)
        stats["calls"] += 1
        if not raw:
            continue
        try:
            rows = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Narration reader: model returned non-JSON; chunk skipped.")
            continue
        for row in rows if isinstance(rows, list) else []:
            i = row.get("i")
            if not isinstance(i, int) or not 0 <= i < len(chunk):
                continue
            n = out[start + i]
            for f in FIELDS:
                v = row.get(f)
                if v is None or str(v).strip() == "":
                    continue
                v = str(v).strip()
                if f == "rail":
                    v = v.upper()
                    ok = v in RAILS and _rail_in(v, chunk[i])
                else:
                    ok = grounded(v, chunk[i])
                if not ok:
                    stats["ungrounded"] += 1
                    continue
                setattr(n, f, v)
    return out


def read(texts: list[str], use_llm: bool = False, stats: dict | None = None) -> list[dict]:
    """
    The grounded model answer where there is one, the regex where there is not.

    Without a model — none configured, or use_llm off — this is the regex
    alone, and every field says which reader produced it.
    """
    regex = [regex_read(t) for t in texts]
    model = llm_read(texts, stats) if use_llm else None
    out = []
    for i, rx in enumerate(regex):
        row, src = {}, {}
        for f in FIELDS:
            mv = getattr(model[i], f) if model is not None else None
            if mv is not None:
                row[f], src[f] = mv, "model"
            else:
                row[f] = getattr(rx, f)
                src[f] = "regex" if row[f] is not None else None
        out.append({**row, "source": src})
    return out
