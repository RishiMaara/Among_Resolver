"""
Compliance Rulebook — the published, inspectable rule set for Agent 7.

This module is deliberately separate from the detection code. It is the
artifact a bank's compliance function, an external auditor, or a regulator
would ask to see: for every control the engine enforces, what the control
is, what authority it derives from, what that authority actually requires,
what threshold THIS system applies, and where to read the source.

Two fields carry most of the weight:

  basis              Is this law, supervisory guidance, or our own risk
                     appetite? Presenting an internal threshold as a
                     statutory requirement would misrepresent the law to
                     whoever relies on this output, so every rule must
                     declare which it is.

  threshold_applied  The concrete parameter this engine uses, stated
                     separately from rule_text (what the source requires).
                     Where a statute sets a number, these agree. Where the
                     statute sets a principle and we chose a number to
                     operationalise it, the difference is visible rather
                     than hidden — which is exactly what an examiner needs
                     in order to challenge the calibration.

All reference URLs were confirmed reachable on 2026-08-30. A few official
sites return 403 to non-browser clients (WAF bot protection); those were
verified by loading them in a browser.

NOTE ON SCOPE: this is a reconciliation engine's screening layer, not a
regulated AML system of record. It does not file reports, does not
maintain a sanctions list of record, and its determinations are advisory
inputs to a human reviewer. See SCOPE_NOTE below, which is served with
every rulebook response.

The rulebook is published openly — there is no access gate on it. A
published control set that only its author can read does not do the job
a published control set exists to do.
"""

from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Dict

from schema import ComplianceBasis


# ── Official sources (URLs verified reachable 2026-08-30) ─────────────────────
FIU_PMLA = "https://fiuindia.gov.in/files/AML_Legislation/pmla_2002.html"
FIU_PML_RULES = "https://fiuindia.gov.in/files/AML_Legislation/pml_rules.html"
FIU_CTR = "https://fiuindia.gov.in/content/ctr.html"
FIU_STR = "https://fiuindia.gov.in/content/str.html"
RBI_KYC_MD = "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=11566"
UN_CONSOLIDATED = "https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list"
MHA_CT = "https://www.mha.gov.in/en/divisionofmha/counter-terrorism-and-counter-radicalization-division"
OFAC_SDN = "https://ofac.treasury.gov/specially-designated-nationals-and-blocked-persons-list-sdn-human-readable-lists"
FATF_RECS = "https://www.fatf-gafi.org/en/publications/Fatfrecommendations/Fatf-recommendations.html"
FFIEC_MANUAL = "https://bsaaml.ffiec.gov/manual"


_SCOPE_HEAD = (
    "This engine performs automated screening to support human review. It does "
    "not file CTRs or STRs, so its determinations are advisory inputs to a "
    "reviewer, not compliance decisions in their own right. "
)
_SCOPE_TAIL = (
    "Rules marked 'internal_policy' are this system's own risk thresholds and "
    "carry no statutory force. Obligations should be confirmed against the "
    "cited primary sources."
)


def scope_note() -> str:
    """
    The scope disclaimer, describing the list ACTUALLY loaded.

    This was a constant asserting "a static illustrative sanctions list rather
    than the live official lists". That was true of the four-name placeholder
    and became false the moment a real UN Consolidated List was fetched — so
    the page understated the system, which is the same fault as overstating
    it: the note stopped describing what the engine does.

    Both readings still need saying, so it asks rather than assumes. The
    exact-match caveat is unconditional, because it is true of either list.
    """
    try:
        import compliance_agent
        prov = compliance_agent.sanctions_provenance()
    except Exception:
        prov = {}

    if prov.get("is_illustrative", True):
        listing = (
            "It screens a static ILLUSTRATIVE sanctions list, not the live "
            "official lists, and will not stop a real designated party. "
        )
    else:
        n = prov.get("entry_count")
        generated = (prov.get("list_generated") or "")[:10]
        listing = (
            f"It screens the UN Consolidated List"
            f"{f' ({n:,} identifiers' if n else ''}"
            f"{f', generated {generated}' if generated else ''}"
            f"{')' if n else ''}. Matching is exact after normalisation — no "
            f"fuzzy matching, transliteration variants or date-of-birth "
            f"disambiguation — so a misspelt name will pass. "
        )
    return _SCOPE_HEAD + listing + _SCOPE_TAIL


# Kept as a name for callers that import it directly; evaluated at import,
# so prefer scope_note() where the list may be refetched at runtime.
SCOPE_NOTE = scope_note()


@dataclass(frozen=True)
class ComplianceRule:
    rule_id: str
    title: str
    severity: str              # LOW | MEDIUM | HIGH
    action: str                # FLAGGED | BLOCKED
    basis: ComplianceBasis
    authority: str             # who sets the underlying obligation
    source_name: str           # name of the source document
    citation: str              # the specific provision relied on
    reference_url: str         # official source a reviewer can open
    rule_text: str             # what the SOURCE actually requires
    threshold_applied: str     # the parameter THIS ENGINE uses
    why: str                   # plain-language rationale
    remediation: str           # what the reviewer should do next

    def to_dict(self) -> dict:
        d = asdict(self)
        d["basis"] = self.basis.value
        return d


COMPLIANCE_RULES: Dict[str, ComplianceRule] = {

    # ── Blocking rules ────────────────────────────────────────────────────────

    "SANCTIONS_HIT": ComplianceRule(
        rule_id="SANCTIONS_HIT",
        title="Sanctioned party match",
        severity="HIGH",
        action="BLOCKED",
        basis=ComplianceBasis.STATUTORY,
        authority="UN Security Council; Government of India (UAPA s.51A); US Treasury OFAC",
        source_name="UN Security Council Consolidated List",
        citation=(
            "UNSC Consolidated List; Unlawful Activities (Prevention) Act 1967 "
            "s.51A (India); RBI Master Direction – KYC (sanctions screening); "
            "31 CFR Ch. V (OFAC, US)"
        ),
        reference_url=UN_CONSOLIDATED,
        rule_text=(
            "Funds and financial assets of designated individuals and entities "
            "must be frozen without delay, and no funds may be made available to "
            "them. Regulated entities must screen customers and counterparties "
            "against the applicable designation lists on an ongoing basis."
        ),
        threshold_applied=(
            "Exact match of payer or payee identifier against the screened list, "
            "after normalisation. LIMITATION: matching is exact only — no fuzzy "
            "matching, transliteration variants or date-of-birth disambiguation "
            "— so a misspelt or transliterated name will pass. The list itself "
            "and the date it was retrieved are reported by /health; run "
            "scripts/fetch_sanctions_list.py to refresh it."
        ),
        why=(
            "Dealing with a designated person or entity is prohibited outright. "
            "This is one of the few cases where a payment must actually be "
            "stopped rather than merely reported."
        ),
        remediation=(
            "Do not release the funds. Escalate to the Principal Officer to "
            "confirm the match against the live official lists, screen for "
            "false positives on common names, and file the required report if "
            "the match is confirmed."
        ),
    ),

    "LIMIT_EXCEEDED": ComplianceRule(
        rule_id="LIMIT_EXCEEDED",
        title="Internal single-transaction ceiling exceeded",
        severity="HIGH",
        action="BLOCKED",
        # NOT statutory. No Indian law caps a single transaction at Rs 5 crore.
        basis=ComplianceBasis.INTERNAL_POLICY,
        authority="AmongResolver internal risk policy",
        source_name="Internal control standard (no external source)",
        citation="Internal control: single-transaction hard ceiling",
        # Points at the genuine reporting context this control sits inside —
        # NOT at a rule that claims to impose the ceiling.
        reference_url=FIU_CTR,
        rule_text=(
            "NO STATUTORY RULE IMPOSES THIS CEILING. Large-value transactions "
            "are lawful. The relevant legal duty for prescribed transactions is "
            "to REPORT them to FIU-IND, not to block them. This control exists "
            "so that a payment of this size is consciously approved by a person "
            "rather than settled automatically."
        ),
        threshold_applied="Single transaction >= Rs 5,00,00,000 (Rs 5 crore)",
        why=(
            "An internal ceiling that halts unusually large single transfers for "
            "human sign-off before they settle. It reflects this system's risk "
            "appetite, not a legal limit."
        ),
        remediation=(
            "Route to a Finance/Compliance approver for manual authorisation. "
            "Confirm the counterparty and commercial purpose, then release or "
            "reject. Separately assess any FIU-IND reporting obligation on its "
            "own merits — exceeding this internal ceiling is not by itself a "
            "reportable event."
        ),
    ),

    # ── Reporting-threshold rules ─────────────────────────────────────────────

    "CTR_THRESHOLD": ComplianceRule(
        rule_id="CTR_THRESHOLD",
        title="Cash transaction at reporting threshold",
        severity="MEDIUM",
        action="FLAGGED",
        basis=ComplianceBasis.STATUTORY,
        authority="Financial Intelligence Unit – India (FIU-IND)",
        source_name="PML (Maintenance of Records) Rules, 2005",
        citation="PML Rules 2005, r.3(1) — cash transactions above Rs 10 lakh",
        reference_url=FIU_CTR,
        rule_text=(
            "Reporting entities must furnish to FIU-IND a Cash Transaction "
            "Report covering all cash transactions of a value exceeding "
            "Rs 10 lakh, or their equivalent in foreign currency."
        ),
        threshold_applied="Single cash transaction >= Rs 10,00,000",
        why=(
            "A reporting duty, not a prohibition. The transaction remains "
            "lawful; it must simply be included in the CTR filing."
        ),
        remediation="Ensure the transaction is included in the CTR filing for the period.",
    ),

    "CTR_AGGREGATE": ComplianceRule(
        rule_id="CTR_AGGREGATE",
        title="Aggregated cash transactions at reporting threshold",
        severity="MEDIUM",
        action="FLAGGED",
        basis=ComplianceBasis.STATUTORY,
        authority="Financial Intelligence Unit – India (FIU-IND)",
        source_name="PML (Maintenance of Records) Rules, 2005",
        citation=(
            "PML Rules 2005, r.3(1) — series of integrally connected cash "
            "transactions individually below Rs 10 lakh"
        ),
        reference_url=FIU_PML_RULES,
        rule_text=(
            "Where a series of cash transactions are integrally connected to "
            "each other and have individually been valued below Rs 10 lakh, and "
            "together exceed that value, they must be aggregated and reported "
            "where they take place within one calendar month."
        ),
        threshold_applied=(
            "Cash transactions from one payer aggregating >= Rs 10,00,000 "
            "within a 30-day rolling window. NOTE: the statute frames this as "
            "one calendar month; this engine uses a 30-day rolling window, "
            "which is a close but not identical construction."
        ),
        why=(
            "Connected cash transactions are assessed in aggregate against the "
            "reporting threshold, not one at a time."
        ),
        remediation="Confirm the transactions are integrally connected, then include in the CTR filing.",
    ),

    # ── Suspicion / typology rules ────────────────────────────────────────────

    "STRUCTURING_PATTERN": ComplianceRule(
        rule_id="STRUCTURING_PATTERN",
        title="Possible structuring (smurfing)",
        severity="MEDIUM",
        action="FLAGGED",
        basis=ComplianceBasis.REGULATORY_GUIDANCE,
        authority="FIU-IND / PMLA 2002",
        source_name="PMLA 2002 and PML (Maintenance of Records) Rules, 2005",
        citation=(
            "PMLA 2002 s.12 (reporting obligation); PML Rules 2005 r.3 "
            "(integrally connected transactions). Detection parameters are internal."
        ),
        reference_url=FIU_STR,
        rule_text=(
            "Reporting entities must report suspicious transactions, including "
            "attempts to structure transactions so as to avoid a reporting "
            "threshold. The law states the obligation; it does not prescribe the "
            "detection parameters used to identify candidates."
        ),
        threshold_applied=(
            ">= 3 transactions from one payer, each below Rs 50,000, totalling "
            ">= Rs 50,000 within 24 hours. INTERNAL CALIBRATION — not a "
            "statutory threshold."
        ),
        why=(
            "Splitting one payment into several smaller ones can be an attempt "
            "to stay below a reporting threshold. Frequently benign — "
            "instalments and partial settlements look identical."
        ),
        remediation=(
            "Review whether the component payments are genuinely one commercial "
            "transaction. If deliberate avoidance is suspected, consider an STR."
        ),
    ),

    "CIRCULAR_FLOW": ComplianceRule(
        rule_id="CIRCULAR_FLOW",
        title="Circular fund flow (possible layering)",
        severity="HIGH",
        action="FLAGGED",
        basis=ComplianceBasis.REGULATORY_GUIDANCE,
        authority="FATF; FFIEC (US supervisory guidance)",
        source_name="FATF Recommendations; FFIEC BSA/AML Examination Manual",
        citation="FATF R.20 (suspicious transaction reporting); FFIEC BSA/AML Manual red flags",
        reference_url=FATF_RECS,
        rule_text=(
            "Funds moving in a closed loop between related parties with no "
            "apparent lawful purpose is a recognised layering typology and a "
            "recognised indicator for suspicious transaction reporting."
        ),
        threshold_applied="Cycle of >= 3 legs returning to the originating party within 24 hours",
        why=(
            "Funds returning to their origin through intermediaries without "
            "commercial rationale is a classic layering pattern."
        ),
        remediation="Establish the commercial rationale for each leg; consider an STR.",
    ),

    "CROSS_BORDER_LIMIT": ComplianceRule(
        rule_id="CROSS_BORDER_LIMIT",
        title="Large cross-border transfer",
        severity="MEDIUM",
        action="FLAGGED",
        basis=ComplianceBasis.REGULATORY_GUIDANCE,
        authority="FATF; RBI",
        source_name="FATF Recommendation 16 (wire transfers)",
        citation="FATF R.16 — originator and beneficiary information on wire transfers",
        reference_url=FATF_RECS,
        rule_text=(
            "Cross-border wire transfers must be accompanied by required and "
            "accurate originator information and required beneficiary "
            "information, so that transfers remain traceable."
        ),
        threshold_applied=(
            "Cross-border wire >= Rs 5,00,000. INTERNAL CALIBRATION — chosen as "
            "a review trigger, not a threshold set by R.16."
        ),
        why="Large cross-border transfers warrant confirmation that traceability data is complete.",
        remediation="Verify originator and beneficiary details are complete before release.",
    ),

    "DORMANT_ACCOUNT_SPIKE": ComplianceRule(
        rule_id="DORMANT_ACCOUNT_SPIKE",
        title="Dormant counterparty reactivation",
        severity="LOW",
        action="FLAGGED",
        basis=ComplianceBasis.REGULATORY_GUIDANCE,
        # The only rule here that cited a US bank examination manual and
        # nothing else. The FFIEC red-flag catalogue is the best-documented
        # public source for this pattern and laundering typologies are not
        # nationally specific, so it stays as the pattern's origin — but a
        # rule applied to an Indian merchant needs an anchor that actually
        # reaches them, and RBI's KYC Master Direction is the one that
        # requires ongoing monitoring of account activity.
        authority="RBI (Master Direction – KYC); pattern from FFIEC (US supervisory guidance)",
        source_name="RBI Master Direction – KYC; FFIEC BSA/AML Examination Manual",
        citation=(
            "RBI Master Direction – KYC, ongoing due diligence and monitoring "
            "of account activity; pattern described in the FFIEC BSA/AML Manual "
            "— unusual activity red flags (US supervisory guidance, not binding "
            "in India)"
        ),
        reference_url=RBI_KYC_MD,
        rule_text=(
            "Supervisory guidance identifies sudden, significant activity on a "
            "long-dormant account as an indicator warranting further review."
        ),
        threshold_applied=">= 90 days inactivity followed by a transaction >= Rs 1,00,000",
        why="A long-dormant counterparty suddenly moving large sums may indicate account takeover or misuse.",
        remediation="Re-verify the counterparty is still who the records say before releasing.",
    ),

    # ── Internal risk / data-quality controls ─────────────────────────────────

    "HIGH_VELOCITY": ComplianceRule(
        rule_id="HIGH_VELOCITY",
        title="Abnormal transaction velocity",
        severity="MEDIUM",
        action="FLAGGED",
        basis=ComplianceBasis.INTERNAL_POLICY,
        authority="AmongResolver internal risk policy",
        source_name="Internal anomaly detection standard",
        citation="Internal velocity threshold",
        reference_url=FFIEC_MANUAL,
        rule_text=(
            "No external rule prescribes this. Supervisory guidance treats "
            "unusual velocity as a general red flag; the parameters here are "
            "this system's own."
        ),
        threshold_applied="> 100 transactions OR > Rs 1,00,00,000 from one payer within 1 hour",
        why="A sudden burst of activity can indicate automated layering or account takeover.",
        remediation="Confirm the burst is expected activity for this counterparty.",
    ),

    "KEYWORD_ALERT": ComplianceRule(
        rule_id="KEYWORD_ALERT",
        title="High-risk narrative keyword",
        severity="MEDIUM",
        action="FLAGGED",
        basis=ComplianceBasis.INTERNAL_POLICY,
        authority="AmongResolver internal risk policy",
        source_name="Internal keyword screening list",
        citation="Internal lexical screening",
        reference_url=FFIEC_MANUAL,
        rule_text=(
            "No external rule prescribes keyword screening of payment "
            "narratives. This is a crude lexical signal with a high "
            "false-positive rate — ordinary words such as 'donation' or "
            "'crypto' trigger it. It is a prompt to look, never a finding."
        ),
        threshold_applied="Substring match against an internal high-risk term list",
        why="Free-text narratives occasionally reveal purpose that structured fields do not.",
        remediation=(
            "Read the full narrative in context before drawing any conclusion. "
            "Most hits are benign and must not be treated as evidence of wrongdoing."
        ),
    ),

    "CONCENTRATION_RISK": ComplianceRule(
        rule_id="CONCENTRATION_RISK",
        title="Counterparty concentration",
        severity="MEDIUM",
        action="FLAGGED",
        basis=ComplianceBasis.INTERNAL_POLICY,
        authority="AmongResolver internal risk policy",
        source_name="Internal concentration standard",
        citation="Internal concentration threshold",
        reference_url=FATF_RECS,
        rule_text=(
            "No external rule prescribes this threshold. Concentration is a "
            "prudential/risk consideration, not an AML obligation."
        ),
        threshold_applied="One payer accounting for > 90% of batch volume across > 1 transaction",
        why=(
            "One counterparty dominating a settlement batch concentrates risk. "
            "Often entirely legitimate for a business with a single large customer."
        ),
        remediation="Confirm the concentration matches the expected business model.",
    ),

    "DUPLICATE_TX": ComplianceRule(
        rule_id="DUPLICATE_TX",
        title="Identical transaction recorded twice",
        severity="LOW",
        action="FLAGGED",
        basis=ComplianceBasis.INTERNAL_POLICY,
        authority="AmongResolver internal data-quality control",
        source_name="Internal data-quality standard",
        citation="Internal duplicate detection",
        reference_url=FFIEC_MANUAL,
        rule_text=(
            "No external rule. This is a data-quality control, not an AML "
            "control, though duplicate submissions can also distort reporting."
        ),
        threshold_applied="Identical amount, timestamp, payer and payee",
        why="Usually double ingestion of the same record rather than financial crime.",
        remediation="Confirm whether this is a genuine second payment or a duplicated record.",
    ),
}


def get_rule(rule_id: str) -> ComplianceRule | None:
    return COMPLIANCE_RULES.get(rule_id)


def rulebook_summary() -> dict:
    """Counts by basis and action — the orientation an examiner wants first."""
    by_basis: Dict[str, int] = {}
    by_action: Dict[str, int] = {}
    for r in COMPLIANCE_RULES.values():
        by_basis[r.basis.value] = by_basis.get(r.basis.value, 0) + 1
        by_action[r.action] = by_action.get(r.action, 0) + 1
    return {
        "total_rules": len(COMPLIANCE_RULES),
        "by_basis": by_basis,
        "by_action": by_action,
    }
