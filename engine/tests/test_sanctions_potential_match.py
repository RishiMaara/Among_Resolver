"""
A name alone is a potential match, not a confirmed one.

Exact hits on a listed name, or on an alias the UN rates better than "Low",
block. A near spelling (85% similar) or a name the list carries only as a
low-quality alias flags for a person instead: blocking on them held whole
payouts for common customer names ("MOHAMMAD ALI" is 85% like a listed
"MOHAMMAD WALI"; "MUHAMMAD YUNUS" is a low-quality alias). The flag is
statutory and HIGH, so the engine never closes it on its own.
"""
from collections import defaultdict
from datetime import datetime, timezone

import pytest

import auto_disposition
import compliance_agent
from schema import ComplianceStatus, NormalizedTxn, SourceType, TzConfidence


@pytest.fixture
def small_list(tmp_path, monkeypatch):
    listing = tmp_path / "un_consolidated.txt"
    listing.write_text("# test list\nMOHAMMAD WALI\nMUHAMMAD YUNUS\nTARIQ AZIZ\n", encoding="utf-8")
    (tmp_path / "un_consolidated_low_quality.txt").write_text("# low\nMUHAMMAD YUNUS\n", encoding="utf-8")
    monkeypatch.setenv("SANCTIONS_LIST_PATH", str(listing))
    names = compliance_agent._load_sanctions_list()
    monkeypatch.setattr(compliance_agent, "SANCTIONS_LIST", names)
    monkeypatch.setattr(compliance_agent, "SANCTIONS_LOW_QUALITY", compliance_agent._load_low_quality_aliases())
    index = defaultdict(list)
    for n in names:
        index[n[:2]].append(n)
    monkeypatch.setattr(compliance_agent, "SANCTIONS_PREFIX_INDEX", index)


def _screen(name):
    t = NormalizedTxn(source=SourceType.GATEWAY, source_txn_id="t", ref_id_canonical="R",
                      amount_cents=200_00, currency="INR",
                      timestamp_utc=datetime(2026, 9, 1, tzinfo=timezone.utc),
                      tz_confidence=TzConfidence.HIGH, payer_id=name)
    compliance_agent.check_sanctions([t])
    return t


def test_an_exact_listed_name_still_blocks(small_list):
    t = _screen("Tariq Aziz")
    assert t.compliance_status is ComplianceStatus.BLOCKED
    assert [f.rule_id for f in t.compliance_findings] == ["SANCTIONS_HIT"]


def test_a_near_spelling_is_flagged_for_a_person_not_blocked(small_list):
    t = _screen("Mohammad Ali")
    assert t.compliance_status is ComplianceStatus.FLAGGED
    assert [f.rule_id for f in t.compliance_findings] == ["SANCTIONS_POTENTIAL_MATCH"]


def test_a_low_quality_alias_is_flagged_not_blocked(small_list):
    t = _screen("Muhammad Yunus")
    assert t.compliance_status is ComplianceStatus.FLAGGED
    assert [f.rule_id for f in t.compliance_findings] == ["SANCTIONS_POTENTIAL_MATCH"]


def test_a_potential_match_is_never_closed_by_the_engine(small_list):
    f = _screen("Mohammad Ali").compliance_findings[0]
    verdict = auto_disposition.disposition({
        "rule_id": f.rule_id, "severity": f.severity, "action": f.action,
        "basis": f.basis.value, "observed": f.observed,
    })
    assert verdict["disposition"] == "human"


def test_an_unrelated_name_passes(small_list):
    assert _screen("Rahul Sharma").compliance_status is ComplianceStatus.PASS
