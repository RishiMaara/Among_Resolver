"""
The investigator: for a settlement the engine would not clear, propose what to do.

WHAT IT IS
----------
A withheld settlement lands on someone's desk with a question: which of these
sets is right, or is the answer "wait", "ask the gateway", "write off the
rounding", or "escalate"? This module prepares the case the way a senior
reconciler would — every read-only fact the engine already has — and
proposes ONE typed action:

    MATCH_PROPOSAL        these transactions are the settlement
    WAIT_FOR_DATA         the missing piece is in transit; re-run after a date
    REQUEST_SOURCE        ask the gateway, bank or ledger owner for a document
    WRITE_OFF_ROUNDING    the residual is rounding (at most Rs 1)
    ESCALATE              a person must decide; here is why

Two proposers make the same kind of proposal from the same case. `propose_rules`
is fixed logic. `propose_model` hands the case to a language model, which can
weigh what rules cannot — memo text, which set's dates cohere, which
reference fragment survives — and must say why.

THE TOOLS ARE RUN FOR IT, AND THEY ONLY READ
--------------------------------------------
There is no loop in which the model calls tools and acts on the results: every
read-only tool — the recorded result, the pool, alternative sets that also
sum to the target (CP-SAT, bounded), exceptions by category, the due date on
the working-day calendar — is run first, and the model is shown the case.
Nothing it returns can change a record. That is a deliberate design for money:
an agent that can act is an agent whose mistakes land in the books.

EVERY PROPOSAL IS VERIFIED BEFORE ANYONE SEES IT
-----------------------------------------------
`verify` checks the proposal in code, whoever made it: a match must name only
transactions in this settlement's pool, once each, in its currency, none
already paid out by another settlement, summing to the target within the
engine's own tolerance — not tighter, which rejected true sets; a
wait must name a working day after the settlement and within ten working
days; a write-off must be the actual residual and at most Rs 1; a request
must name a party that can answer it. The reason must pass the grounding
check — every figure and id it cites must be in the case. A proposal that
fails is shown as REJECTED with the failed checks, never as advice.

And a verified proposal is still a proposal. Accepting a match goes through
the reviewer decision endpoint, where separation of duties applies.

`scripts/investigation_eval.py` measures both proposers on benchmark
settlements the engine withheld, with the verifier on and off.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import copy
import re

import grounding_check
import india_calendar
import llm_provider
import settled_ledger
from linkage import build_candidate_links, txn_key
from schema import SourceType

logger = logging.getLogger(__name__)

ACTIONS = ("MATCH_PROPOSAL", "WAIT_FOR_DATA", "REQUEST_SOURCE", "WRITE_OFF_ROUNDING", "ESCALATE")
PARTIES = ("gateway", "bank", "ledger owner", "customer")
MAX_WRITE_OFF_CENTS = 100
MAX_WAIT_WORKING_DAYS = 10
MAX_ALTERNATIVES = 3


@dataclass
class Proposal:
    action: str
    txn_ids: list[str] = field(default_factory=list)
    until_date: str = ""
    request: str = ""
    party: str = ""
    amount_cents: int = 0
    reason: str = ""
    proposer: str = "rules"


# ── the case: every read-only fact, gathered once ─────────────────────────

def build_case(batch, candidates: list, report, window_days: int = 5,
               alt_time_limit_s: float = 2.0, tolerance_cents: int | None = None,
               workers: int | None = None) -> dict:
    """What the engine knows about a withheld settlement, as one document."""
    from subset_sum import SubsetSumConfig, _solve_cpsat  # pylint: disable=import-outside-toplevel
    # The verifier holds a proposal to the arithmetic the engine holds itself
    # to. Exact-to-the-paisa was tried first and rejected the TRUE set in 4 of
    # 38 benchmark cases, where the target is rebuilt from an estimated fee and
    # lands a paisa off the members' sum.
    tol = SubsetSumConfig().tolerance_cents if tolerance_cents is None else tolerance_cents
    # Every core in production; a measurement pins one for reproducibility.
    workers = SubsetSumConfig().num_search_workers if workers is None else workers
    # A settlement's members are the processor's records, so the member feed
    # is the declared one or, undeclared, the gateway — stated in the case as
    # an assumption. The engine's own search spans every feed when none is
    # declared, and its proposal used to reach this case with its other-feed
    # records silently dropped: the demo's withheld preset showed 4 of 13
    # records, and the verifier rejected it for a sum those missing records
    # explained. Those records are now shown, with their feed, and the
    # verifier names them. See FAILURE_LOG 30.
    member_feed = batch.member_source or SourceType.GATEWAY
    window = timedelta(days=window_days)
    in_window = [t for t in candidates if abs(t.timestamp_utc - batch.settled_at_utc) <= window]
    pool = [t for t in in_window if t.source == member_feed]
    by_id: dict = {}
    for t in pool:
        by_id.setdefault(t.source_txn_id, t)
    m = report.match_result
    target = m.target_cents
    # What the engine proposed, from whichever feed; a member-feed record
    # wins an id collision.
    elsewhere = {t.source_txn_id: t for t in in_window
                 if t.source != member_feed and t.source_txn_id in set(m.matched_txn_ids)}
    known = {**elsewhere, **by_id}
    engine_ids = [i for i in m.matched_txn_ids if i in known]

    # Sets that also reach the target. Computed even when the engine proposed
    # nothing: an empty proposal is where a choice most needs options.
    alternatives: list[list[str]] = []
    if len(pool) <= 800:
        # The engine's set is excluded only if it lies in this pool; one that
        # reached into another feed is not a solution here to begin with.
        in_pool = engine_ids and all(i in by_id for i in engine_ids)
        forbidden = [{txn_key(by_id[i]) for i in engine_ids}] if in_pool else []
        for _ in range(MAX_ALTERNATIVES):
            alt = _solve_cpsat(pool, target, tol, alt_time_limit_s,
                               forbidden_solutions=forbidden or None, num_search_workers=workers)
            if alt is None:
                break
            ids = sorted(t.source_txn_id for t in alt[0])
            alternatives.append(ids)
            forbidden.append({txn_key(t) for t in alt[0]})

    # Whether each record's reference names this settlement, as the engine's
    # linkage judged it — the evidence in the reference, without its text.
    try:
        named = build_candidate_links(batch, list(known.values()), window_days).anchor_keys
    except Exception:  # pragma: no cover - a case must build even if linkage fails
        named = set()

    def describe(ids):
        return [{"id": i, "amount_cents": known[i].amount_cents,
                 "date": known[i].timestamp_utc.date().isoformat(),
                 "feed": known[i].source.value,
                 "names_settlement": txn_key(known[i]) in named,
                 "ref": known[i].ref_id_canonical, "memo": known[i].memo_raw[:60]}
                for i in ids if i in known]

    settled_on = batch.settled_at_utc.date()
    return {
        "batch_id": batch.batch_id,
        "currency": batch.currency,
        "target_cents": target,
        "tolerance_cents": tol,
        "settled_on": settled_on.isoformat(),
        "withheld_reason": m.withheld_reason or ("unmatched" if not m.matched_txn_ids else "ambiguous"),
        "member_feed": member_feed.value,
        "member_feed_declared": batch.member_source is not None,
        "confidence": m.confidence,
        "residual_cents": target - m.matched_sum_cents if m.matched_txn_ids else target,
        "engine_proposal": describe(sorted(engine_ids)),
        # Each set's total, stated. A reviewer quotes these, and so does the
        # model; left out, a reason citing a set's correct total failed the
        # grounding check as if the figure were invented — 18 sound proposals
        # were rejected that way on the first measured run.
        "engine_proposal_sum_cents": sum(known[i].amount_cents for i in engine_ids),
        "alternatives": [describe(a) for a in alternatives],
        "alternative_sums_cents": [sum(by_id[i].amount_cents for i in a) for a in alternatives],
        "pool_size": len(pool),
        "exceptions": [{"reason": e.reason.value, "ids": e.candidate_txn_ids[:5],
                        "note": e.diagnosis_note[:160]} for e in report.exceptions[:12]],
        "next_working_day": india_calendar.add_working_days(settled_on, 1).isoformat(),
        "_pool": {i: {"amount_cents": t.amount_cents, "currency": t.currency,
                      "named": txn_key(t) in named, "feed": t.source.value,
                      "ref": t.ref_id_canonical or ""}
                  for i, t in known.items()},
        "_sets": [sorted(engine_ids)] + [list(a) for a in alternatives],
    }


def _public(case: dict) -> dict:
    return {k: v for k, v in case.items() if not k.startswith("_")}


# ── the proposers ─────────────────────────────────────────────────────────

def propose_rules(case: dict) -> Proposal:
    """The fixed-logic baseline."""
    reason = case["withheld_reason"]
    if case["tolerance_cents"] < abs(case["residual_cents"]) <= MAX_WRITE_OFF_CENTS \
            and case["engine_proposal"]:
        return Proposal("WRITE_OFF_ROUNDING", amount_cents=case["residual_cents"],
                        reason=f"Residual of {case['residual_cents']} paise is rounding.")
    if case["engine_proposal"]:
        n = len(case["alternatives"])
        return Proposal("MATCH_PROPOSAL", txn_ids=[r["id"] for r in case["engine_proposal"]],
                        reason=(f"The engine's own set sums to the target; {n} other set(s) "
                                f"also do, so a person should confirm."))
    if any(e["reason"] == "timing_lag" for e in case["exceptions"]):
        return Proposal("WAIT_FOR_DATA", until_date=case["next_working_day"],
                        reason="A counterpart looks in transit; re-run after the next working day.")
    if reason == "unmatched":
        return Proposal("REQUEST_SOURCE", party="gateway",
                        request="the settlement report listing this payout's transactions",
                        reason="Nothing in the pool sums to the payout.")
    return Proposal("ESCALATE", reason=f"Withheld as {reason}; no rule applies.")


_SYSTEM = (
    "You are a senior payments reconciler. A settlement was withheld from "
    "auto-clearing. From the case below, propose exactly one action: "
    "MATCH_PROPOSAL (list the transaction ids that make up the settlement), "
    "WAIT_FOR_DATA (until_date, YYYY-MM-DD), REQUEST_SOURCE (party: gateway, bank, "
    "ledger owner or customer; and what to request), WRITE_OFF_ROUNDING (amount_cents, "
    "only for a residual of at most 100 paise) or ESCALATE. The engine's own set and "
    "alternative sets all sum to the target; prefer the set whose references, memos "
    "and dates cohere as one payout. Each record names its feed, and one payment can "
    "appear once in each feed: a set must not count the same payment twice. If none "
    "is clearly better, ESCALATE. Give a "
    "one-sentence reason. Cite ids and amounts exactly as the case lists them; do not "
    "state totals or differences you computed yourself — every figure in the reason "
    "is checked against the case, and a reason with a figure not in it is rejected."
)
_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "action": {"type": "STRING", "enum": list(ACTIONS)},
        "txn_ids": {"type": "ARRAY", "items": {"type": "STRING"}},
        "until_date": {"type": "STRING", "nullable": True},
        "party": {"type": "STRING", "nullable": True},
        "request": {"type": "STRING", "nullable": True},
        "amount_cents": {"type": "INTEGER", "nullable": True},
        "reason": {"type": "STRING"},
    },
    "required": ["action", "reason"],
}


def _aliased(case: dict, redact_text: bool) -> tuple[dict, dict[str, str]]:
    """
    The case as the model sees it: ids and the settlement's name replaced by
    opaque aliases, and — for measurement — reference and memo text removed.

    Aliases because an identifier can carry meaning it should not: the
    benchmark names its members S19_TRUE_0 and its settlements after the
    failure they simulate, and the first measured run showed the model
    reading the answer off the labels. Production ids are opaque anyway.

    redact_text for the evaluation only: the benchmark's memos say "decoy"
    and "unrelated payment", which no real bank narration does. In production
    the text is real evidence and the model sees it.
    """
    pub = copy.deepcopy(_public(case))
    alias: dict[str, str] = {}

    def a(real: str) -> str:
        if real not in alias:
            alias[real] = f"tx{len(alias) + 1:03d}"
        return alias[real]

    for group in [pub["engine_proposal"]] + pub["alternatives"]:
        for row in group:
            row["id"] = a(row["id"])
            if redact_text:
                row.pop("ref", None)
                row.pop("memo", None)
    for e in pub["exceptions"]:
        e["ids"] = [a(i) for i in e["ids"]]
        if redact_text:
            e.pop("note", None)
    pub["batch_id"] = "SETTLEMENT"
    return pub, {v: k for k, v in alias.items()}


def propose_model(case: dict, redact_text: bool = False) -> Proposal | None:
    """The model's proposal, or None when no model is available or it fails."""
    if not llm_provider.is_configured():
        return None
    shown, back = _aliased(case, redact_text)
    raw = llm_provider.generate(json.dumps(shown, default=str), system=_SYSTEM,
                                schema=_SCHEMA, max_output_tokens=1500)
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if d.get("action") not in ACTIONS:
        return None
    # Aliases back to real ids. An alias the model invented maps to nothing
    # and stays as it is, so the verifier rejects it as not in the pool.
    reason = re.sub(r"\btx\d{3}\b", lambda m: back.get(m.group(0), m.group(0)),
                    str(d.get("reason") or ""))
    reason = reason.replace("SETTLEMENT", case["batch_id"])
    return Proposal(action=d["action"],
                    txn_ids=[back.get(str(x), str(x)) for x in d.get("txn_ids") or []],
                    until_date=str(d.get("until_date") or ""), request=str(d.get("request") or ""),
                    party=str(d.get("party") or "").lower(),
                    amount_cents=int(d.get("amount_cents") or 0),
                    reason=reason, proposer="model")


# ── the verifier ──────────────────────────────────────────────────────────

def verify(p: Proposal, case: dict) -> dict:
    """Check a proposal in code. Returns {valid, failed: [...], plain}."""
    failed: list[str] = []
    pool = case["_pool"]
    if p.action not in ACTIONS:
        failed.append(f"unknown action {p.action!r}")
    elif p.action == "MATCH_PROPOSAL":
        ids = p.txn_ids
        if not ids:
            failed.append("a match must name transactions")
        if len(set(ids)) != len(ids):
            failed.append("a transaction is named twice")
        unknown = [i for i in ids if i not in pool]
        if unknown:
            failed.append(f"{len(unknown)} id(s) are not in this settlement's pool: "
                          + ", ".join(unknown[:3]))
        known = [i for i in set(ids) if i in pool]
        if any(pool[i]["currency"] != case["currency"] for i in known):
            failed.append("a transaction is in another currency")
        # One payment, two feeds. A gateway capture and its ledger booking
        # carry the same reference and amount, and a set holding both counts
        # that payment twice yet can still reach the target: the demo's
        # withheld preset does exactly that, with four orders doubled, and
        # this verifier passed it as consistent until FAILURE_LOG 30.
        first: dict = {}
        doubled: list[tuple[str, str]] = []
        for i in sorted(known):
            rec = pool[i]
            if not rec.get("ref"):
                continue
            k = (rec["ref"], rec["amount_cents"])
            if k in first and pool[first[k]].get("feed") != rec.get("feed"):
                doubled.append((first[k], i))
            first.setdefault(k, i)
        if doubled:
            failed.append(f"{len(doubled)} payment(s) counted twice, once in each feed: "
                          + ", ".join(f"{a} and {b}" for a, b in doubled[:3]))
        feed = case.get("member_feed")
        outside = sorted(i for i in known if feed and pool[i].get("feed") != feed)
        if outside:
            failed.append(
                f"{len(outside)} record(s) are not from the {feed} feed, where this "
                f"settlement's payments are"
                + ("" if case.get("member_feed_declared") else
                   " (assumed: no member feed was declared — declare it and re-run)")
                + ": " + ", ".join(outside[:3]))
        total = sum(pool[i]["amount_cents"] for i in known)
        if not unknown and abs(total - case["target_cents"]) > case["tolerance_cents"]:
            failed.append(f"the set sums to {total} paise, {total - case['target_cents']:+d} "
                          f"from the target, beyond the engine's tolerance of "
                          f"{case['tolerance_cents']}")
        taken = {i: b for i, b in settled_ledger.owners(known).items() if b != case["batch_id"]}
        if taken:
            failed.append(f"{len(taken)} transaction(s) already paid out by another settlement")
        # An arithmetic tie is not evidence. If another listed set reaches the
        # same target, the chosen one must carry more evidence than each rival
        # — more records whose reference names the settlement — or picking it
        # is a guess dressed as a finding. Measured: without this rule, 11
        # arithmetically valid WRONG sets reached reviewers from 58 cases.
        chosen = set(ids)
        rivals = [set(r) for r in case.get("_sets", []) if r and set(r) != chosen]
        if not failed and rivals:
            def named(group):
                return sum(1 for i in group if pool.get(i, {}).get("named"))
            best_rival = max(named(r) for r in rivals)
            if named(chosen) <= best_rival:
                failed.append(
                    f"{len(rivals)} other set(s) reach the same target and this one has no "
                    f"more evidence than the best of them ({named(chosen)} vs {best_rival} "
                    f"record(s) whose reference names the settlement); escalate with the "
                    f"sets listed rather than choose between equals")
    elif p.action == "WAIT_FOR_DATA":
        try:
            until = date.fromisoformat(p.until_date)
            settled = date.fromisoformat(case["settled_on"])
            if until <= settled:
                failed.append("the wait ends before the settlement date")
            elif india_calendar.working_days_between(settled, until) > MAX_WAIT_WORKING_DAYS:
                failed.append(f"the wait is longer than {MAX_WAIT_WORKING_DAYS} working days")
            elif not india_calendar.is_working_day(until):
                failed.append(f"{until.isoformat()} is not a working day "
                              f"({india_calendar.closed_because(until)})")
        except ValueError:
            failed.append(f"until_date {p.until_date!r} is not a date")
    elif p.action == "REQUEST_SOURCE":
        if p.party not in PARTIES:
            failed.append(f"party {p.party!r} is not one who can answer (gateway, bank, ledger owner, customer)")
        if not p.request.strip():
            failed.append("the request does not say what to ask for")
    elif p.action == "WRITE_OFF_ROUNDING":
        if p.amount_cents != case["residual_cents"]:
            failed.append(f"the write-off {p.amount_cents} is not the residual {case['residual_cents']}")
        if abs(p.amount_cents) > MAX_WRITE_OFF_CENTS or p.amount_cents == 0:
            failed.append("a rounding write-off is between 1 and 100 paise")

    grounding = grounding_check.verify(p.reason, _public(case))
    if not grounding.ok:
        failed.append("the reason cites figures or ids not in the case: "
                      + ", ".join((grounding.ungrounded_figures + grounding.ungrounded_ids)[:4]))

    valid = not failed
    return {"valid": valid, "failed": failed,
            "plain": ("Checked: this proposal is consistent with the data. It is still a "
                      "proposal; accepting it is a reviewer's decision." if valid else
                      "REJECTED before reaching a reviewer: " + "; ".join(failed) + ".")}


def investigate(batch, candidates: list, report, use_model: bool = False) -> dict | None:
    """Case, proposal and verdict for a settlement that did not clear."""
    if report.match_result.cleared:
        return None
    case = build_case(batch, candidates, report)
    proposal = None
    if use_model:
        proposal = propose_model(case)
    if proposal is None:
        proposal = propose_rules(case)
    verdict = verify(proposal, case)
    return {"case": _public(case), "proposal": proposal.__dict__, "verification": verdict}
