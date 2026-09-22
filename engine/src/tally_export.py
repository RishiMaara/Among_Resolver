"""
The approved posting as a Tally voucher import (XML).

Exports only a balanced journal from a cleared or accepted settlement that
a named person approved (separation of duties applies); the export is
audited and Tally posts it, not this engine. Tally's sign convention: a
debit is ISDEEMEDPOSITIVE=Yes with a NEGATIVE amount, amounts sum to zero;
it is tested, not trusted. Pass the company's own ledger names, since Tally
matches by exact name.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from xml.sax.saxutils import escape

import cash_position

DEFAULT_LEDGERS = {
    cash_position.ACC_BANK: "Bank Account",
    cash_position.ACC_GATEWAY_CLEARING: "Razorpay Clearing",
    cash_position.ACC_GATEWAY_FEES: "Payment Gateway Charges",
    cash_position.ACC_TAX_WITHHELD: "TDS Receivable",
}


def _amount(inr: float | int | str) -> Decimal:
    return Decimal(str(inr)).quantize(Decimal("0.01"))


def journal_to_tally(journal: dict, *, voucher_date: date, company: str = "",
                     ledgers: dict[str, str] | None = None,
                     narration: str = "") -> str:
    """A Tally Journal voucher for one balanced journal proposal."""
    names = {**DEFAULT_LEDGERS, **(ledgers or {})}
    entries = []
    total = Decimal("0")
    for line in journal.get("lines") or []:
        debit, credit = _amount(line.get("debit_inr") or 0), _amount(line.get("credit_inr") or 0)
        if debit == 0 and credit == 0:
            continue
        # Tally: debit -> deemed positive, negative amount; credit -> positive.
        amount = -debit if debit else credit
        total += amount
        entries.append(
            "          <ALLLEDGERENTRIES.LIST>\n"
            f"            <LEDGERNAME>{escape(names.get(line['account'], line['account']))}</LEDGERNAME>\n"
            f"            <ISDEEMEDPOSITIVE>{'Yes' if debit else 'No'}</ISDEEMEDPOSITIVE>\n"
            f"            <AMOUNT>{amount}</AMOUNT>\n"
            "          </ALLLEDGERENTRIES.LIST>")
    if total != 0:
        raise ValueError(f"voucher does not balance by {total}; refusing to export it")
    company_xml = (f"\n          <STATICVARIABLES><SVCURRENTCOMPANY>{escape(company)}"
                   f"</SVCURRENTCOMPANY></STATICVARIABLES>" if company else "")
    entry_id = escape(str(journal.get("entry_id") or ""))
    return f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Vouchers</REPORTNAME>{company_xml}
      </REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
        <VOUCHER VCHTYPE="Journal" ACTION="Create">
          <DATE>{voucher_date:%Y%m%d}</DATE>
          <VOUCHERTYPENAME>Journal</VOUCHERTYPENAME>
          <VOUCHERNUMBER>{entry_id}</VOUCHERNUMBER>
          <NARRATION>{escape(narration)}</NARRATION>
{chr(10).join(entries)}
        </VOUCHER>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>
"""
