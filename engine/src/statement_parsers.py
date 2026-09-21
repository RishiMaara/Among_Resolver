"""
Bank statements in the formats banks actually send: MT940, CAMT.053, OFX, PDF.

WHY
---
A CSV export is what a person makes by hand. What a treasury system receives
from the bank is SWIFT MT940, ISO 20022 CAMT.053, OFX from an online-banking
download, or a PDF. Asking for a CSV first puts a person between the bank and
the reconciliation — the step where columns get renamed, rows get filtered
and a statement quietly stops being the bank's.

EVERY PARSE PROVES ITSELF
-------------------------
A parser that misreads one amount produces a reconciliation that is wrong
with a straight face. So every statement is checked against the rule the bank
itself guarantees, borrowed from the open-source bankstatementparser project
(Apache-2.0 / MIT), which calls it the Golden Rule:

    opening balance + credits - debits = closing balance

and, where the statement prints a running balance on each line, every line is
checked against the one before it. For a PDF this does double duty: a text
layer does not say which column an amount came from, and the running balance
does — if the balance went up by the amount it was a credit, if down a debit.
A statement that does not balance is REFUSED with the arithmetic shown,
never partly used.

bankstatementparser itself is not a dependency: its core pulls lxml and pins
pandas below 3, and this engine ships inside a serverless bundle within a few
megabytes of its limit. The parsers here use the standard library, with
defusedxml for XML from outside and pypdf for PDF text.

WHAT IS NOT HANDLED
-------------------
Scanned PDFs, which have no text layer; they are refused with that reason.
PDF layouts vary by bank: the reader here handles the common single-line
table (date, narration, reference, withdrawal, deposit, balance), and the
balance check is what stops a layout it does not understand from getting
through.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import narration_reader

FORMATS = ("mt940", "camt053", "ofx", "pdf")


@dataclass
class StatementLine:
    booked: date
    amount_cents: int                 # positive credit, negative debit
    description: str = ""
    reference: str = ""
    value_date: date | None = None
    balance_cents: int | None = None
    bank_reference: str = ""


@dataclass
class ParsedStatement:
    format: str
    account: str = ""
    currency: str = "INR"
    opening_cents: int | None = None
    closing_cents: int | None = None
    lines: list[StatementLine] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def credits_cents(self) -> int:
        return sum(ln.amount_cents for ln in self.lines if ln.amount_cents > 0)

    @property
    def debits_cents(self) -> int:
        return -sum(ln.amount_cents for ln in self.lines if ln.amount_cents < 0)


class StatementUnreadable(ValueError):
    """The file is a statement format this module reads, but not readably."""


# ── amounts ───────────────────────────────────────────────────────────────

def _paise(text: str, decimal_comma: bool = False) -> int:
    t = (text or "").strip().replace(" ", "")
    if decimal_comma:
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", "")
    t = t.replace("₹", "").replace("INR", "").replace("Rs.", "").replace("Rs", "")
    if not re.fullmatch(r"-?\d+(\.\d{1,2})?", t):
        raise StatementUnreadable(f"not an amount: {text!r}")
    neg = t.startswith("-")
    whole, _, frac = t.lstrip("-").partition(".")
    value = int(whole) * 100 + int((frac + "00")[:2])
    return -value if neg else value


def _yymmdd(s: str) -> date:
    return date(2000 + int(s[0:2]), int(s[2:4]), int(s[4:6]))


# ── detection ─────────────────────────────────────────────────────────────

def detect(content: bytes, filename: str = "") -> str | None:
    """Which statement format this is, or None for anything else."""
    name = (filename or "").lower()
    head = content[:4096]
    if head.startswith(b"%PDF"):
        return "pdf"
    text = head.decode("utf-8", errors="ignore")
    if "camt.053" in text or "BkToCstmrStmt" in text:
        return "camt053"
    if "OFXHEADER" in text or "<OFX>" in text.upper():
        return "ofx"
    if re.search(r":20:", text) and re.search(r":6[02][FM]:", content.decode("utf-8", errors="ignore")):
        return "mt940"
    if name.endswith((".sta", ".mt940", ".940")):
        return "mt940"
    return None


# ── MT940 ─────────────────────────────────────────────────────────────────

_TAG = re.compile(r"^:(\d{2}[A-Z]?):", re.M)
_BAL = re.compile(r"^([CD])(\d{6})([A-Z]{3})([\d,]+)$")
_LINE61 = re.compile(
    r"^(?P<vdate>\d{6})(?P<edate>\d{4})?(?P<mark>RC|RD|C|D)(?P<funds>[A-Z])?"
    r"(?P<amount>[\d,]+)(?P<type>[A-Z0-9]{4})(?P<ref>[^/\n]*)(?://(?P<bankref>[^\n]*))?"
    r"(?:\n(?P<extra>.*))?$", re.S)


def parse_mt940(text: str) -> ParsedStatement:
    st = ParsedStatement("mt940")
    fields = []
    positions = [(m.start(), m.group(1), m.end()) for m in _TAG.finditer(text)]
    for i, (_, tag, body_start) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        body = text[body_start:end].strip()
        body = re.sub(r"\n-\}?\s*$", "", body).strip()
        fields.append((tag, body))
    if not fields:
        raise StatementUnreadable("no MT940 fields (:20:, :60F:, :61:, :62F:) found")

    pending: StatementLine | None = None
    for tag, body in fields:
        if tag == "25":
            st.account = body
        elif tag in ("60F", "60M", "62F", "62M"):
            m = _BAL.match(body.replace("\n", ""))
            if not m:
                raise StatementUnreadable(f"balance field :{tag}: unreadable: {body!r}")
            value = _paise(m.group(4), decimal_comma=True) * (-1 if m.group(1) == "D" else 1)
            st.currency = m.group(3)
            if tag.startswith("60") and st.opening_cents is None:
                st.opening_cents = value
            elif tag.startswith("62"):
                st.closing_cents = value
        elif tag == "61":
            m = _LINE61.match(body)
            if not m:
                raise StatementUnreadable(f"statement line :61: unreadable: {body[:60]!r}")
            vdate = _yymmdd(m.group("vdate"))
            booked = vdate
            if m.group("edate"):
                booked = date(vdate.year, int(m.group("edate")[:2]), int(m.group("edate")[2:]))
            amount = _paise(m.group("amount"), decimal_comma=True)
            mark = m.group("mark")
            sign = 1 if mark in ("C", "RD") else -1        # RD reverses a debit: money in
            pending = StatementLine(booked=booked, value_date=vdate, amount_cents=sign * amount,
                                    reference=(m.group("ref") or "").strip(),
                                    bank_reference=(m.group("bankref") or "").strip(),
                                    description=(m.group("extra") or "").strip())
            st.lines.append(pending)
        elif tag == "86" and pending is not None:
            pending.description = " ".join(filter(None, [pending.description,
                                                         " ".join(body.split())]))
    return st


# ── CAMT.053 ──────────────────────────────────────────────────────────────

def parse_camt053(content: bytes) -> ParsedStatement:
    from defusedxml import ElementTree as ET  # pylint: disable=import-outside-toplevel
    try:
        root = ET.fromstring(content)
    except Exception as exc:
        raise StatementUnreadable(f"CAMT.053 is not well-formed XML ({type(exc).__name__})")
    ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""

    def f(node, path):
        return node.find("/".join(ns + p for p in path.split("/")))

    def text(node, path, default=""):
        el = f(node, path)
        return (el.text or "").strip() if el is not None and el.text else default

    stmt = f(root, "BkToCstmrStmt/Stmt")
    if stmt is None:
        raise StatementUnreadable("no BkToCstmrStmt/Stmt element")
    st = ParsedStatement("camt053")
    st.account = text(stmt, "Acct/Id/IBAN") or text(stmt, "Acct/Id/Othr/Id")
    for bal in stmt.findall(ns + "Bal"):
        code = text(bal, "Tp/CdOrPrtry/Cd")
        amt_el = f(bal, "Amt")
        if amt_el is None:
            continue
        value = _paise(amt_el.text) * (-1 if text(bal, "CdtDbtInd") == "DBIT" else 1)
        st.currency = amt_el.get("Ccy") or st.currency
        if code in ("OPBD", "PRCD") and st.opening_cents is None:
            st.opening_cents = value
        elif code == "CLBD":
            st.closing_cents = value
    for n in stmt.findall(ns + "Ntry"):
        if text(n, "Sts/Cd", text(n, "Sts")) not in ("BOOK", ""):
            st.notes.append("A pending (not booked) entry was left out, as the bank's balance does.")
            continue
        amt_el = f(n, "Amt")
        amount = _paise(amt_el.text) * (-1 if text(n, "CdtDbtInd") == "DBIT" else 1)
        booked = text(n, "BookgDt/Dt") or text(n, "BookgDt/DtTm")[:10]
        vd = text(n, "ValDt/Dt")
        info = " ".join(filter(None, [text(n, "NtryDtls/TxDtls/RmtInf/Ustrd"),
                                      text(n, "AddtlNtryInf")]))
        st.lines.append(StatementLine(
            booked=date.fromisoformat(booked), value_date=date.fromisoformat(vd) if vd else None,
            amount_cents=amount, description=info,
            reference=text(n, "NtryDtls/TxDtls/Refs/EndToEndId") or text(n, "AcctSvcrRef"),
            bank_reference=text(n, "AcctSvcrRef")))
    return st


# ── OFX ───────────────────────────────────────────────────────────────────

def _ofx_value(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}>([^<\r\n]*)", block, re.I)
    return m.group(1).strip() if m else ""


def _ofx_date(s: str) -> date:
    return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))


def _ofx_description(block: str) -> str:
    name, memo = _ofx_value(block, "NAME"), _ofx_value(block, "MEMO")
    if memo and name and memo.startswith(name):
        return memo
    return " ".join(filter(None, [name, memo]))


def parse_ofx(text: str) -> ParsedStatement:
    st = ParsedStatement("ofx")
    st.currency = _ofx_value(text, "CURDEF") or "INR"
    st.account = _ofx_value(text, "ACCTID")
    for block in re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|(?=</BANKTRANLIST>))",
                            text, re.S | re.I):
        amt = _ofx_value(block, "TRNAMT")
        posted = _ofx_value(block, "DTPOSTED")
        if not amt or not posted:
            continue
        st.lines.append(StatementLine(
            booked=_ofx_date(posted), amount_cents=_paise(amt),
            description=_ofx_description(block),
            reference=_ofx_value(block, "FITID"), bank_reference=_ofx_value(block, "REFNUM")))
    ledger = re.search(r"<LEDGERBAL>(.*?)(?:</LEDGERBAL>|<AVAILBAL>|$)", text, re.S | re.I)
    if ledger and _ofx_value(ledger.group(1), "BALAMT"):
        st.closing_cents = _paise(_ofx_value(ledger.group(1), "BALAMT"))
        # OFX states no opening balance. Deriving one from the closing
        # balance makes the Golden Rule true by construction, so it is
        # reported as derived and the rule is not claimed as checked.
        st.opening_cents = st.closing_cents - sum(ln.amount_cents for ln in st.lines)
        st.notes.append("OFX states only the closing balance; the opening balance is derived "
                        "from it, so the balance check cannot catch a misread line here.")
    return st


# ── PDF (text layer) ──────────────────────────────────────────────────────

_DATE = (r"(\d{2}[/-]\d{2}[/-]\d{2,4}|\d{2}\s+[A-Za-z]{3}\s+\d{2,4}|\d{2}-[A-Za-z]{3}-\d{2,4})")
_AMT = r"(-?\d{1,3}(?:,\d{2,3})*(?:\.\d{2})|-?\d+\.\d{2})"
_ROW = re.compile(rf"^\s*{_DATE}\s+(.+?)\s+{_AMT}(?:\s+{_AMT})?(?:\s+{_AMT})?\s*(Cr|Dr|CR|DR)?\s*$")
_OPENING = re.compile(rf"opening\s+balance[^\d-]*{_AMT}", re.I)
_CLOSING = re.compile(rf"closing\s+balance[^\d-]*{_AMT}", re.I)


def _pdf_date(s: str) -> date:
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d %b %Y", "%d %b %y",
                "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise StatementUnreadable(f"unreadable date {s!r}")


def pdf_text(content: bytes) -> str:
    from pypdf import PdfReader  # pylint: disable=import-outside-toplevel
    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as exc:
        raise StatementUnreadable(f"the PDF could not be opened ({type(exc).__name__})")


def parse_pdf(content: bytes) -> ParsedStatement:
    text = pdf_text(content)
    if len(text.strip()) < 20:
        raise StatementUnreadable(
            "this PDF has no text layer — it is a scan. Download the statement again as "
            "a text PDF, MT940 or CAMT.053 from net banking; a scanned image needs OCR, "
            "which this engine does not do on its own.")
    st = ParsedStatement("pdf")
    m = _OPENING.search(text)
    if m:
        st.opening_cents = _paise(m.group(1))
    m = _CLOSING.search(text)
    if m:
        st.closing_cents = _paise(m.group(1))
    acct = re.search(r"(?:A/?c|Account)\s*(?:No\.?|Number)?\s*[:.]?\s*([0-9Xx*]{6,20})", text)
    if acct:
        st.account = acct.group(1)

    prev = st.opening_cents
    for raw in text.splitlines():
        m = _ROW.match(raw)
        if not m or "balance" in m.group(2).lower() and "opening" in m.group(2).lower():
            continue
        amounts = [a for a in (m.group(3), m.group(4), m.group(5)) if a]
        booked, rest = _pdf_date(m.group(1)), m.group(2).strip()
        if len(amounts) < 2:
            continue      # a line with no running balance cannot be placed; see below
        balance = _paise(amounts[-1])
        movement = [_paise(a) for a in amounts[:-1] if _paise(a) != 0]
        if len(movement) != 1:
            raise StatementUnreadable(f"line {raw.strip()[:60]!r}: cannot tell the amount "
                                      f"from the balance")
        amt = abs(movement[0])
        # The column is not in the text layer; the running balance says it.
        if prev is None:
            sign = -1 if (m.group(6) or "").upper() == "DR" else 1
        elif prev + amt == balance:
            sign = 1
        elif prev - amt == balance:
            sign = -1
        else:
            raise StatementUnreadable(
                f"line dated {booked.isoformat()}: {amt / 100:,.2f} does not move the balance "
                f"from {prev / 100:,.2f} to {balance / 100:,.2f} either way — misread or missing line")
        ref = ""
        parts = rest.split()
        if len(parts) > 1 and re.fullmatch(r"[A-Z0-9/-]{6,}", parts[-1]):
            ref = parts[-1]
            rest = " ".join(parts[:-1])
        st.lines.append(StatementLine(booked=booked, amount_cents=sign * amt, description=rest,
                                      reference=ref, balance_cents=balance))
        prev = balance
    if st.opening_cents is None and st.lines:
        first = st.lines[0]
        st.opening_cents = first.balance_cents - first.amount_cents
        st.notes.append("No opening balance printed; derived from the first line's balance.")
    if st.closing_cents is None and st.lines:
        st.closing_cents = st.lines[-1].balance_cents
        st.notes.append("No closing balance printed; the last line's balance is used.")
    if not st.lines:
        raise StatementUnreadable("no transaction lines recognised in the PDF's text")
    return st


# ── the proof ─────────────────────────────────────────────────────────────

def verify(st: ParsedStatement) -> dict:
    """opening + credits - debits = closing, and every running balance."""
    rule: dict | None = None
    if st.opening_cents is not None and st.closing_cents is not None:
        expected = st.opening_cents + st.credits_cents - st.debits_cents
        rule = {
            "holds": expected == st.closing_cents,
            "opening_cents": st.opening_cents, "credits_cents": st.credits_cents,
            "debits_cents": st.debits_cents, "closing_cents": st.closing_cents,
            "difference_cents": st.closing_cents - expected,
            "checkable": st.format != "ofx",
        }

    running: dict | None = None
    if st.opening_cents is not None and any(ln.balance_cents is not None for ln in st.lines):
        prev, bad = st.opening_cents, None
        for i, ln in enumerate(st.lines):
            if ln.balance_cents is None:
                continue
            if prev + ln.amount_cents != ln.balance_cents:
                bad = i
                break
            prev = ln.balance_cents
        running = {"holds": bad is None, "first_bad_line": bad}

    holds = rule is not None and rule["holds"] and (running is None or running["holds"])
    if rule is not None:
        total = rule["opening_cents"] + rule["credits_cents"] - rule["debits_cents"]
        plain = (
            f"Opening ₹{rule['opening_cents'] / 100:,.2f} + credits ₹{rule['credits_cents'] / 100:,.2f}"
            f" − debits ₹{rule['debits_cents'] / 100:,.2f} = ₹{total / 100:,.2f}; "
            f"the statement's closing balance is ₹{rule['closing_cents'] / 100:,.2f}"
            + (" — it balances." if rule["holds"] else
               f" — off by ₹{abs(rule['difference_cents']) / 100:,.2f}."))
    else:
        plain = "The statement states no opening and closing balance, so it cannot be checked."
    return {"golden_rule": rule, "running_balance": running, "holds": holds, "plain": plain}


def parse(content: bytes, filename: str = "") -> tuple[ParsedStatement, dict]:
    kind = detect(content, filename)
    if kind is None:
        raise StatementUnreadable("not an MT940, CAMT.053, OFX or PDF statement")
    if kind == "pdf":
        st = parse_pdf(content)
    elif kind == "camt053":
        st = parse_camt053(content)
    else:
        text = content.decode("utf-8", errors="replace")
        st = parse_mt940(text) if kind == "mt940" else parse_ofx(text)
    return st, verify(st)


def to_rows(st: ParsedStatement, credits_only: bool = True) -> list[dict]:
    """
    Rows in the shape the ingestion layer takes, credits only by default.

    A settlement arrives as a credit. Outgoing payments cannot compose one,
    and admitting them to the pool gives the solver negative numbers to
    combine with real credits into sums that never happened.
    """
    rows, seen = [], set()
    for i, ln in enumerate(st.lines):
        if credits_only and ln.amount_cents <= 0:
            continue
        # The customer reference carries the UTR on most rails; the bank's
        # own reference is the fallback. Banks reuse placeholders ("NONREF"),
        # so an id already used gets the line number appended.
        ident = ln.reference or ln.bank_reference or f"{st.format.upper()}-{ln.booked:%Y%m%d}"
        if ident in seen or ident.upper() == "NONREF":
            ident = f"{ident}-{i:04d}"
        seen.add(ident)
        rows.append({
            "txn_id": ident,
            "ref_id": ln.reference or ident,
            "amount": f"{ln.amount_cents / 100:.2f}",
            "currency": st.currency,
            "timestamp": datetime(ln.booked.year, ln.booked.month, ln.booked.day,
                                  tzinfo=timezone.utc).isoformat(),
            "memo": ln.description,
            "statement_format": st.format,
            "value_date": ln.value_date.isoformat() if ln.value_date else "",
            # What the narration says, read by rules only: ingestion stays
            # deterministic, and the model reader is an explicit request
            # (POST /narrations/read), never a side effect of an upload.
            **{f"narration_{k}": v for k, v in
               narration_reader.regex_read(ln.description).__dict__.items() if v},
        })
    return rows
