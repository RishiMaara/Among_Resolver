"""
Recovery claims drafted from a Razorpay payout check.

1. The sample's two findings become two claims: the Rs 43.09 fee overcharge
   to Razorpay, resting on the rate card, and the Rs 10 short credit to the
   bank, resting on the data alone.
2. A finding in the merchant's favour is not a claim.
3. A payout missing from the statement is a claim to check before sending;
   lines that sum to more than was paid are a stated claim for the difference.
"""
from pathlib import Path

from fastapi.testclient import TestClient

import main
from recovery_claims import build_claims

SAMPLE = Path(__file__).resolve().parents[2] / "public" / "sample-data" / "razorpay"


def _result(checks, sid="setl_T1", amount=100_000):
    return {"settlement_id": sid, "utr": "UTR9", "amount_cents": amount, "currency": "INR",
            "settled_on": "2026-09-10", "checks": checks}


def test_the_samples_findings_become_two_claims():
    names = {"recon_file": "recon_combined.json", "settlements_file": "settlements.json",
             "bank_file": "bank_statement.csv", "ledger_file": "ledger.json"}
    files = {k: (v, (SAMPLE / v).read_bytes()) for k, v in names.items()}
    out = TestClient(main.app).post("/razorpay/reconcile/upload", files=files).json()
    rec = out["recovery"]
    got = {(c["kind"], c["to"], c["amount_cents"], c["basis"]) for c in rec["claims"]}
    assert got == {("fee_overcharge", "Razorpay Support", 4309, "estimated"),
                   ("short_credit", "Your bank", 1000, "stated")}
    assert rec["total_cents"] == 5309 and "Nothing has been sent" in rec["plain"]
    fee = next(c for c in rec["claims"] if c["kind"] == "fee_overcharge")
    assert fee["evidence_csv"].splitlines()[1].endswith(",43.09")
    assert "negotiated pricing" in fee["check_first"]


def test_a_finding_in_the_merchants_favour_is_not_a_claim():
    r = _result({"fees": {"summary": {"total_overcharge_cents": 0}, "findings": [
        {"category": "fee_undercharge", "txn_id": "pay_1", "difference_cents": -500,
         "expected_cents": 2000, "actual_cents": 1500}]}})
    assert build_claims({"results": [r]})["claims"] == []


def test_missing_payout_and_underpaid_settlement():
    missing = _result({"bank": {"verdict": "not_found"}}, sid="setl_M")
    under = _result({"tie_out": {"verdict": "does_not_tie_out", "residual_paise": 250,
                                 "members": 3, "sum_of_net_paise": 100_250,
                                 "settlement_amount_paise": 100_000}}, sid="setl_U")
    claims = {c["settlement_id"]: c for c in build_claims({"results": [missing, under]})["claims"]}
    assert claims["setl_M"]["kind"] == "payout_not_received" and claims["setl_M"]["check_first"]
    assert claims["setl_U"]["amount_cents"] == 250 and claims["setl_U"]["basis"] == "stated"
