"""
Razorpay-native reconciliation: Razorpay names the members; the engine
checks what that naming does not prove.

  1. Arithmetic: sum(credit - debit) over members equals the payout.
  2. A blind second opinion: re-solved with settlement ids stripped, timed by
     creation, using only the cycle learned from OTHER settlements.
  3. The bank: the settlement's UTR and amount among the bank credits.
  4. The books: every payment in the merchant's ledger.
  5. Fees and tax: Razorpay's fee includes GST; rate, GST, TDS, TCS by date.

VERIFIED when the arithmetic closes and nothing checked disagrees. Built to
Razorpay's published contract; not yet run against a live account.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone

import razorpay_source as rz
import settlement_cycle
from fee_audit import run_fee_audit
from fee_decomposition import FeeRateCard
from india_tax import ist_date
from schema import NormalizedTxn, SourceType

logger = logging.getLogger(__name__)

NET_TO_NET = FeeRateCard(gateway_fee_bps=0, flat_fee_cents=0, tax_withholding_bps=0)


def _blind(txn: NormalizedTxn, item: dict) -> NormalizedTxn:
    """The same line with its settlement id gone and its capture time as the time."""
    created = item.get("created_at")
    ts = (datetime.fromtimestamp(int(created), tz=timezone.utc) if created
          else txn.timestamp_utc)
    order = str(item.get("order_id") or "")
    return replace(txn, ref_id_canonical=(order or txn.source_txn_id).upper().replace("_", ""),
                   timestamp_utc=ts, extra={k: v for k, v in txn.extra.items()
                                            if k not in ("settlement_id", "settlement_utr",
                                                         "ref_raw")})


def _lags(settlement: dict, items: list[dict]) -> list[int]:
    """Capture-to-payout lag in days for one settlement's payments."""
    paid = datetime.fromtimestamp(int(settlement.get("created_at") or 0), tz=timezone.utc).date()
    out = []
    for i in rz.members_of(str(settlement.get("id") or ""), items):
        if i.get("type") == "payment" and i.get("created_at"):
            out.append((paid - datetime.fromtimestamp(int(i["created_at"]), tz=timezone.utc).date()).days)
    return out


def blind_check(settlement: dict, items: list[dict], window_days: int = 7,
                others: list[dict] | None = None) -> dict:
    """Re-solve one settlement without the ids that name its members."""
    from pipeline import reconcile_settlement  # pylint: disable=import-outside-toplevel
    pool = []
    for i in items:
        t = rz.recon_item_to_txn(i)
        if t is not None:
            pool.append(_blind(t, i))
    truth = {i["entity_id"] for i in rz.members_of(str(settlement.get("id") or ""), items)}
    batch = rz.settlement_to_batch(settlement)
    batch = replace(batch, batch_id=f"{batch.batch_id}:blind")
    other_lags = [d for o in (others or []) for d in _lags(o, items)]
    cycle = settlement_cycle.profile_from(other_lags, len(others or []))
    try:
        with settlement_cycle.using(cycle):
            report = reconcile_settlement(batch, pool, settlement_window_days=window_days,
                                          rate_card=NET_TO_NET)
    except Exception as exc:  # pragma: no cover - the check must not fail the run
        return {"verdict": "error", "plain": f"The blind re-solve failed: {type(exc).__name__}."}
    m = report.match_result
    got = set(m.matched_txn_ids)
    if got == truth and m.cleared:
        verdict, plain = "agrees", (
            f"Solved blind — no settlement ids, capture times only — the engine "
            f"cleared the same {len(got)} line(s) Razorpay lists.")
    elif got == truth:
        verdict, plain = "agrees_as_proposal", (
            f"Solved blind, the engine proposed exactly Razorpay's {len(got)} line(s) "
            f"but would not clear them on its own evidence (confidence "
            f"{m.confidence:.2f}).")
    elif m.cleared:
        verdict, plain = "disagrees", (
            f"Solved blind, the engine CLEARED a different set: {len(got - truth)} "
            f"line(s) Razorpay does not list, {len(truth - got)} it does that the "
            f"engine left out. One of the two is wrong; look at those lines.")
    else:
        verdict, plain = "undecided", (
            "Solved blind, the engine could not single out one set — normal when "
            "many lines could sum to the same payout. Razorpay's list stands; this "
            "check neither confirms nor contradicts it.")
    return {"verdict": verdict, "plain": plain, "engine_set_size": len(got),
            "razorpay_set_size": len(truth), "confidence": m.confidence,
            "cycle_learned_from_other_settlements": len(others or []) if cycle else 0,
            "only_engine": sorted(got - truth)[:20], "only_razorpay": sorted(truth - got)[:20]}


def bank_check(settlement: dict, bank: list[NormalizedTxn]) -> dict:
    """Did the payout arrive: the settlement's UTR, and its amount, among bank credits."""
    utr = str(settlement.get("utr") or "").strip().upper()
    amount = int(settlement.get("amount") or 0)

    def mentions_utr(t: NormalizedTxn) -> bool:
        hay = f"{t.source_txn_id} {t.ref_id_canonical} {t.memo_raw}".upper().replace(" ", "")
        return bool(utr) and utr.replace(" ", "") in hay

    by_utr = [t for t in bank if mentions_utr(t)]
    exact = [t for t in by_utr if t.amount_cents == amount]
    if exact:
        t = exact[0]
        return {"verdict": "arrived", "bank_txn_id": t.source_txn_id,
                "plain": f"Bank credit {t.source_txn_id} carries UTR {utr} and the exact amount."}
    if by_utr:
        t = by_utr[0]
        diff = t.amount_cents - amount
        side = "less" if diff < 0 else "more"
        return {"verdict": "amount_differs", "bank_txn_id": t.source_txn_id,
                "difference_cents": diff,
                "plain": (f"Bank credit {t.source_txn_id} carries UTR {utr} but is "
                          f"₹{abs(diff) / 100:,.2f} {side} than the settlement. The bank "
                          f"and Razorpay disagree on what was paid.")}
    same_amount = [t for t in bank if t.amount_cents == amount]
    if same_amount:
        return {"verdict": "amount_only", "bank_txn_id": same_amount[0].source_txn_id,
                "plain": (f"A bank credit of the exact amount exists, but no credit "
                          f"mentions UTR {utr or '(none on the settlement)'}. Probably "
                          f"this payout; the UTR would make it certain.")}
    return {"verdict": "not_found",
            "plain": (f"No bank credit carries UTR {utr or '(none)'} or the amount "
                      f"₹{amount / 100:,.2f}. Either the statement does not cover the "
                      f"payout date, or the money has not arrived.")}


def books_check(members: list[dict], ledger: list[NormalizedTxn]) -> dict:
    """Is every payment in this settlement in the merchant's books?"""
    keys = set()
    for t in ledger:
        for v in (t.source_txn_id, t.ref_id_canonical, t.memo_raw):
            for tok in str(v or "").replace(",", " ").split():
                keys.add(tok.upper().replace("_", ""))
            keys.add(str(v or "").upper().replace("_", ""))
    missing = []
    for i in members:
        if i.get("type") != "payment":
            continue
        ids = [str(i.get(k) or "").upper().replace("_", "")
               for k in ("order_id", "entity_id", "payment_id", "order_receipt")]
        if not any(x and x in keys for x in ids):
            missing.append(i.get("entity_id"))
    if missing:
        return {"verdict": "missing_in_books", "missing": missing[:50],
                "plain": (f"{len(missing)} payment(s) Razorpay settled are not in the "
                          f"ledger by order or payment id: {', '.join(missing[:5])}"
                          f"{'…' if len(missing) > 5 else ''}. Revenue not yet booked, "
                          f"or booked under another reference.")}
    return {"verdict": "all_booked", "missing": [],
            "plain": "Every payment in this settlement is in the ledger."}


def reconcile(settlements: list[dict], recon: list[dict],
              bank: list[NormalizedTxn] | None = None,
              ledger: list[NormalizedTxn] | None = None,
              blind: bool = True) -> dict:
    """Every settlement, checked five ways. See the module docstring."""
    results = []
    for s in settlements:
        sid = str(s.get("id") or "")
        members = rz.members_of(sid, recon)
        tie = rz.verify_tie_out(s, recon)
        member_txns = [t for t in (rz.recon_item_to_txn(i) for i in members) if t]
        settled_on = datetime.fromtimestamp(int(s.get("created_at") or 0), tz=timezone.utc)
        fee_findings, fee_summary = run_fee_audit(member_txns, as_of=ist_date(settled_on))

        checks = {"tie_out": {
            "verdict": "ties_out" if tie["ties_out"] else "does_not_tie_out",
            "plain": (f"{tie['members']} line(s) sum to ₹{tie['sum_of_net_paise'] / 100:,.2f}, "
                      f"the settlement is ₹{tie['settlement_amount_paise'] / 100:,.2f}"
                      + ("." if tie["ties_out"] else
                         f" — off by ₹{tie['residual_paise'] / 100:,.2f}.")),
            **tie}}
        if s.get("_derived"):
            # No settlements list: the amount IS the lines' sum, so a tie-out
            # would be a check of the report against itself.
            checks["tie_out"] = {
                **tie, "verdict": "derived",
                "plain": ("No settlements list was given, so this payout's amount is the "
                          "sum of its own lines in the report; they tie out by construction. "
                          "The bank credit is the independent check.")}
        if blind and members:
            others = [o for o in settlements if o is not s and rz.members_of(str(o.get("id") or ""), recon)]
            checks["blind_solve"] = blind_check(s, recon, others=others)
        if bank is not None:
            checks["bank"] = bank_check(s, bank)
        if ledger is not None:
            checks["books"] = books_check(members, ledger)
        checks["fees"] = {
            "verdict": "findings" if fee_findings else "clean",
            "summary": fee_summary,
            "findings": [{"category": f.category.value, "severity": f.severity.value,
                          "txn_id": f.txn_id, "difference_cents": f.difference_cents,
                          "citation": f.citation, "description": f.description}
                         for f in fee_findings[:20]],
        }

        bad = {"does_not_tie_out", "disagrees", "amount_differs", "not_found"}
        soft = {"missing_in_books", "amount_only", "findings", "derived"}
        verdicts = {k: v["verdict"] for k, v in checks.items()}
        if verdicts.get("tie_out") == "derived" and verdicts.get("bank") == "arrived":
            verdicts.pop("tie_out")      # the bank credit proved the amount instead
        if not members:
            status = "no_lines"
        elif any(v in bad for v in verdicts.values()):
            status = "not_verified"
        elif any(v in soft for v in verdicts.values()):
            status = "verified_with_findings"
        else:
            status = "verified"
        results.append({
            "settlement_id": sid, "utr": s.get("utr"), "amount_cents": int(s.get("amount") or 0),
            "currency": s.get("currency") or "INR", "settled_on": settled_on.date().isoformat(),
            "members": len(members),
            "by_type": {t: sum(1 for i in members if i.get("type") == t)
                        for t in sorted({i.get("type") for i in members})},
            "status": status, "checks": checks,
        })

    # Razorpay's own membership is the best evidence of its payout cycle there
    # is. Taught to the store only for settlements that tie out, so a later
    # reconciliation from a bank file with no references can use it.
    for s, r in zip(settlements, results):
        if r["checks"]["tie_out"]["verdict"] == "ties_out":
            paid = datetime.fromtimestamp(int(s.get("created_at") or 0), tz=timezone.utc)
            times = [datetime.fromtimestamp(int(i["created_at"]), tz=timezone.utc)
                     for i in rz.members_of(str(s.get("id") or ""), recon)
                     if i.get("type") == "payment" and i.get("created_at")]
            settlement_cycle.record_clear(f"razorpay:{s.get('id')}", "gateway",
                                          s.get("currency") or "INR", paid, times)

    unsettled = [i for i in recon if not i.get("settlement_id")]
    tally = {k: sum(1 for r in results if r["status"] == k)
             for k in ("verified", "verified_with_findings", "not_verified", "no_lines")}
    return {
        "source": "razorpay_settlement_recon",
        "settlements": len(results),
        "tally": tally,
        "unsettled_lines": {
            "count": len(unsettled),
            "value_cents": sum(int(i.get("credit") or 0) - int(i.get("debit") or 0) for i in unsettled),
            "plain": (f"{len(unsettled)} line(s) in this period name no settlement yet — "
                      f"on hold, or waiting for the next payout." if unsettled else ""),
        },
        "results": results,
    }
