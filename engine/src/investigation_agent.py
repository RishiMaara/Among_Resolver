"""
The investigator: for a settlement the engine would not clear, propose one
typed action.

    MATCH_PROPOSAL      these transactions are the settlement
    WAIT_FOR_DATA       the missing piece is in transit; re-run after a date
    REQUEST_SOURCE      ask the gateway, bank or ledger owner for a document
    WRITE_OFF_ROUNDING  the residual is rounding (at most Rs 1)
    ESCALATE            a person must decide; here is why

Every read-only tool (recorded result, pool, alternative sets, exceptions,
working-day due date) is run first and the proposer sees the case; nothing
it returns can change a record. `propose_rules` is fixed logic;
`propose_model` asks a model. `verify` checks every proposal in code (pool,
currency, feed, double counts, already paid out, sum within tolerance,
calendar, write-off size, grounded reason); a failure is shown REJECTED,
never as advice. A verified proposal is still only a proposal.
scripts/investigation_eval.py measures both proposers.
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
MAX_ANCHORED_ALTERNATIVES = 2


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

    # Sets that keep every record naming the settlement. An anchor is
    # evidence, and a set that leaves one out contradicts it — yet the sets
    # above are whatever the solver reached first, and on the benchmark's 38
    # solvable withheld cases the true set was among them in only 5. So the
    # case also lists up to two sets with every anchor forced in.
    anchored = {txn_key(t) for t in pool if txn_key(t) in named}
    if anchored and len(pool) <= 800:
        seen = [set(a) for a in alternatives] + ([set(engine_ids)] if engine_ids else [])
        forbid = [{txn_key(by_id[i]) for i in a if i in by_id} for a in seen]
        for _ in range(MAX_ANCHORED_ALTERNATIVES):
            alt = _solve_cpsat(pool, target, tol, alt_time_limit_s,
                               forbidden_solutions=[f for f in forbid if f] or None,
                               forced_ids=anchored, num_search_workers=workers)
            if alt is None:
                break
            ids = sorted(t.source_txn_id for t in alt[0])
            if set(ids) not in seen:
                alternatives.insert(0, ids)
                seen.append(set(ids))
            forbid.append({txn_key(t) for t in alt[0]})

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

def _best_on_evidence(case: dict) -> tuple[list[str] | None, int, int]:
    """
    The one listed set, inside the member feed, carrying strictly the most
    records that name the settlement — or None when no set leads. Returns
    (ids, its evidence, how many sets were compared).
    """
    feed = case.get("member_feed")
    listed = [case["engine_proposal"]] + list(case["alternatives"])
    admissible: dict[frozenset, int] = {}
    for rows in listed:
        if rows and all(not feed or r.get("feed", feed) == feed for r in rows):
            admissible[frozenset(r["id"] for r in rows)] = sum(
                1 for r in rows if r.get("names_settlement"))
    if not admissible:
        return None, 0, 0
    best = max(admissible.values())
    top = [ids for ids, n in admissible.items() if n == best]
    if best == 0 or len(top) != 1:
        return None, best, len(admissible)
    return sorted(top[0]), best, len(admissible)


def propose_rules(case: dict) -> Proposal:
    """
    The fixed-logic baseline, and the fallback when the model gives no
    usable answer. It chooses between sets on evidence, never on arithmetic
    alone: the one set carrying the most records that name the settlement,
    or an escalation with the sets listed. Proposing the engine's own set
    whatever its evidence — as this did — put a set that had tied on
    arithmetic in front of the verifier, which rejected it 53 times in 58.
    """
    reason = case["withheld_reason"]
    if case["tolerance_cents"] < abs(case["residual_cents"]) <= MAX_WRITE_OFF_CENTS \
            and case["engine_proposal"]:
        return Proposal("WRITE_OFF_ROUNDING", amount_cents=case["residual_cents"],
                        reason=f"Residual of {case['residual_cents']} paise is rounding.")
    ids, evidence, compared = _best_on_evidence(case)
    if ids:
        return Proposal("MATCH_PROPOSAL", txn_ids=ids,
                        reason=(f"Of the {compared} listed set(s) that reach the target, only "
                                f"this one has {evidence} record(s) whose reference names "
                                f"the settlement."))
    if any(e["reason"] == "timing_lag" for e in case["exceptions"]):
        return Proposal("WAIT_FOR_DATA", until_date=case["next_working_day"],
                        reason="A counterpart looks in transit; re-run after the next working day.")
    if reason == "unmatched" or not (case["engine_proposal"] or case["alternatives"]):
        return Proposal("REQUEST_SOURCE", party="gateway",
                        request="the settlement report listing this payout's transactions",
                        reason="Nothing in the pool sums to the payout.")
    return Proposal("ESCALATE", reason=(
        "Several sets reach the target and none carries more evidence than the rest; "
        "a person should choose between the sets listed."))


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
    The case as the model sees it: ids and the settlement name as opaque
    aliases, because benchmark labels once gave the answer away. redact_text is
    for evaluation only (benchmark memos say "decoy"); in production the text is
    real evidence.
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


def prompt_for(case: dict, redact_text: bool = False,
               rejected: list[str] | None = None) -> tuple[str, dict[str, str]]:
    """
    What the model is shown, and the alias map back to real ids. With
    `rejected`, the verifier's reasons for turning down its first answer are
    appended — in aliases too, so a retry cannot read a real id off them.
    """
    shown, back = _aliased(case, redact_text)
    prompt = json.dumps(shown, default=str)
    if rejected:
        fwd = {v: k for k, v in back.items()}
        said = "; ".join(rejected)
        for real, alias in sorted(fwd.items(), key=lambda kv: -len(kv[0])):
            said = said.replace(real, alias)
        said = said.replace(case["batch_id"], "SETTLEMENT")
        prompt += ("\n\nYour previous proposal was rejected by the code that checks it: "
                   f"{said}. Propose again — a different action if no set is supported "
                   "by the evidence.")
    return prompt, back


def propose_model(case: dict, redact_text: bool = False,
                  rejected: list[str] | None = None) -> Proposal | None:
    """The model's proposal, or None when no model is available or it fails."""
    if not llm_provider.is_configured():
        return None
    prompt, back = prompt_for(case, redact_text, rejected)
    raw = llm_provider.generate(prompt, system=_SYSTEM, schema=_SCHEMA, max_output_tokens=1500)
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

# How long the verifier may search the pool for a rival set.
RIVAL_PROBE_SECONDS = 3.0


def _rival_in_pool(case: dict, chosen: set[str], evidence: int) -> str | None:
    """
    Is there ANOTHER set of member-feed records, anywhere in the pool, that
    reaches the target with at least as much evidence as the chosen one?

    The listed sets are a sample. Comparing only against them let three wrong
    matches through where unrelated records also carried the settlement's
    reference: the chosen set out-evidenced every LISTED rival while an
    equally evidenced one sat unlisted in the pool (FAILURE_LOG 44). One
    bounded solve settles it. A search that runs out of time has not ruled a
    rival out, and is treated as though it had found one.
    """
    from ortools.sat.python import cp_model  # pylint: disable=import-outside-toplevel

    pool = case["_pool"]
    feed = case.get("member_feed")
    ids = [i for i, r in pool.items()
           if (not feed or r.get("feed") == feed) and r.get("currency") == case["currency"]]
    if not ids:
        return None
    model = cp_model.CpModel()
    pick = {i: model.new_bool_var(i) for i in ids}
    total = sum(pool[i]["amount_cents"] * pick[i] for i in ids)
    model.add(total >= case["target_cents"] - case["tolerance_cents"])
    model.add(total <= case["target_cents"] + case["tolerance_cents"])
    model.add(sum(pick[i] for i in ids if pool[i].get("named")) >= evidence)
    # Any set but the chosen one: at least one record in or out differs.
    model.add_bool_or([pick[i].Not() if i in chosen else pick[i] for i in ids])
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = RIVAL_PROBE_SECONDS
    solver.parameters.num_search_workers = 1
    status = solver.solve(model)
    if status == cp_model.INFEASIBLE:
        return None
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        other = sorted(i for i in ids if solver.value(pick[i]))
        return (f"another set of {len(other)} {feed or 'member-feed'} record(s) in the pool "
                f"also reaches the target with at least as much evidence ({evidence} naming "
                f"the settlement); escalate rather than choose between them")
    return ("could not rule out, within the time allowed, another set in the pool with as "
            "much evidence; escalate rather than choose")


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
        # A rival is compared as the payments it stands for. The engine's own
        # set can reach into the ledger feed, and its ledger copies name the
        # settlement too: taken as they are, they tied the TRUE set on
        # evidence and got it rejected — on CI, whose parallel solver picked a
        # different engine set than a laptop did. Dropping such sets instead
        # let 4 more wrong proposals through in the evaluation, because their
        # evidence is real; it belongs to the gateway payments they copy. So
        # each other-feed record is read as its member-feed twin (same
        # reference, same amount) where one exists.
        twin = {(v.get("ref"), v["amount_cents"]): i for i, v in pool.items()
                if feed and v.get("feed") == feed and v.get("ref")}

        def as_member_feed(group):
            out = set()
            for i in group:
                rec = pool.get(i, {})
                if feed and rec.get("feed") != feed:
                    i = twin.get((rec.get("ref"), rec.get("amount_cents")), i)
                out.add(i)
            return out

        rivals = [r for r in (as_member_feed(g) for g in case.get("_sets", []) if g)
                  if r != chosen]

        def named(group):
            return sum(1 for i in group if pool.get(i, {}).get("named"))
        if not failed and rivals:
            best_rival = max(named(r) for r in rivals)
            if named(chosen) <= best_rival:
                failed.append(
                    f"{len(rivals)} other set(s) reach the same target and this one has no "
                    f"more evidence than the best of them ({named(chosen)} vs {best_rival} "
                    f"record(s) whose reference names the settlement); escalate with the "
                    f"sets listed rather than choose between equals")
        if not failed:
            rival = _rival_in_pool(case, chosen, named(chosen))
            if rival:
                failed.append(rival)
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


def decide(case: dict, use_model: bool = False, redact_text: bool = False,
           ask=None) -> tuple[Proposal, dict, list[dict]]:
    """
    The investigator's loop, at most two model calls:
      1. Rules first: a set that alone leads on evidence needs no model.
      2. Otherwise the model proposes and the verifier checks it.
      3. Rejected, the model is told why (in aliases) and answers once more.
      4. Still rejected: the rules' answer if it verified, else an escalation.
    `ask` stands in for propose_model. Returns proposal, verdict and attempts.
    """
    ask = ask or propose_model
    rules = propose_rules(case)
    rules_verdict = verify(rules, case)
    attempts = [{"proposer": "rules", "action": rules.action, "valid": rules_verdict["valid"]}]
    if rules.action == "MATCH_PROPOSAL" and rules_verdict["valid"]:
        return rules, rules_verdict, attempts
    if use_model:
        rejected: list[str] | None = None
        for _ in range(2):
            proposal = ask(case, redact_text=redact_text, rejected=rejected)
            if proposal is None:
                break
            verdict = verify(proposal, case)
            attempts.append({"proposer": "model", "action": proposal.action,
                             "valid": verdict["valid"], "failed": verdict["failed"][:3]})
            if verdict["valid"]:
                return proposal, verdict, attempts
            rejected = verdict["failed"]
    if rules_verdict["valid"]:
        return rules, rules_verdict, attempts
    # Nothing verified — not the rules' pick, not the model's. A proposal the
    # checks turned down is never the answer; a person is asked instead.
    escalate = Proposal("ESCALATE", reason=(
        "No proposed set passed the checks; a person should choose between the "
        "sets listed."))
    return escalate, verify(escalate, case), attempts


def investigate(batch, candidates: list, report, use_model: bool = False) -> dict | None:
    """Case, proposal and verdict for a settlement that did not clear."""
    if report.match_result.cleared:
        return None
    case = build_case(batch, candidates, report)
    proposal, verdict, attempts = decide(case, use_model=use_model)
    return {"case": _public(case), "proposal": proposal.__dict__, "verification": verdict,
            "attempts": attempts}
