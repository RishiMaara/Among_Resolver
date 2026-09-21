"""
The approved posting, as a Tally import file.

WHY TALLY
---------
Most Indian SMEs keep their books in Tally, and Tally imports vouchers as XML
(ENVELOPE / HEADER / BODY / IMPORTDATA / TALLYMESSAGE / VOUCHER), either as a
file or posted to its HTTP port. A reconciliation that ends in a journal the
accountant has to retype has not closed the loop; one that ends in an import
file has.

WHAT IT WILL AND WILL NOT EXPORT
--------------------------------
Only a journal that balanced, from a settlement that cleared or was accepted,
and that a named person APPROVED — the same approval separation of duties
already guards, so whoever accepted the match cannot also be the one whose
approval releases the file. The export is recorded in the audit trail with
who asked for it. The engine still posts nothing: Tally does, when someone
imports the file.

TALLY'S SIGN CONVENTION
-----------------------
In a voucher's ledger entries a debit is ISDEEMEDPOSITIVE=Yes with a NEGATIVE
amount, a credit ISDEEMEDPOSITIVE=No with a positive one, and the amounts sum
to zero. Getting this backwards imports a mirror-image entry without an
error, which is why it is tested rather than trusted.

Ledger names default to plain ones; pass the names in the company's own chart
of accounts, because Tally matches ledgers by exact name and creates nothing.
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
