"""
What a blind adversarial test written after the engine found (FAILURE_LOG
46-47), pinned so it stays found.

1. Every payment but one names the settlement, the one that belongs is
   missing from the feed, and an unrelated payment of exactly its amount
   carries no reference: the engine cleared the unrelated payment in. Now
   proposed, not cleared, and the payment is named.
2. A member that does not name the settlement but shares a reference with
   the ones that do still clears.
3. An exact re-export of a row is read once; a blank amount (read as 0) no
   longer makes a one-answer settlement ambiguous.
4. The settlement's own bank credit is not "unexplained", a gateway payment
   and its ERP line are not duplicates, and a reconcile run posts nothing.
"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import compliance_agent
import main
from cash_position import build_cash_position
from orchestrator import reconcile_batch
from schema import NormalizedTxn, SettlementBatch, SourceType, TzConfidence
from subset_sum import SubsetSumConfig

T0 = datetime(2026, 9, 17, 11, 30, tzinfo=timezone.utc)
CFG = SubsetSumConfig(num_search_workers=1)


def _tx(txn_id, ref, amount, *, source=SourceType.GATEWAY, hours=40, raw=None, memo=""):
    return NormalizedTxn(source=source, source_txn_id=txn_id,
                         ref_id_canonical="".join(ch for ch in ref if ch.isalnum()).upper(),
                         amount_cents=amount, currency="INR", timestamp_utc=T0 - timedelta(hours=hours),
                         tz_confidence=TzConfidence.HIGH, memo_raw=memo, memo_normalized=memo.lower(),
                         extra={"ref_raw": raw if raw is not None else ref})


def _batch(bid, net):
    return SettlementBatch(batch_id=bid, net_amount_cents=net, currency="INR", settled_at_utc=T0,
                           declared_deductions_cents=0, member_source=SourceType.GATEWAY)


def _named(stl, amounts):
    return [_tx(f"pay_n{i}", f"{stl}/leg{i}", a, hours=30 + i) for i, a in enumerate(amounts)]


def test_a_stranger_of_the_missing_members_amount_is_proposed_not_cleared():
    stl = "setl_Kp8Qm2Xw7Rt4Zb"
    named = _named(stl, [411_25, 1_208_40, 733_10])
    stranger = _tx("pay_stranger", "order_Ab9Xk2Lm7Qp3Rs", 926_62, hours=35)
    others = [_tx(f"pay_o{i}", f"setl_Other{i}Zz9", 500_00 + 37 * i, hours=40 + i) for i in range(12)]
    net = sum(t.amount_cents for t in named) + 926_62
    report = reconcile_batch(_batch(stl, net), named + [stranger] + others, subset_config=CFG)
    m = report.match_result
    assert not m.cleared
    assert m.withheld_reason == "unreferenced_member"
    assert m.unreferenced_txn_ids == ["pay_stranger"]
    assert report.summary()["unreferenced_members"] == ["pay_stranger"]


def test_a_member_sharing_a_reference_with_the_named_ones_still_clears():
    stl = "STL20260917A7"
    named = [_tx(f"pay_n{i}", f"{stl}-B55120-{i}", a, hours=30 + i)
             for i, a in enumerate([411_25, 1_208_40, 733_10])]
    kin = _tx("pay_kin", "B55120-9", 926_62, hours=33)
    others = [_tx(f"pay_o{i}", f"STL2026081{i}Q-C7{i}41-1", 500_00 + 37 * i, hours=40 + i) for i in range(12)]
    net = sum(t.amount_cents for t in named) + 926_62
    report = reconcile_batch(_batch(stl, net), named + [kin] + others, subset_config=CFG)
    assert report.match_result.cleared, report.match_result.reasoning
    assert sorted(report.match_result.matched_txn_ids) == ["pay_kin", "pay_n0", "pay_n1", "pay_n2"]


def _upload(csv_text, batch_id, net):
    return TestClient(main.app).post("/reconcile/upload", data={
        "batch_id": batch_id, "net_amount": net, "settled_at": "2026-09-17T11:30:00Z",
        "declared_deductions": "0", "member_source": "gateway",
    }, files={"gateway_file": ("g.csv", csv_text.encode())}).json()


def test_an_exact_re_export_of_a_row_is_read_once():
    rows = "\n".join(f"pay_dup{i},BLDUP77{i},{a},INR,2026-09-16T08:0{i}:00Z,captured"
                     for i, a in enumerate(["100.00", "250.50", "75.25"]))
    body = "txn_id,ref_id,amount,currency,timestamp,status\n" + rows + "\n" + rows.splitlines()[1] + "\n"
    j = _upload(body.replace("BLDUP77", "BLDUP77-"), "BLDUP77", "425.75")
    assert j["summary"]["cleared"], j["plain_summary"]
    assert any("read once" in n for n in j["ingestion_notes"])


def test_a_blank_amount_does_not_make_one_answer_ambiguous():
    body = ("txn_id,ref_id,amount,currency,timestamp,status\n"
            "pay_bz0,BLZERO88-1,100.00,INR,2026-09-16T08:00:00Z,captured\n"
            "pay_bz1,BLZERO88-2,200.00,INR,2026-09-16T08:01:00Z,captured\n"
            "pay_bz2,BLZERO88-3,,INR,2026-09-16T08:02:00Z,captured\n")
    j = _upload(body, "BLZERO88", "300.00")
    assert j["summary"]["cleared"], j["plain_summary"]
    assert sorted(j["matched_txn_ids"]) == ["pay_bz0", "pay_bz1"]


def test_the_settlements_own_credit_is_not_unexplained():
    stl = "SETTLE-777"
    members = _named(stl, [100_00, 200_00])
    credit = _tx("UTR777", "UTR-SETTLE-777", 300_00, source=SourceType.BANK, hours=1)
    stray = _tx("UTR778", "UTR-OTHER", 55_00, source=SourceType.BANK, hours=2)
    pos = build_cash_position(_batch(stl, 300_00), members + [credit, stray],
                              matched_txn_ids=["pay_n0", "pay_n1"], cleared=True)
    unexplained = next(b for b in pos.buckets if b.key == "bank_unexplained")
    assert (unexplained.count, unexplained.amount_cents) == (1, 55_00)
    assert any("UTR777" in n for n in pos.notes)


def test_a_payment_and_its_ledger_line_are_not_duplicates():
    pay = _tx("pay_1", "R1", 999_00)
    ledger = _tx("JV1", "R1", 999_00, source=SourceType.ERP)
    compliance_agent.check_duplicates([pay, ledger])
    assert not [f for f in pay.compliance_findings + ledger.compliance_findings
                if f.rule_id == "DUPLICATE_TX"]
    again = _tx("pay_1b", "R1", 999_00)
    compliance_agent.check_duplicates([pay, again])
    assert any(f.rule_id == "DUPLICATE_TX" for f in again.compliance_findings)


def test_a_reconcile_run_posts_nothing_and_the_journal_awaits_approval():
    body = ("txn_id,ref_id,amount,currency,timestamp,status\n"
            "pay_j0,JRNL55-1,100.00,INR,2026-09-16T08:00:00Z,captured\n"
            "pay_j1,JRNL55-2,200.00,INR,2026-09-16T08:01:00Z,captured\n")
    j = _upload(body, "JRNL55", "300.00")
    assert j["summary"]["cleared"]
    assert j["cash_position"]["journal"]["status"] == "proposed"


def test_rows_that_differ_only_in_an_unmapped_column_are_not_merged():
    # The mapper picks bank_ref_num (the same on every row) as the id; the UTR
    # is what tells the ten payments apart, and merging on mapped fields read
    # them as one.
    body = "utr,bank_ref_num,credit,currency,value_date,remitter\n" + "".join(
        f"UTRBLM{i:05d},BLMRG9,1000.00,INR,2026-09-16,ACME LTD\n" for i in range(10))
    j = TestClient(main.app).post("/reconcile/upload", data={
        "batch_id": "BLMRG9", "net_amount": "9700.00", "declared_deductions": "300.00",
        "settled_at": "2026-09-17T11:30:00Z",
    }, files={"gateway_file": ("g.csv", body.encode())}).json()
    assert j["summary"]["cleared"], j["plain_summary"]
    assert j["summary"]["matched_count"] == 10
    assert not any("read once" in n for n in j["ingestion_notes"])
