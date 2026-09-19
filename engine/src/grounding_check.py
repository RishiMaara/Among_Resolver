"""
Check an AI answer against the facts it was given — after it is written.

WHY A PROMPT IS NOT ENOUGH
--------------------------
Settlement Q&A already hands the model only the engine's recorded results and
tells it, in the system prompt, never to invent a figure. That is a request.
A model that ignores it — because a memo in an uploaded file said to, because
it rounded something into a different number, because it "helpfully" added a
total the engine never computed — produces an answer that reads exactly as
authoritative as a correct one. The reviewer cannot tell which figures came
from the ledger and which came from the model.

So every answer is checked before it is shown. Each figure, transaction id and
date in it must be traceable to the grounding the model was given (or to the
question the person asked). Anything that is not traceable means the answer
is withheld, the reason is logged, and the reviewer gets the engine's own
deterministic summary instead.

WHAT COUNTS AS TRACEABLE
------------------------
A number in the answer matches a number in the grounding if it is that number,
or that number rounded to the precision the answer used. Two unit changes are
understood, because a correct answer uses them constantly:

  * paise to rupees — a field ending in _cents or _paise holding 6646636 is
    grounding for "66,466.36" and for "66,466";
  * a fraction to a percentage — 0.95 is grounding for "95%".

Indian and Western digit grouping are both read ("1,00,000" and "100,000").

WHAT IS DELIBERATELY NOT CHECKED
--------------------------------
Numbered-list markers at the start of a line ("1. The batch ...") are layout,
not claims. Ordinals ("2nd") are words. Numbers spelled out in words are not
read at all — a model that writes "fourteen" is not caught here.

Two limits worth stating plainly, because each is a way to over-read a pass:

  * A figure is traced by VALUE, not by meaning. "3 exceptions" passes if a 3
    appears anywhere in the results, even as a match count. The check catches
    invented numbers, not a real number attached to the wrong noun.
  * The grounding includes text that arrived in uploaded files. A figure
    planted there reaches the grounding and so counts as traceable. The Q&A
    prompt fences that text as untrusted; this check does not second-guess it.

It is a strong filter, not a proof, and the tests pin both edges.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# A token mixing letters and digits: pay_Kx81, SETTLE-001, E12_T0, T1.
_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])"
    r"(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])"
    r"[A-Za-z0-9][A-Za-z0-9_\-]*"
    r"(?![A-Za-z0-9_\-])"
)
_ORDINAL_RE = re.compile(r"^\d+(st|nd|rd|th)$", re.IGNORECASE)
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?")
_LIST_MARKER_RE = re.compile(r"(?m)^\s*\d+[.)]\s")
# 1,00,000 · 66,466.36 · 6646636 · 0.95 · 95%
_NUMBER_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{2,3})+|\d+)(\.\d+)?(%?)")
_MONEY_SUFFIX_RE = re.compile(r"^\s*(c\b|cents?\b|paise\b)", re.IGNORECASE)

_MONEY_KEYS = ("_cents", "_paise")


@dataclass
class GroundingVerdict:
    ok: bool
    checked: int = 0
    ungrounded_figures: list[str] = field(default_factory=list)
    ungrounded_ids: list[str] = field(default_factory=list)

    @property
    def items(self) -> list[str]:
        return self.ungrounded_figures + self.ungrounded_ids

    def describe(self) -> str:
        parts = []
        if self.ungrounded_figures:
            parts.append("figures " + ", ".join(self.ungrounded_figures))
        if self.ungrounded_ids:
            parts.append("identifiers " + ", ".join(self.ungrounded_ids))
        return "; ".join(parts) or "nothing ungrounded"


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _numbers_in_text(text: str) -> list[tuple[str, Decimal, int, bool, int]]:
    """(raw, value, decimals, is_percent, end_offset) for every numeral."""
    out = []
    for m in _NUMBER_RE.finditer(text):
        raw = m.group(0)
        value = _decimal(m.group(1) + (m.group(2) or ""))
        if value is None:
            continue
        decimals = len(m.group(2)) - 1 if m.group(2) else 0
        out.append((raw, value, decimals, bool(m.group(3)), m.end()))
    return out


class _Facts:
    """Everything the grounding says, flattened into what an answer may cite."""

    def __init__(self, grounding: dict, question: str = ""):
        self.values: set[Decimal] = set()
        self.corpus_parts: list[str] = []
        self._walk(grounding, key="")
        self._add_text(question)
        self.corpus = "\n".join(self.corpus_parts)

    def _add_number(self, value: Decimal, money: bool) -> None:
        self.values.add(value)
        if money:
            self.values.add(value / 100)
        if Decimal(0) < value <= Decimal(1):
            self.values.add(value * 100)  # a fraction may be quoted as a percent

    def _add_text(self, text: str) -> None:
        self.corpus_parts.append(text)
        for raw, value, _, _, end in _numbers_in_text(text):
            money = bool(_MONEY_SUFFIX_RE.match(text[end:end + 8]))
            self._add_number(value, money)

    def _walk(self, node, key: str) -> None:
        if isinstance(node, bool) or node is None:
            return
        if isinstance(node, (int, float)):
            value = _decimal(repr(node)) if isinstance(node, float) else Decimal(node)
            if value is not None:
                self._add_number(value, key.endswith(_MONEY_KEYS))
            self.corpus_parts.append(str(node))
            return
        if isinstance(node, str):
            self._add_text(node)
            return
        if isinstance(node, dict):
            for k, v in node.items():
                self.corpus_parts.append(str(k))
                self._walk(v, str(k))
            return
        if isinstance(node, (list, tuple)):
            for v in node:
                self._walk(v, key)
            return
        self._add_text(json.dumps(node, default=str))

    def has_number(self, value: Decimal, decimals: int, is_percent: bool) -> bool:
        quantum = Decimal(1).scaleb(-decimals)
        for g in self.values:
            if g.quantize(quantum, rounding=ROUND_HALF_UP) == value:
                return True
            if is_percent and (g * 100).quantize(quantum, rounding=ROUND_HALF_UP) == value:
                return True
        return False


def verify(answer: str, grounding: dict, question: str = "") -> GroundingVerdict:
    """
    Is every figure, identifier and date in `answer` traceable to `grounding`
    (or to the question that was asked)?
    """
    facts = _Facts(grounding, question)
    text = _LIST_MARKER_RE.sub(" ", answer)

    ungrounded_ids: list[str] = []
    checked = 0

    for m in _DATE_RE.finditer(text):
        checked += 1
        if m.group(0)[:10] not in facts.corpus:
            ungrounded_ids.append(m.group(0))
    text = _DATE_RE.sub(" ", text)

    for m in _ID_RE.finditer(text):
        token = m.group(0).strip("-_")
        if not token or _ORDINAL_RE.match(token):
            continue
        checked += 1
        if token not in facts.corpus:
            ungrounded_ids.append(token)
    # Numbers inside identifiers are part of the identifier, not claims.
    text = _ID_RE.sub(" ", text)

    ungrounded_figures: list[str] = []
    for raw, value, decimals, is_percent, _ in _numbers_in_text(text):
        checked += 1
        if not facts.has_number(value, decimals, is_percent):
            ungrounded_figures.append(raw)

    return GroundingVerdict(
        ok=not ungrounded_figures and not ungrounded_ids,
        checked=checked,
        ungrounded_figures=sorted(set(ungrounded_figures), key=ungrounded_figures.index),
        ungrounded_ids=sorted(set(ungrounded_ids), key=ungrounded_ids.index),
    )
