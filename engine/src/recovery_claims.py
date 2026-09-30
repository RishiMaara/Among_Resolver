"""
Recovery claims: what a payout check found that someone owes the merchant,
written up so it can be sent.

The Razorpay check (razorpay_recon.reconcile) already finds money: a fee
above the rate card, GST charged on the payment instead of on the fee, a bank
credit short of the payout, lines that sum to more than was paid out. A
finding in a report is where most of that money stays. This turns each one
into a claim: who to send it to, how much, the evidence line by line, and a
message a person can paste into a support ticket or an email.

WHAT IT DOES NOT DO
-------------------
It sends nothing. A claim is a draft for a person to read and send; nothing
here reaches Razorpay or a bank.

It does not overstate. Each claim says what its figure rests on:
  stated     both numbers come from the data itself (the bank credit against
             the payout, the lines against the payout). Nothing is assumed.
  estimated  the expected fee comes from the rate card the engine audits
             against. A merchant with negotiated pricing must check the
             expected figure before sending, and the message says so.
Findings in the merchant's favour (a fee below the rate card) are not claims.
"""

from __future__ import annotations

import csv
import io

RAZORPAY = "Razorpay Support"
BANK = "Your bank"


def _rs(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def _claim(r: dict, kind: str, to: str, amount: int, basis: str, subject: str,
           lines: list[str], evidence: list[dict] | None = None,
           columns: list[str] | None = None, check_first: str = "") -> dict:
    sid, utr = r["settlement_id"], r.get("utr") or "not stated"
    body = "\n".join([
        "Hello,",
        "",
        *lines,
        "",
        f"Settlement: {sid}",
        f"UTR: {utr}",
        f"Settled on: {r.get('settled_on', '')}",
        f"Amount claimed: {_rs(amount)}",
        *([f"The line-by-line evidence is attached ({_n(len(evidence), 'line')})."] if evidence else []),
        "",
        "Please confirm the correction, or explain the difference.",
        "",
        "Thank you.",
    ])
    return {
        "claim_id": f"{sid}:{kind}", "settlement_id": sid, "utr": r.get("utr"),
        "kind": kind, "to": to, "amount_cents": amount,
        "currency": r.get("currency") or "INR", "basis": basis,
        "check_first": check_first, "subject": subject, "message": body,
        "evidence": evidence or [],
        "evidence_csv": _csv(evidence, columns) if evidence and columns else "",
    }


def _fee_claims(r: dict) -> list[dict]:
    fees = r["checks"].get("fees") or {}
    findings = fees.get("findings") or []
    out = []
    over = [f for f in findings if f["category"] == "fee_overcharge" and f["difference_cents"] > 0]
    total = int((fees.get("summary") or {}).get("total_overcharge_cents") or 0)
    if over and total > 0:
        rows = [{"payment_id": f["txn_id"], "expected_fee": f"{f.get('expected_cents', 0) / 100:.2f}",
                 "charged_fee": f"{f.get('actual_cents', 0) / 100:.2f}",
                 "difference": f"{f['difference_cents'] / 100:.2f}"} for f in over]
        # The report lists at most 20 findings; the summary total covers all.
        more = total > sum(f["difference_cents"] for f in over)
        out.append(_claim(
            r, "fee_overcharge", RAZORPAY, total, "estimated",
            f"Fee above the expected rate on settlement {r['settlement_id']}",
            [f"On settlement {r['settlement_id']}, {_n(len(over), 'payment')} "
             f"{'was' if len(over) == 1 else 'were'} charged a fee above the rate we "
             f"expect, {_rs(total)} in total."
             + (" The attachment lists the largest; the total covers every payment." if more else ""),
             "The expected fee is from the rate card our reconciliation audits against."],
            rows, ["payment_id", "expected_fee", "charged_fee", "difference"],
            check_first=("The expected fee comes from the rate card the engine audits against. "
                         "If your account has negotiated pricing, confirm the expected figures "
                         "before sending.")))
    gst = [f for f in findings if f["category"] == "gst_miscalculation" and f["difference_cents"] > 0]
    if gst:
        amount = sum(f["difference_cents"] for f in gst)
        rows = [{"payment_id": f["txn_id"], "expected_gst": f"{f.get('expected_cents', 0) / 100:.2f}",
                 "charged_gst": f"{f.get('actual_cents', 0) / 100:.2f}",
                 "difference": f"{f['difference_cents'] / 100:.2f}"} for f in gst]
        out.append(_claim(
            r, "gst_overcharge", RAZORPAY, amount, "estimated",
            f"GST charged above 18% of the fee on settlement {r['settlement_id']}",
            [f"On settlement {r['settlement_id']}, GST on {_n(len(gst), 'payment')} is higher than "
             f"18% of the fee charged, {_rs(amount)} in total. GST on a payment gateway's "
             f"service is due on the fee, not on the payment."],
            rows, ["payment_id", "expected_gst", "charged_gst", "difference"],
            check_first="Confirm the GST rate on your invoice before sending."))
    return out


def _bank_claims(r: dict) -> list[dict]:
    bank = r["checks"].get("bank") or {}
    verdict = bank.get("verdict")
    if verdict == "amount_differs" and int(bank.get("difference_cents") or 0) < 0:
        short = -int(bank["difference_cents"])
        return [_claim(
            r, "short_credit", BANK, short, "stated",
            f"Payout credited short: UTR {r.get('utr')}",
            [f"Razorpay settlement {r['settlement_id']} of {_rs(r['amount_cents'])} was paid "
             f"under UTR {r.get('utr')}. Our statement shows credit {bank.get('bank_txn_id')} "
             f"under that UTR for {_rs(r['amount_cents'] - short)}, {_rs(short)} less.",
             "Please credit the difference or tell us where it went."])]
    if verdict == "not_found":
        return [_claim(
            r, "payout_not_received", RAZORPAY, int(r["amount_cents"]), "stated",
            f"Payout not received: settlement {r['settlement_id']}",
            [f"Settlement {r['settlement_id']} of {_rs(r['amount_cents'])} is marked paid "
             f"under UTR {r.get('utr') or '(none stated)'}, but no credit with that UTR or "
             f"amount appears in our bank statement."],
            check_first=("Confirm the statement covers the payout date and the working days "
                         "after it before sending; a payout can land a day or two later."))]
    return []


def _tie_out_claim(r: dict) -> list[dict]:
    tie = r["checks"].get("tie_out") or {}
    residual = int(tie.get("residual_paise") or 0)
    if tie.get("verdict") != "does_not_tie_out" or residual <= 0:
        return []
    return [_claim(
        r, "underpaid_settlement", RAZORPAY, residual, "stated",
        f"Settlement paid less than its lines: {r['settlement_id']}",
        [f"The {_n(int(tie.get('members') or 0), 'line')} your report lists for settlement "
         f"{r['settlement_id']} add up to {_rs(tie.get('sum_of_net_paise', 0))} after fees, "
         f"but the settlement paid {_rs(tie.get('settlement_amount_paise', 0))}, "
         f"{_rs(residual)} less. Please send the breakdown of the difference."])]


def build_claims(report: dict) -> dict:
    """Every claim a Razorpay payout check supports, with totals."""
    claims: list[dict] = []
    for r in report.get("results") or []:
        claims += _tie_out_claim(r) + _bank_claims(r) + _fee_claims(r)
    stated = sum(c["amount_cents"] for c in claims if c["basis"] == "stated")
    estimated = sum(c["amount_cents"] for c in claims if c["basis"] == "estimated")
    return {
        "claims": claims,
        "total_cents": stated + estimated,
        "stated_cents": stated,
        "estimated_cents": estimated,
        "plain": (
            f"{_n(len(claims), 'claim')} drafted, {_rs(stated + estimated)} in total"
            + (f": {_rs(stated)} rests on the data alone and {_rs(estimated)} on the rate card."
               if stated and estimated else ".")
            + " Nothing has been sent." if claims else
            "Nothing to claim: no payout was short and no fee was above the rate card."),
    }
