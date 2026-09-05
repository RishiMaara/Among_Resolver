"""
Compliance Agent (Agent 7)

Enforces strict RBI, FATF, and FinCEN Anti-Money Laundering (AML) 
and Combating the Financing of Terrorism (CFT) rules deterministically.

Rules Implemented:
1. Structuring (Smurfing)
2. CTR (Cash Transaction Reporting) Aggregates
3. Cross-Border Wire limits
4. High Velocity Anomalies
5. Sanctions & PEP Screening
6. Suspicious Keyword Screening
7. Dormant Account Spikes
8. Circular Flow (Layering)
9. Duplicate Transactions
10. Counterparty Concentration Risk
11. Extreme Amount Blocking
"""

import logging
import os
import pathlib
import difflib


from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import List, Tuple, Dict
from schema import NormalizedTxn, ComplianceStatus, ComplianceFinding
import audit


# ── Rule registry ──────────────────────────────────────────────────────────────
#
# The rule definitions live in compliance_rulebook.py, which is the published,
# inspectable artifact a bank's compliance function or a regulator would ask
# to see. This module does detection; that module states what is being
# enforced, on whose authority, and with what threshold.

from compliance_rulebook import COMPLIANCE_RULES, get_rule  # noqa: E402, F401


def _inr(cents: int) -> str:
    """
    Format integer cents as INR using Indian digit grouping
    (lakh/crore), e.g. 6_00_00_000.00 — not Western thousands grouping.
    A reviewer working to RBI/FIU-IND thresholds reads amounts in lakhs
    and crores; rendering Rs 6 crore as "60,000,000.00" forces them to
    count digits to check it against a Rs 5 crore ceiling.
    """
    whole, paise = divmod(int(round(abs(cents))), 100)
    digits = str(whole)

    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        # after the last three digits, group in pairs (thousand, lakh, crore...)
        pairs = []
        while len(head) > 2:
            pairs.insert(0, head[-2:])
            head = head[:-2]
        if head:
            pairs.insert(0, head)
        grouped = ",".join(pairs + [tail])
    else:
        grouped = digits

    sign = "-" if cents < 0 else ""
    return f"{sign}₹{grouped}.{paise:02d}"

# --- Thresholds (in integer Cents) ---
INR_50K = 50_000 * 100
INR_1L = 100_000 * 100
INR_5L = 500_000 * 100
INR_10L = 1_000_000 * 100
INR_1CR = 10_000_000 * 100
INR_5CR = 50_000_000 * 100

# Cycle search over a payment graph is exponential in depth. Real layering
# rings are short; an unbounded search would hang on a dense batch.
MAX_CYCLE_DEPTH = 4

# ── Sanctions screening list ───────────────────────────────────────────────────
#
# SANCTIONS_HIT is the only rule in this engine that blocks funds on a
# statutory basis, so where its list comes from matters more than any other
# configuration here. Four names hardcoded in source is a demo, and a demo that
# silently passes every real designated party is worse than one that admits it.
#
# The list is therefore loaded from SANCTIONS_LIST_PATH (one identifier per
# line, blank lines and # comments ignored). Without that variable the engine
# falls back to a tiny illustrative set and says so loudly at import — a
# deployment that screens four names must never look like one that screens the
# UN Consolidated List.
#
# Even a file-backed list is a point-in-time copy: designations change, and
# real screening also needs fuzzy name matching, aliases, transliteration and
# date-of-birth disambiguation. The rulebook states this limitation to any
# reviewer (see compliance_rulebook.SANCTIONS_HIT.threshold_applied).

# Stored already folded, in the same form normalize_party produces. A constant
# written in a different shape from the list it stands in for is a trap: the
# fallback would compare unequal to itself.
_ILLUSTRATIVE_SANCTIONS = {"OFAC SDN 1", "UN TERROR 2", "BIN LADEN", "DAWOOD IBRAHIM"}

# Where a real list is expected to live when SANCTIONS_LIST_PATH is not set.
# Convention beats configuration for the common case: fetch_sanctions_list.py
# writes here, so a deployment that ran the fetch screens a real list without
# anyone remembering to export a variable. Discovery is still ANNOUNCED at
# import - a file picked up implicitly must not be less visible than one
# named explicitly.
DEFAULT_SANCTIONS_PATHS = (
    pathlib.Path(__file__).resolve().parents[2] / "data" / "sanctions" / "un_consolidated.txt",
)

# Screening goes stale in a way most rules do not: designations are added
# continuously, so a list retrieved months ago passes parties designated today
# while looking identical to one retrieved this morning.
STALE_AFTER_DAYS = 30

SANCTIONS_LIST_SOURCE = "illustrative-builtin"
SANCTIONS_LIST_META: dict[str, str] = {}


def normalize_party(name: str) -> str:
    """
    Fold a counterparty name to the form the sanctions list is stored in.

    Without this, screening compares raw `payer_id` strings against list
    entries by exact equality, and a real list of full names matches nothing:
    "Al-Qaida" never equals "AL QAIDA", so the engine reports a clean screen
    against a list it cannot hit. Punctuation and spacing are noise here;
    folding both sides identically is what makes the comparison mean anything.

    Deliberately the same rule as fetch_sanctions_list.normalize. If the two
    drift apart the list stops matching itself and the failure is silent,
    which is why a test asserts they agree.
    """
    return " ".join("".join(
        ch if ch.isalnum() else " " for ch in (name or "")
    ).upper().split())


def _fold_illustrative() -> set[str]:
    return {normalize_party(n) for n in _ILLUSTRATIVE_SANCTIONS}


def _read_list_file(path: str) -> tuple[set[str], dict[str, str]]:
    """
    Parse a list file into (identifiers, provenance metadata).

    `# key: value` header comments carry provenance. They are metadata, not
    decoration: an auditor's first question about a sanctions block is which
    list it came from and how old that list was.
    """
    names: set[str] = set()
    meta: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                body = line.lstrip("#").strip()
                if ":" in body:
                    key, _, val = body.partition(":")
                    key = key.strip().lower()
                    if key in ("source", "generated", "retrieved", "entries"):
                        meta[key] = val.strip()
                continue
            folded = normalize_party(line)
            if folded:
                names.add(folded)
    return names, meta


def _warn_if_stale(meta: dict[str, str], path: str) -> None:
    log = logging.getLogger(__name__)
    retrieved = meta.get("retrieved")
    if not retrieved:
        log.warning(
            "Compliance: sanctions list %r carries no `# retrieved:` date, so "
            "its age is unknown. An undated list cannot be shown to be current.",
            path,
        )
        return
    try:
        when = datetime.fromisoformat(retrieved)
    except ValueError:
        return
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - when).days
    if age > STALE_AFTER_DAYS:
        log.warning(
            "Compliance: sanctions list %r was retrieved %d days ago. "
            "Designations added since then will NOT be screened. Re-run "
            "scripts/fetch_sanctions_list.py.",
            path, age,
        )


def _load_sanctions_list() -> set[str]:
    global SANCTIONS_LIST_SOURCE, SANCTIONS_LIST_META
    log = logging.getLogger(__name__)

    path = os.environ.get("SANCTIONS_LIST_PATH", "").strip()
    discovered = False
    if not path:
        for candidate in DEFAULT_SANCTIONS_PATHS:
            if candidate.is_file():
                path = str(candidate)
                discovered = True
                break

    if not path:
        SANCTIONS_LIST_SOURCE = "illustrative-builtin"
        SANCTIONS_LIST_META = {}
        log.warning(
            "Compliance: screening an ILLUSTRATIVE %d-name sanctions list. This "
            "is NOT the UN/OFAC/MHA lists and will not stop a real designated "
            "party. Run scripts/fetch_sanctions_list.py or set "
            "SANCTIONS_LIST_PATH to screen a real list.",
            len(_ILLUSTRATIVE_SANCTIONS),
        )
        return _fold_illustrative()

    try:
        names, meta = _read_list_file(path)
    except OSError as e:
        # Fail loudly and keep screening. Silently continuing with an empty
        # list would mean every counterparty passes sanctions screening
        # because a path was mistyped.
        SANCTIONS_LIST_SOURCE = "illustrative-builtin"
        SANCTIONS_LIST_META = {}
        log.error(
            "Compliance: could not read SANCTIONS_LIST_PATH %r (%s). Falling "
            "back to the illustrative list - screening is NOT effective.",
            path, e,
        )
        return _fold_illustrative()

    if not names:
        SANCTIONS_LIST_SOURCE = "illustrative-builtin"
        SANCTIONS_LIST_META = {}
        log.error(
            "Compliance: SANCTIONS_LIST_PATH %r is empty. Falling back to the "
            "illustrative list rather than screening against nothing.", path,
        )
        return _fold_illustrative()

    SANCTIONS_LIST_SOURCE = path
    SANCTIONS_LIST_META = meta
    log.info(
        "Compliance: loaded %d sanctioned identifiers from %s%s "
        "(list generated %s, retrieved %s).",
        len(names), path, " [auto-discovered]" if discovered else "",
        meta.get("generated", "unknown"), meta.get("retrieved", "unknown"),
    )
    _warn_if_stale(meta, path)
    return names


def sanctions_provenance() -> dict:
    """
    What an auditor needs in order to judge a sanctions block, as data.

    `is_illustrative` is the field that matters. A block produced by the
    built-in demo list and a block produced by the UN Consolidated List look
    identical in the report otherwise, and they mean entirely different things.
    """
    real = SANCTIONS_LIST_SOURCE != "illustrative-builtin"
    return {
        "source": SANCTIONS_LIST_SOURCE,
        "is_illustrative": not real,
        "entry_count": len(SANCTIONS_LIST),
        "list_generated": SANCTIONS_LIST_META.get("generated"),
        "retrieved": SANCTIONS_LIST_META.get("retrieved"),
        "match_mode": "exact after normalisation; no fuzzy, transliteration or DOB matching",
    }


SANCTIONS_LIST = _load_sanctions_list()

SANCTIONS_PREFIX_INDEX = defaultdict(list)
for s in SANCTIONS_LIST:
    prefix = s[:2] if len(s) >= 2 else s
    SANCTIONS_PREFIX_INDEX[prefix].append(s)

SUSPICIOUS_KEYWORDS = ["bomb", "terror", "isis", "taliban", "crypto", "hawala", "donation", "bribe"]


def _log(txn: NormalizedTxn | List[NormalizedTxn], rule: str, reason: str, severity: str, action: str):
    txns = [txn] if isinstance(txn, NormalizedTxn) else txn
    txn_ids = [t.source_txn_id for t in txns]

    rule_def = COMPLIANCE_RULES.get(rule)

    # Update status for each txn if not already blocked, and attach a
    # finding so downstream consumers (the exception queue, the UI, the
    # audit trail) can explain WHY without re-running the scan. Previously
    # the reason existed only as a formatted string in the audit log, so a
    # blocked transaction surfaced to the user as a bare "blocked by
    # Compliance & Ethics Agent" with no rule, no basis and no citation.
    for t in txns:
        if action == "BLOCKED":
            t.compliance_status = ComplianceStatus.BLOCKED
        elif action == "FLAGGED" and t.compliance_status != ComplianceStatus.BLOCKED:
            t.compliance_status = ComplianceStatus.FLAGGED

        if rule_def is not None:
            t.compliance_findings.append(ComplianceFinding(
                rule_id=rule_def.rule_id,
                title=rule_def.title,
                severity=rule_def.severity,
                action=action,
                basis=rule_def.basis,
                authority=rule_def.authority,
                source_name=rule_def.source_name,
                citation=rule_def.citation,
                reference_url=rule_def.reference_url,
                rule_text=rule_def.rule_text,
                threshold_applied=rule_def.threshold_applied,
                why=rule_def.why,
                observed=reason,
                remediation=rule_def.remediation,
            ))

    basis = rule_def.basis.value if rule_def else "unregistered_rule"
    detail = (
        f"{action} ({severity}) [{basis}]: Rule {rule} - {reason}. "
        f"Txns: {txn_ids}"
    )
    audit.log_decision(
        batch_id="GLOBAL_COMPLIANCE_SCAN",
        agent="compliance_agent",
        detail=detail
    )

def _group_by_payer(transactions: List[NormalizedTxn]) -> Dict[str, List[NormalizedTxn]]:
    groups = defaultdict(list)
    for t in transactions:
        payer = t.payer_id or t.ref_id_canonical or "UNKNOWN_PAYER"
        groups[payer].append(t)
    for k in groups:
        groups[k].sort(key=lambda x: x.timestamp_utc)
    return groups

def check_structuring(transactions: List[NormalizedTxn]):
    """Rule 1: Detect ≥3 txns <₹50k summing to ≥₹50k within 24h."""
    groups = _group_by_payer(transactions)
    for payer, txns in groups.items():
        if payer == "UNKNOWN_PAYER":
            continue
        small_txns = [t for t in txns if t.amount_cents < INR_50K]
        
        # Sliding 24h window
        for i in range(len(small_txns)):
            window = []
            for j in range(i, len(small_txns)):
                if (small_txns[j].timestamp_utc - small_txns[i].timestamp_utc) <= timedelta(hours=24):
                    window.append(small_txns[j])
                else:
                    break
            
            if len(window) >= 3 and sum(t.amount_cents for t in window) >= INR_50K:
                _log(window, "STRUCTURING_PATTERN", f"{len(window)} transactions below {_inr(INR_50K)} from the same source within 24h, totalling {_inr(sum(t.amount_cents for t in window))}", "MEDIUM", "FLAGGED")
                break # Avoid spamming flags for the same sequence

def check_ctr(transactions: List[NormalizedTxn]):
    """Rule 2: Single cash ≥₹10L, or series of cash <₹10L aggregating ≥₹10L in 30d."""
    groups = _group_by_payer(transactions)
    
    for payer, txns in groups.items():
        cash_txns = [t for t in txns if t.is_cash]
        
        # Single Txn Check
        for t in cash_txns:
            if t.amount_cents >= INR_10L:
                _log(t, "CTR_THRESHOLD", f"Single cash transaction of {_inr(t.amount_cents)} at or above the {_inr(INR_10L)} reporting threshold", "MEDIUM", "FLAGGED")
                
        # Aggregate check
        small_cash = [t for t in cash_txns if t.amount_cents < INR_10L]
        for i in range(len(small_cash)):
            window = []
            for j in range(i, len(small_cash)):
                if (small_cash[j].timestamp_utc - small_cash[i].timestamp_utc) <= timedelta(days=30):
                    window.append(small_cash[j])
                else:
                    break
            if sum(t.amount_cents for t in window) >= INR_10L:
                _log(window, "CTR_AGGREGATE", f"{len(window)} connected cash transactions totalling {_inr(sum(t.amount_cents for t in window))} within 30 days", "MEDIUM", "FLAGGED")
                break

def check_cross_border(transactions: List[NormalizedTxn]):
    """Rule 3: Cross border wire ≥₹5L."""
    for t in transactions:
        if t.is_wire_transfer and t.amount_cents >= INR_5L:
             _log(t, "CROSS_BORDER_LIMIT", f"Cross-border wire of {_inr(t.amount_cents)} at or above the {_inr(INR_5L)} review trigger", "MEDIUM", "FLAGGED")

def check_velocity(transactions: List[NormalizedTxn]):
    """Rule 4: >100 txns or >₹1Cr in 1h."""
    groups = _group_by_payer(transactions)
    for payer, txns in groups.items():
        for i in range(len(txns)):
            window = []
            for j in range(i, len(txns)):
                if (txns[j].timestamp_utc - txns[i].timestamp_utc) <= timedelta(hours=1):
                    window.append(txns[j])
                else:
                    break
            if len(window) > 100 or sum(t.amount_cents for t in window) > INR_1CR:
                _log(window, "HIGH_VELOCITY", f"{len(window)} transactions totalling {_inr(sum(t.amount_cents for t in window))} within 1 hour", "MEDIUM", "FLAGGED")
                break

def check_sanctions(transactions: List[NormalizedTxn]):
    """Rule 5: Payer/Payee in Sanctions List."""
    for t in transactions:
        # Both sides go through the same normaliser. Comparing a raw payer_id
        # against a list of real names is a screen that cannot hit: "Al-Qaida"
        # does not equal "AL QAIDA" by string equality, so the engine would
        # report a clean screen against a list it is structurally unable to
        # match.
        parties = [normalize_party(t.payer_id), normalize_party(t.payee_id)]
        hits = []
        for p in parties:
            if not p:
                continue
            # 1. Exact match (fast path)
            if p in SANCTIONS_LIST:
                hits.append(p)
                continue
            # 2. Fuzzy match (slow path, optimized via prefix index)
            p_len = len(p)
            prefix = p[:2] if p_len >= 2 else p
            candidates = SANCTIONS_PREFIX_INDEX.get(prefix, [])
            
            for s in candidates:
                if abs(p_len - len(s)) > 4:
                    continue
                if difflib.SequenceMatcher(None, p, s).ratio() > 0.85:
                    hits.append(f"{p} (fuzzy match for {s})")
                    break

        if hits:
            _log(
                t, "SANCTIONS_HIT",
                f"Counterparty {', '.join(hits)} matches the screened sanctions list",
                "HIGH", "BLOCKED",
            )

def check_keywords(transactions: List[NormalizedTxn]):
    """Rule 6: Memo contains suspicious keywords."""
    for t in transactions:
        memo = t.memo_normalized.lower()
        if any(word in memo for word in SUSPICIOUS_KEYWORDS):
            matched = [w for w in SUSPICIOUS_KEYWORDS if w in memo]
            _log(t, "KEYWORD_ALERT", f"Memo contains high-risk keyword(s): {','.join(matched)}", "MEDIUM", "FLAGGED")

def check_dormant_spike(transactions: List[NormalizedTxn]):
    """Rule 7: >90d gap then large transaction."""
    groups = _group_by_payer(transactions)
    for payer, txns in groups.items():
        for i in range(1, len(txns)):
            gap = txns[i].timestamp_utc - txns[i-1].timestamp_utc
            if gap >= timedelta(days=90) and txns[i].amount_cents >= INR_1L:
                _log(txns[i], "DORMANT_ACCOUNT_SPIKE", f"Counterparty inactive for {gap.days} days then moved {_inr(txns[i].amount_cents)}", "LOW", "FLAGGED")

def check_circular_flow(transactions: List[NormalizedTxn]):
    """
    Rule 8: funds returning to their origin through intermediaries inside 24h.

    A -> B -> C -> A with no apparent commercial purpose is a recognised
    layering pattern. The cycle must pass through DISTINCT intermediaries and
    move forward in time: A -> B -> A -> B -> A is one pair of parties
    invoicing each other, not a layering chain, and reporting it as one buries
    the real signal in noise.
    """
    graph: Dict[str, list] = defaultdict(list)
    for t in transactions:
        if t.payer_id and t.payee_id and t.payer_id != t.payee_id:
            graph[t.payer_id].append((t.payee_id, t))

    def dfs(node: str, start_node: str, path_txns: list, seen: set, depth: int = 0):
        # Depth cap keeps this bounded: without it, cycle search over a dense
        # payment graph is exponential, and this runs over every batch.
        if depth > MAX_CYCLE_DEPTH:
            return None

        for neighbor, txn in graph.get(node, ()):
            if path_txns:
                last = path_txns[-1]
                delta = txn.timestamp_utc - last.timestamp_utc
                # Legs must advance in time and stay inside the window. Money
                # cannot flow back through a leg that happened earlier.
                if delta < timedelta(0) or delta > timedelta(hours=24):
                    continue

            if neighbor == start_node:
                # >= 2 intermediate hops, i.e. at least A -> B -> C -> A.
                if depth >= 2:
                    return path_txns + [txn]
                continue

            # Distinct intermediaries only — revisiting a node makes it a
            # back-and-forth between two parties, not a laundering ring.
            if neighbor in seen:
                continue

            cycle = dfs(neighbor, start_node, path_txns + [txn],
                        seen | {neighbor}, depth + 1)
            if cycle:
                return cycle
        return None

    reported: set = set()
    for start in list(graph.keys()):
        cycle = dfs(start, start, [], {start})
        if not cycle:
            continue
        key = tuple(sorted(t.source_txn_id for t in cycle))
        if key in reported:
            continue
        reported.add(key)
        parties = " -> ".join([cycle[0].payer_id] + [t.payee_id for t in cycle])
        _log(cycle, "CIRCULAR_FLOW",
             f"Funds returned to origin across {len(cycle)} legs within 24h: {parties}",
             "HIGH", "FLAGGED")


def check_duplicates(transactions: List[NormalizedTxn]):
    """Rule 9: Exact same amount, date, parties."""
    seen = {}
    for t in transactions:
        key = (t.amount_cents, t.timestamp_utc, t.payer_id, t.payee_id)
        if key in seen:
            _log([seen[key], t], "DUPLICATE_TX", f"Identical amount, timestamp and counterparties as {seen[key].source_txn_id}", "LOW", "FLAGGED")
        else:
            seen[key] = t

def check_concentration(transactions: List[NormalizedTxn]):
    """Rule 10: >90% of batch volume from one payer."""
    if not transactions: return
    total_vol = sum(t.amount_cents for t in transactions)
    if total_vol == 0: return
    
    groups = _group_by_payer(transactions)
    for payer, txns in groups.items():
        if payer == "UNKNOWN_PAYER": continue
        group_vol = sum(t.amount_cents for t in txns)
        if group_vol > 0.9 * total_vol and len(txns) > 1:
            _log(txns, "CONCENTRATION_RISK", f"Counterparty {payer} accounts for {100*group_vol/total_vol:.1f}% of batch volume across {len(txns)} txns", "MEDIUM", "FLAGGED")

def check_extreme_amount(transactions: List[NormalizedTxn]):
    """Rule 11: Hard block above the internal ₹5Cr single-transaction ceiling.

    NOTE: internal risk policy, not a statutory cap — see COMPLIANCE_RULES.
    """
    for t in transactions:
        if t.amount_cents >= INR_5CR:
            _log(
                t, "LIMIT_EXCEEDED",
                f"Amount {_inr(t.amount_cents)} is at or above the internal "
                f"single-transaction ceiling of {_inr(INR_5CR)} "
                f"(exceeds by {_inr(t.amount_cents - INR_5CR)})",
                "HIGH", "BLOCKED",
            )


def scan(transactions: List[NormalizedTxn]) -> Tuple[List[NormalizedTxn], List[NormalizedTxn]]:
    """
    Main entry point for Agent 7.
    Returns (safe_txns, blocked_txns)
    """
    if not transactions:
        return [], []
        
    audit.log_decision(
        batch_id="GLOBAL_COMPLIANCE_SCAN", 
        agent="compliance_agent", 
        detail=f"Scan started for {len(transactions)} transactions."
    )

    # Run all rules sequentially
    check_structuring(transactions)
    check_ctr(transactions)
    check_cross_border(transactions)
    check_velocity(transactions)
    check_sanctions(transactions)
    check_keywords(transactions)
    check_dormant_spike(transactions)
    check_circular_flow(transactions)
    check_duplicates(transactions)
    check_concentration(transactions)
    check_extreme_amount(transactions)

    safe_txns = []
    blocked_txns = []

    for t in transactions:
        if t.compliance_status == ComplianceStatus.BLOCKED:
            blocked_txns.append(t)
        else:
            safe_txns.append(t)
            
    audit.log_decision(
        batch_id="GLOBAL_COMPLIANCE_SCAN", 
        agent="compliance_agent", 
        detail=f"Scan completed. Safe: {len(safe_txns)}. Blocked: {len(blocked_txns)}"
    )

    return safe_txns, blocked_txns
