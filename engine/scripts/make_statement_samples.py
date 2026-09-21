#!/usr/bin/env python3
"""
One bank account, one week, in the four formats banks send: MT940, CAMT.053,
OFX and a text PDF. Each carries the demo settlement credit (UTR20260901001,
Rs 66,466.36) among ordinary debits and credits, so any of them can stand in
for the CSV bank statement in the main sample.

The figures are invented and identical across the four files; each balances
(opening + credits - debits = closing), which is what the parsers check.

The PDF is written by hand — a minimal text-layer PDF, one line of text per
table row — so that producing it needs no PDF library.

From engine/:
    python scripts/make_statement_samples.py
"""

from __future__ import annotations

import os
from datetime import date

OUT = os.path.join(os.path.dirname(__file__), "..", "..", "public", "sample-data", "statements")
ACCOUNT = "50200012345678"
OPENING = 1_254_320_00

# (date, description, reference, amount in paise: + credit, - debit)
LINES = [
    (date(2026, 8, 31), "NEFT DR VENDOR ANAND PACKAGING", "N243250001", -85_000_00),
    (date(2026, 9, 1), "NEFT CR UTR20260901001 RAZORPAY SOFTWARE PVT LTD SETTLE-001", "UTR20260901001", 66_466_36),
    (date(2026, 9, 1), "UPI CR 624418889120 SHARMA TRADERS", "624418889120", 12_500_00),
    (date(2026, 9, 2), "ACH DR ELECTRICITY BESCOM", "ACH0909221", -8_742_50),
    (date(2026, 9, 3), "IMPS CR 624617700031 REFUND FROM SUPPLIER", "624617700031", 4_210_00),
    (date(2026, 9, 4), "SALARY BATCH SEP 2026", "SAL2609", -3_20_000_00),
]
CLOSING = OPENING + sum(a for *_, a in LINES)


def rupees(p: int, comma_decimal: bool = False) -> str:
    s = f"{abs(p) / 100:.2f}"
    return s.replace(".", ",") if comma_decimal else s


def indian(p: int) -> str:
    whole, frac = f"{abs(p) / 100:.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail]) + "." + frac


def mt940() -> str:
    out = [":20:STMT20260904", f":25:HDFC0000123/{ACCOUNT}", ":28C:00123/001",
           f":60F:C260830INR{rupees(OPENING, True)}"]
    for d, desc, ref, amt in LINES:
        mark = "C" if amt > 0 else "D"
        out.append(f":61:{d:%y%m%d}{d:%m%d}{mark}{rupees(amt, True)}NTRF{ref}//{ref[-8:]}")
        out.append(f":86:{desc}")
    out += [f":62F:C260904INR{rupees(CLOSING, True)}", "-}"]
    return "{1:F01HDFCINBBAXXX0000000000}{2:O9400000260904HDFCINBBAXXX00000000002609040000N}{4:\n" \
        + "\n".join(out) + "\n"


def camt053() -> str:
    entries = []
    for d, desc, ref, amt in LINES:
        entries.append(f"""      <Ntry>
        <Amt Ccy="INR">{rupees(amt)}</Amt>
        <CdtDbtInd>{'CRDT' if amt > 0 else 'DBIT'}</CdtDbtInd>
        <Sts><Cd>BOOK</Cd></Sts>
        <BookgDt><Dt>{d.isoformat()}</Dt></BookgDt>
        <ValDt><Dt>{d.isoformat()}</Dt></ValDt>
        <AcctSvcrRef>{ref}</AcctSvcrRef>
        <NtryDtls><TxDtls><Refs><EndToEndId>{ref}</EndToEndId></Refs>
          <RmtInf><Ustrd>{desc}</Ustrd></RmtInf></TxDtls></NtryDtls>
      </Ntry>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">
  <BkToCstmrStmt>
    <GrpHdr><MsgId>STMT20260904</MsgId><CreDtTm>2026-09-04T23:59:00+05:30</CreDtTm></GrpHdr>
    <Stmt>
      <Id>STMT20260904-001</Id>
      <Acct><Id><Othr><Id>{ACCOUNT}</Id></Othr></Id><Ccy>INR</Ccy></Acct>
      <Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy="INR">{rupees(OPENING)}</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-08-30</Dt></Dt></Bal>
      <Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="INR">{rupees(CLOSING)}</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-09-04</Dt></Dt></Bal>
{chr(10).join(entries)}
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""


def ofx() -> str:
    trns = "\n".join(
        f"<STMTTRN><TRNTYPE>{'CREDIT' if amt > 0 else 'DEBIT'}<DTPOSTED>{d:%Y%m%d}"
        f"<TRNAMT>{'' if amt > 0 else '-'}{rupees(amt)}<FITID>{ref}<NAME>{desc[:32]}"
        f"<MEMO>{desc}</STMTTRN>" for d, desc, ref, amt in LINES)
    return f"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
ENCODING:USASCII

<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>INR
<BANKACCTFROM><BANKID>HDFC0000123<ACCTID>{ACCOUNT}<ACCTTYPE>CURRENT</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260831<DTEND>20260904
{trns}
</BANKTRANLIST>
<LEDGERBAL><BALAMT>{rupees(CLOSING)}<DTASOF>20260904</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


def pdf_lines() -> list[str]:
    rows = [f"Account No: {ACCOUNT}   Statement 31/08/2026 to 04/09/2026   Currency INR",
            f"Opening Balance {indian(OPENING)}",
            "Date Narration Ref Withdrawal Deposit Balance"]
    bal = OPENING
    for d, desc, ref, amt in LINES:
        bal += amt
        rows.append(f"{d:%d/%m/%Y} {desc} {ref} {indian(amt)} {indian(bal)}")
    rows.append(f"Closing Balance {indian(CLOSING)}")
    return rows


def pdf(lines: list[str]) -> bytes:
    """A one-page PDF whose text layer is these lines, top to bottom."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    ops = ["BT", "/F1 8 Tf", "40 800 Td", "11 TL"]
    for i, ln in enumerate(lines):
        ops.append(f"({esc(ln)}) Tj" if i == 0 else f"T* ({esc(ln)}) Tj")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    files = {
        "statement.mt940": mt940().encode(),
        "statement_camt053.xml": camt053().encode(),
        "statement.ofx": ofx().encode(),
        "statement.pdf": pdf(pdf_lines()),
    }
    for name, body in files.items():
        with open(os.path.join(OUT, name), "wb") as f:
            f.write(body)
    print(f"opening {OPENING / 100:,.2f} closing {CLOSING / 100:,.2f} -> {os.path.abspath(OUT)}")


if __name__ == "__main__":
    main()
