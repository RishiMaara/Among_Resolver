import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

import pytest
from datetime import datetime, timezone, timedelta
from schema import NormalizedTxn, SourceType, TzConfidence, ComplianceStatus
import compliance_agent

@pytest.fixture
def base_time():
    return datetime(2026, 8, 1, tzinfo=timezone.utc)

def make_txn(amount_cents: int, timestamp: datetime, payer: str, payee: str = "", is_cash=False, is_wire=False) -> NormalizedTxn:
    return NormalizedTxn(
        source=SourceType.GATEWAY,
        source_txn_id=f"tx_{timestamp.timestamp()}",
        ref_id_canonical="ref123",
        amount_cents=amount_cents,
        currency="INR",
        timestamp_utc=timestamp,
        tz_confidence=TzConfidence.HIGH,
        payer_id=payer,
        payee_id=payee,
        is_cash=is_cash,
        is_wire_transfer=is_wire
    )

def test_structuring_rule(base_time):
    txns = [
        make_txn(49900 * 100, base_time, "payerA"),
        make_txn(49800 * 100, base_time + timedelta(hours=1), "payerA"),
        make_txn(49950 * 100, base_time + timedelta(hours=2), "payerA")
    ]
    safe, blocked = compliance_agent.scan(txns)
    assert len(safe) == 3
    assert len(blocked) == 0
    # Should be flagged
    assert all(t.compliance_status == ComplianceStatus.FLAGGED for t in safe)

def test_ctr_single_rule(base_time):
    txns = [make_txn(10_000_000 * 100, base_time, "payerB", is_cash=True)]
    safe, blocked = compliance_agent.scan(txns)
    assert safe[0].compliance_status == ComplianceStatus.FLAGGED

def test_sanctions_rule(base_time, monkeypatch):
    """
    The RULE, not the roster.

    This asserted that "BIN_LADEN" is designated, which was true only of the
    four-name illustrative list. Once a real UN Consolidated List is fetched —
    3,422 identifiers, regenerated daily — the test broke, not because
    screening stopped working but because it was pinned to the contents of a
    list that is supposed to change.

    A screening test should prove the mechanism blocks a designated party.
    Which parties are designated is the list's business, and it is refetched
    precisely so nobody hard-codes it.
    """
    designated = compliance_agent.normalize_party("TEST DESIGNATED PARTY")
    monkeypatch.setattr(compliance_agent, "SANCTIONS_LIST", {designated})

    txns = [make_txn(100 * 100, base_time, "TEST DESIGNATED PARTY", "payeeC")]
    safe, blocked = compliance_agent.scan(txns)
    assert len(safe) == 0
    assert len(blocked) == 1
    assert blocked[0].compliance_status == ComplianceStatus.BLOCKED


def test_sanctions_rule_passes_an_undesignated_party(base_time, monkeypatch):
    """The other half: screening must not block everybody."""
    monkeypatch.setattr(
        compliance_agent, "SANCTIONS_LIST",
        {compliance_agent.normalize_party("SOMEONE ELSE ENTIRELY")},
    )
    txns = [make_txn(100 * 100, base_time, "ORDINARY CUSTOMER", "payeeC")]
    safe, blocked = compliance_agent.scan(txns)
    assert len(blocked) == 0
    assert len(safe) == 1


def test_real_sanctions_list_is_loaded_and_not_illustrative():
    """
    The list in use is the real one.

    An illustrative four-name list and a real 3,422-entry one behave
    identically on every test that does not look — which is exactly how a demo
    ends up claiming screening it does not do.

    Absent and stale are different failures, and this separates them.

    The fetched list is gitignored on purpose: a copy committed here is stale a
    week later, and stale screening is the failure that matters. But that meant
    a fresh clone had no list, fell back to the four illustrative names, and
    failed this test — so anyone cloning the repository and running the suite,
    which is exactly what a reviewer does, met a red failure caused by a file
    they were never given. The code was fine. The precondition was missing and
    nothing said so.

    Missing list: skip, and say which command produces it. Present but
    illustrative: fail, because that is the regression this test exists for.
    """
    prov = compliance_agent.sanctions_provenance()

    if prov.get("is_illustrative") and not prov.get("list_generated"):
        pytest.skip(
            "No sanctions list has been fetched into engine/data/sanctions/ "
            "(it is gitignored, so a fresh clone has none). Run "
            "`python engine/scripts/fetch_sanctions_list.py` and re-run. CI "
            "fetches it before pytest, so this assertion does run there."
        )

    assert prov["is_illustrative"] is False, prov
    assert prov["entry_count"] > 1000, prov

def test_velocity_rule(base_time):
    # 120 txns in 1 hour
    txns = [make_txn(100 * 100, base_time + timedelta(minutes=i*0.1), "payerC") for i in range(120)]
    safe, blocked = compliance_agent.scan(txns)
    assert len(safe) == 120
    assert all(t.compliance_status == ComplianceStatus.FLAGGED for t in safe)

def test_dormant_spike(base_time):
    txns = [
        make_txn(10 * 100, base_time - timedelta(days=100), "payerD"),
        make_txn(200_000 * 100, base_time, "payerD")
    ]
    safe, blocked = compliance_agent.scan(txns)
    assert safe[1].compliance_status == ComplianceStatus.FLAGGED

def test_extreme_amount(base_time):
    txns = [make_txn(60_000_000 * 100, base_time, "payerE")] # 6 Cr
    safe, blocked = compliance_agent.scan(txns)
    assert len(safe) == 0
    assert len(blocked) == 1
    assert blocked[0].compliance_status == ComplianceStatus.BLOCKED


class TestCircularFlowDetection:
    """
    A -> B -> C -> A with no commercial purpose is a layering pattern.
    A -> B -> A -> B -> A is one pair of parties invoicing each other, and
    reporting that as layering buries the real signal in noise.
    """

    def _txn(self, tid, payer, payee, hours, amount=100_000):
        from schema import NormalizedTxn, SourceType, TzConfidence
        from datetime import datetime, timedelta, timezone
        base = datetime(2026, 8, 20, tzinfo=timezone.utc)
        return NormalizedTxn(
            source=SourceType.GATEWAY, source_txn_id=tid,
            ref_id_canonical=f"REF{tid}", amount_cents=amount, currency="INR",
            timestamp_utc=base + timedelta(hours=hours),
            tz_confidence=TzConfidence.HIGH, payer_id=payer, payee_id=payee,
        )

    def test_detects_a_three_party_ring(self):
        import compliance_agent
        from schema import ComplianceStatus
        txns = [
            self._txn("T1", "A", "B", 0),
            self._txn("T2", "B", "C", 1),
            self._txn("T3", "C", "A", 2),
        ]
        compliance_agent.check_circular_flow(txns)
        assert any(t.compliance_status is ComplianceStatus.FLAGGED for t in txns)

    def test_two_parties_bouncing_funds_is_not_a_ring(self):
        """Distinct intermediaries only — otherwise ordinary back-and-forth
        billing between two counterparties is reported as laundering."""
        import compliance_agent
        from schema import ComplianceStatus
        txns = [
            self._txn("T1", "A", "B", 0),
            self._txn("T2", "B", "A", 1),
            self._txn("T3", "A", "B", 2),
            self._txn("T4", "B", "A", 3),
        ]
        compliance_agent.check_circular_flow(txns)
        assert all(t.compliance_status is ComplianceStatus.PASS for t in txns)

    def test_a_ring_spread_beyond_24h_is_not_flagged(self):
        import compliance_agent
        from schema import ComplianceStatus
        txns = [
            self._txn("T1", "A", "B", 0),
            self._txn("T2", "B", "C", 1),
            self._txn("T3", "C", "A", 60),   # far outside the window
        ]
        compliance_agent.check_circular_flow(txns)
        assert all(t.compliance_status is ComplianceStatus.PASS for t in txns)

    def test_legs_must_advance_in_time(self):
        """Money cannot flow back through a leg that happened earlier."""
        import compliance_agent
        from schema import ComplianceStatus
        txns = [
            self._txn("T1", "A", "B", 5),
            self._txn("T2", "B", "C", 3),   # before T1
            self._txn("T3", "C", "A", 1),   # before T2
        ]
        compliance_agent.check_circular_flow(txns)
        assert all(t.compliance_status is ComplianceStatus.PASS for t in txns)


class TestSanctionsListProvenance:
    """
    SANCTIONS_HIT is the only rule that blocks funds on a statutory basis, so
    where its list comes from matters more than any other configuration.
    """

    def test_falls_back_loudly_rather_than_screening_nothing(self, tmp_path, monkeypatch):
        """A mistyped path must not mean every counterparty silently passes."""
        import compliance_agent
        monkeypatch.setenv("SANCTIONS_LIST_PATH", str(tmp_path / "nope.txt"))
        loaded = compliance_agent._load_sanctions_list()
        assert loaded == compliance_agent._ILLUSTRATIVE_SANCTIONS

    def test_empty_file_does_not_disable_screening(self, tmp_path, monkeypatch):
        import compliance_agent
        p = tmp_path / "empty.txt"
        p.write_text("# only comments\n\n", encoding="utf-8")
        monkeypatch.setenv("SANCTIONS_LIST_PATH", str(p))
        assert compliance_agent._load_sanctions_list() == compliance_agent._ILLUSTRATIVE_SANCTIONS

    def test_loads_a_real_list_when_configured(self, tmp_path, monkeypatch):
        import compliance_agent
        p = tmp_path / "sdn.txt"
        p.write_text("# UN consolidated extract\nACME_BAD_CORP\nsome_person\n", encoding="utf-8")
        monkeypatch.setenv("SANCTIONS_LIST_PATH", str(p))
        loaded = compliance_agent._load_sanctions_list()
        # Stored folded: punctuation becomes spacing so the list can be
        # compared against real counterparty names.
        assert loaded == {"ACME BAD CORP", "SOME PERSON"}

    def test_screening_matches_a_real_name_despite_punctuation(self, tmp_path, monkeypatch):
        """
        The failure this guards against is a screen that CANNOT hit.

        Before both sides were folded, screening compared a raw payer_id to
        list entries by exact equality. Point that at a real list of names and
        every counterparty passes, because "Al-Qaida" is not "AL QAIDA" — a
        clean screen against a list the engine is structurally unable to match.
        """
        import compliance_agent
        from schema import ComplianceStatus

        p = tmp_path / "list.txt"
        p.write_text("AL QAIDA\n", encoding="utf-8")
        monkeypatch.setenv("SANCTIONS_LIST_PATH", str(p))
        monkeypatch.setattr(compliance_agent, "SANCTIONS_LIST",
                            compliance_agent._load_sanctions_list())

        for spelling in ("Al-Qaida", "al qaida", "AL  QAIDA", "Al.Qaida"):
            txn = _party_txn("t1", payee=spelling)
            compliance_agent.check_sanctions([txn])
            assert txn.compliance_status is ComplianceStatus.BLOCKED, spelling

        clean = _party_txn("t2", payee="Acme Retail Pvt Ltd")
        compliance_agent.check_sanctions([clean])
        assert clean.compliance_status is ComplianceStatus.PASS

    def test_normalisers_agree_across_modules(self):
        """
        The fetch script writes the list; the agent reads it. If their two
        normalisers drift the list stops matching itself, and nothing fails
        loudly — screening just quietly returns no hits.
        """
        import importlib.util
        import pathlib
        import compliance_agent

        script = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "fetch_sanctions_list.py"
        spec = importlib.util.spec_from_file_location("_fetch_sanctions", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        for sample in ("Al-Qaida", "  ISIL   (Da'esh) ", "Mohammed_Omar", "ACME Pvt. Ltd.", ""):
            assert mod.normalize(sample) == compliance_agent.normalize_party(sample), sample

    def test_provenance_says_when_the_list_is_illustrative(self, monkeypatch):
        """
        A block from the demo list and a block from the UN list look identical
        in a report otherwise, and they mean entirely different things.
        """
        import compliance_agent

        monkeypatch.delenv("SANCTIONS_LIST_PATH", raising=False)
        monkeypatch.setattr(compliance_agent, "DEFAULT_SANCTIONS_PATHS", ())
        compliance_agent._load_sanctions_list()
        assert compliance_agent.sanctions_provenance()["is_illustrative"] is True

    def test_auto_discovers_a_fetched_list_without_env_config(self, tmp_path, monkeypatch):
        import compliance_agent

        p = tmp_path / "un_consolidated.txt"
        p.write_text(
            "# source: https://scsanctions.un.org/resources/xml/en/consolidated.xml\n"
            "# generated: 2026-08-01T00:00:00\n"
            "# retrieved: 2026-08-30T00:00:00+00:00\n"
            "SOME DESIGNATED ENTITY\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("SANCTIONS_LIST_PATH", raising=False)
        monkeypatch.setattr(compliance_agent, "DEFAULT_SANCTIONS_PATHS", (p,))
        loaded = compliance_agent._load_sanctions_list()

        assert loaded == {"SOME DESIGNATED ENTITY"}
        prov = compliance_agent.sanctions_provenance()
        assert prov["is_illustrative"] is False
        assert prov["retrieved"] == "2026-08-30T00:00:00+00:00"

    def test_stale_list_is_reported_not_silently_trusted(self, tmp_path, monkeypatch, caplog):
        """A list retrieved months ago passes parties designated since."""
        import compliance_agent

        p = tmp_path / "un_consolidated.txt"
        p.write_text(
            "# retrieved: 2020-01-01T00:00:00+00:00\nSOME DESIGNATED ENTITY\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("SANCTIONS_LIST_PATH", str(p))
        with caplog.at_level("WARNING"):
            compliance_agent._load_sanctions_list()
        assert any("retrieved" in r.message and "NOT be screened" in r.message
                   for r in caplog.records)


def _party_txn(tid, payer="ACME PAYER", payee="ACME PAYEE"):
    """A transaction whose only interesting property is its counterparties."""
    from schema import NormalizedTxn, SourceType, TzConfidence
    from datetime import datetime, timezone
    return NormalizedTxn(
        source=SourceType.GATEWAY, source_txn_id=tid,
        ref_id_canonical=f"REF{tid}", amount_cents=100_00, currency="INR",
        timestamp_utc=datetime(2026, 8, 20, tzinfo=timezone.utc),
        tz_confidence=TzConfidence.HIGH, payer_id=payer, payee_id=payee,
    )
