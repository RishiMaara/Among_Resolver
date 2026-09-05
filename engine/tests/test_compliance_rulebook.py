"""
Rulebook integrity tests.

The rulebook is the artifact a bank's compliance function or a regulator
inspects. Its failure mode is not a crash — it is quietly saying something
untrue about the law. These tests guard that.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ast

import compliance_agent
import compliance_rulebook
from compliance_rulebook import COMPLIANCE_RULES, rulebook_summary
from schema import ComplianceBasis


def _logged_rule_ids(src: str) -> set[str]:
    """
    Collect the rule ids passed as the 2nd argument to _log().

    Parsed via AST rather than regex: the first argument can be a subscript
    (txns[i]) or a nested list literal ([seen[key], t]), and a regex that
    tries to skip over it gets those wrong in both directions — which would
    make this test either miss real gaps or invent fake ones.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "_log"):
            continue
        if len(node.args) < 2:
            continue
        second = node.args[1]
        if isinstance(second, ast.Constant) and isinstance(second.value, str):
            found.add(second.value)
    return found


class TestRulebookCoverage:

    def test_every_rule_logged_by_the_agent_is_registered(self):
        """
        A rule that fires without a registry entry produces an unexplained
        block — the exact failure this whole feature exists to prevent.
        Scrape the rule ids the agent actually passes to _log and require
        each to be registered.
        """
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "src", "compliance_agent.py"),
            encoding="utf-8",
        ).read()
        used = _logged_rule_ids(src)
        assert used, "expected to find _log calls to scrape"

        missing = used - set(COMPLIANCE_RULES)
        assert not missing, f"rules fire but are not in the rulebook: {sorted(missing)}"

    def test_no_orphan_rules_in_registry(self):
        """A registered rule that can never fire is misleading documentation."""
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "src", "compliance_agent.py"),
            encoding="utf-8",
        ).read()
        used = _logged_rule_ids(src)
        orphans = set(COMPLIANCE_RULES) - used
        assert not orphans, f"registered but unreachable: {sorted(orphans)}"


class TestRulebookHonesty:

    def test_every_rule_declares_a_valid_basis(self):
        for rule_id, rule in COMPLIANCE_RULES.items():
            assert isinstance(rule.basis, ComplianceBasis), rule_id

    def test_internal_policy_rules_never_claim_an_external_authority(self):
        """
        The core honesty guarantee: an internal threshold must not be
        attributed to a statute or a regulator. If someone later relabels the
        Rs 5 crore ceiling as statutory, or points its authority at RBI/FIU,
        this fails.
        """
        for rule_id, rule in COMPLIANCE_RULES.items():
            if rule.basis is not ComplianceBasis.INTERNAL_POLICY:
                continue
            authority = rule.authority.lower()
            for regulator in ("rbi", "fiu", "fatf", "ffiec", "pmla", "sebi", "un "):
                assert regulator not in authority, (
                    f"{rule_id} is internal_policy but claims authority "
                    f"'{rule.authority}'"
                )
            assert "internal" in authority, (
                f"{rule_id} is internal_policy but its authority "
                f"'{rule.authority}' does not say so"
            )

    def test_internal_rules_state_that_no_external_rule_imposes_them(self):
        for rule_id, rule in COMPLIANCE_RULES.items():
            if rule.basis is not ComplianceBasis.INTERNAL_POLICY:
                continue
            text = rule.rule_text.lower()
            assert ("no statutory" in text or "no external rule" in text), (
                f"{rule_id} is internal_policy but rule_text does not say that "
                f"no external rule imposes it"
            )

    def test_the_rs_5cr_block_is_not_presented_as_law(self):
        """Specific guard on the rule that actually fires in the demo dataset."""
        rule = COMPLIANCE_RULES["LIMIT_EXCEEDED"]
        assert rule.basis is ComplianceBasis.INTERNAL_POLICY
        assert "not a statutory" in rule.rule_text.lower() or \
               "no statutory" in rule.rule_text.lower()

    def test_statutory_rules_cite_a_named_source_and_provision(self):
        for rule_id, rule in COMPLIANCE_RULES.items():
            if rule.basis is not ComplianceBasis.STATUTORY:
                continue
            assert rule.source_name.strip(), f"{rule_id} missing source_name"
            assert rule.citation.strip(), f"{rule_id} missing citation"
            assert "internal" not in rule.source_name.lower(), rule_id


class TestRulebookCompleteness:

    def test_all_fields_populated(self):
        required = [
            "title", "severity", "action", "authority", "source_name",
            "citation", "reference_url", "rule_text", "threshold_applied",
            "why", "remediation",
        ]
        for rule_id, rule in COMPLIANCE_RULES.items():
            for fld in required:
                assert getattr(rule, fld).strip(), f"{rule_id}.{fld} is empty"

    def test_reference_urls_are_https_and_plausible(self):
        for rule_id, rule in COMPLIANCE_RULES.items():
            assert rule.reference_url.startswith("https://"), rule_id

    def test_action_and_severity_are_known_values(self):
        for rule_id, rule in COMPLIANCE_RULES.items():
            assert rule.action in {"FLAGGED", "BLOCKED"}, rule_id
            assert rule.severity in {"LOW", "MEDIUM", "HIGH"}, rule_id

    def test_blocking_rules_are_high_severity(self):
        for rule_id, rule in COMPLIANCE_RULES.items():
            if rule.action == "BLOCKED":
                assert rule.severity == "HIGH", (
                    f"{rule_id} blocks funds but is only {rule.severity} severity"
                )

    def test_summary_counts_match_the_registry(self):
        s = rulebook_summary()
        assert s["total_rules"] == len(COMPLIANCE_RULES)
        assert sum(s["by_basis"].values()) == len(COMPLIANCE_RULES)
        assert sum(s["by_action"].values()) == len(COMPLIANCE_RULES)

    def test_scope_note_states_what_the_engine_does_not_do(self):
        """
        Not a 'trial' caveat — these are permanent facts about the engine.
        It screens a static list, it files nothing, and internal_policy rules
        are not law. If any of that changes the note must change with it.
        """
        d = compliance_rulebook.SCOPE_NOTE.lower()
        assert "advisory" in d
        assert "static" in d          # the sanctions list is not live
        assert "internal_policy" in d


class TestIndianCurrencyFormatting:
    """A reviewer checking Rs 6cr against a Rs 5cr ceiling must not have to
    count digits — amounts use lakh/crore grouping."""

    def test_crore_grouping(self):
        assert compliance_agent._inr(6_00_00_000 * 100) == "₹6,00,00,000.00"

    def test_lakh_grouping(self):
        assert compliance_agent._inr(1_00_000 * 100) == "₹1,00,000.00"

    def test_thousands_and_small_values(self):
        assert compliance_agent._inr(1234_56) == "₹1,234.56"
        assert compliance_agent._inr(0) == "₹0.00"
        assert compliance_agent._inr(50_00) == "₹50.00"


# ── citation audit ────────────────────────────────────────────────────────
#
# A compliance surface is only worth the reader's ability to check it, so
# every rule has to carry a citation that actually reaches them. These pin the
# audit rather than leaving it as something someone remembers to redo.

def _rules():
    import compliance_rulebook as m
    return list(m.COMPLIANCE_RULES.values())


def test_every_rule_carries_a_complete_citation():
    for r in _rules():
        for field in ("rule_id", "title", "severity", "action", "basis",
                      "authority", "source_name", "citation", "reference_url",
                      "threshold_applied", "why", "remediation"):
            assert str(getattr(r, field, "") or "").strip(), \
                f"{r.rule_id} is missing {field}"


def test_a_statutory_rule_cites_indian_law():
    """
    The three statutory rules are the only ones claiming legal force, so each
    must name the Indian instrument it rests on. A statutory claim resting on
    a foreign regulator's handbook would be the single worst error this
    rulebook could make.
    """
    from compliance_rulebook import ComplianceBasis
    indian = ("PMLA", "PML Rules", "FIU-IND", "UAPA", "RBI",
              "Unlawful Activities")
    statutory = [r for r in _rules() if r.basis == ComplianceBasis.STATUTORY]
    assert len(statutory) >= 3
    for r in statutory:
        text = f"{r.citation} {r.authority}"
        assert any(k in text for k in indian), \
            f"{r.rule_id} claims statutory force without an Indian instrument"


def test_no_rule_rests_on_a_foreign_handbook_alone():
    """
    DORMANT_ACCOUNT_SPIKE used to cite only the FFIEC BSA/AML Manual, which is
    a US bank examination handbook with no force in India. The pattern it
    describes is fine to borrow — laundering typologies are not nationally
    specific — but a rule applied to an Indian merchant needs an anchor that
    reaches them.
    """
    foreign_only = ("FFIEC", "OFAC", "31 CFR", "FinCEN", "BSA/AML")
    reachable = ("PMLA", "PML Rules", "FIU-IND", "UAPA", "RBI", "FATF",
                 "UNSC", "UN Security", "AmongResolver internal")
    for r in _rules():
        text = f"{r.citation} {r.authority}"
        if any(k in text for k in foreign_only):
            assert any(k in text for k in reachable), (
                f"{r.rule_id} cites only foreign supervisory material; add an "
                f"Indian or international anchor or mark it internal_policy"
            )


def test_the_sanctions_rule_states_the_limitation_that_is_actually_true():
    """
    Its threshold text used to announce a demo limitation — "a small static
    illustrative list" — that stopped being true once a real 3,422-entry UN
    list was fetched. Understating the engine in the one place a reviewer
    looks hardest is its own kind of inaccuracy.
    """
    r = next(x for x in _rules() if x.rule_id == "SANCTIONS_HIT")
    t = r.threshold_applied
    assert "illustrative" not in t.lower(), "stale demo caveat is back"
    # What IS still true, and matters more.
    assert "exact" in t.lower()
    assert "fuzzy" in t.lower()


def test_an_internal_rule_never_claims_an_outside_authority():
    """The distinction the whole screen rests on."""
    from compliance_rulebook import ComplianceBasis
    for r in _rules():
        if r.basis == ComplianceBasis.INTERNAL_POLICY:
            assert "AmongResolver" in r.authority, \
                f"{r.rule_id} is internal but names an external authority"
