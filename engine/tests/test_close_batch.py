"""
The batch-close agent, and the part of it that actually matters.

The brief this was built for asks for three things: throughput, a measured
match rate, and an honest list of what could not be resolved. The first two
are easy to report and easy to game — plant fewer problems in the fixture and
the match rate goes up. The third is the one that cannot be faked, so it is
what these tests are about.

An exception list is worth something only if the reason attached to each entry
tells a controller what to go and do. "Withheld for review" does not. "The six
payments referencing this settlement come to Rs 4,444.00, Rs 349.00 short"
does — that is a query they can run against their own system.

So: plant a specific problem, and assert the agent names THAT problem.
"""

import json
import logging
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SRC = Path(__file__).resolve().parents[1] / "src"
for p in (str(SRC), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(scope="module")
def cb():
    return pytest.importorskip("close_batch")


@pytest.fixture(scope="module")
def batch(cb, tmp_path_factory):
    """One generated batch, closed once, shared by every test below."""
    corpus = tmp_path_factory.mktemp("batch_close")
    # Use 80 settlements to ensure all 5 hazard types get planted:
    # With hazard_every=16, hazards appear at: 3, 19, 35, 51, 67
    # These map to hazards: [0], [1], [2], [3], [4] - all 5 types
    stats = cb.generate(str(corpus), settlements=80, seed=11, hazard_every=16)
    logging.disable(logging.WARNING)
    try:
        outcomes, run = cb.close_batch(str(corpus), window=5)
    finally:
        logging.disable(logging.NOTSET)
    return outcomes, run, stats, corpus


class TestTheBarTheBriefSets:
    def test_it_closes_a_batch_of_more_than_fifty_records(self, batch):
        _, run, stats, _ = batch
        assert stats["records"] > 50
        assert run["records"] > 50

    def test_every_settlement_is_attempted_not_sampled(self, batch):
        outcomes, _, stats, _ = batch
        # A match rate over a subset somebody chose is not a match rate.
        assert len(outcomes) == stats["settlements"]

    def test_it_reports_a_match_rate_and_it_is_not_perfect(self, batch):
        outcomes, _, _, _ = batch
        correct = [o for o in outcomes if o.closed and o.exact]
        assert correct, "nothing closed at all"
        # A fixture with problems in it must not close everything, or the
        # exception list is decorative.
        assert len(correct) < len(outcomes)

    def test_no_false_clears(self, batch):
        outcomes, _, _, _ = batch
        # The one number that must be zero whatever else moves.
        assert [o.settlement_id for o in outcomes if o.false_clear] == []


class TestTheExceptionList:
    def test_every_unresolved_settlement_carries_a_reason(self, batch):
        outcomes, _, _, _ = batch
        unresolved = [o for o in outcomes if not o.closed]
        assert unresolved, "the fixture planted problems but nothing was declined"
        for o in unresolved:
            assert o.reason and len(o.reason) > 20, (
                f"{o.settlement_id} was declined with no usable reason: {o.reason!r}"
            )

    def test_it_does_not_decline_settlements_that_have_no_problem(self, batch):
        outcomes, _, _, _ = batch
        planted = {o.settlement_id for o in outcomes if o.planted_hazard}
        declined = {o.settlement_id for o in outcomes if not o.closed}
        # False alarms are the other half of the cost. An agent that declines
        # everything has a perfect false-clear record and is worthless.
        assert not (declined - planted), (
            f"declined without a planted problem: {declined - planted}"
        )

    def test_a_missing_member_is_reported_as_a_gap_with_an_amount(self, batch):
        outcomes, _, _, _ = batch
        hits = [o for o in outcomes
                if o.planted_hazard == "missing_member" and not o.closed]
        # With settlements=80 and hazard_every=16, hazard at position 3
        # is guaranteed to be "missing_member" (HAZARDS[0])
        assert hits, "missing_member hazard should be planted at position 3"
        reason = hits[0].reason
        # Not "ambiguous". A number and a direction the controller can search on.
        assert "short of" in reason or "over" in reason
        assert "referencing this settlement" in reason

    def test_an_out_of_window_member_is_named_as_such(self, batch):
        outcomes, _, _, _ = batch
        hits = [o for o in outcomes
                if o.planted_hazard == "out_of_window" and not o.closed]
        # With settlements=80 and hazard_every=16, hazard at position 51
        # is guaranteed to be "out_of_window" (HAZARDS[3])
        assert hits, "out_of_window hazard should be planted at position 51"
        # The distinguishing case: the references are fine and the arithmetic
        # is fine, the members just are not reachable inside the lookback.
        assert "lookback" in hits[0].reason


class TestReproducibility:
    def test_the_same_seed_produces_the_same_batch(self, cb, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        cb.generate(str(a), settlements=8, seed=99, hazard_every=16)
        cb.generate(str(b), settlements=8, seed=99, hazard_every=16)
        assert (a / "gateway.csv").read_text() == (b / "gateway.csv").read_text()
        assert json.loads((a / "truth.json").read_text()) == \
               json.loads((b / "truth.json").read_text())

    def test_closing_the_same_batch_twice_gives_the_same_answer(self, cb, batch):
        outcomes, _, _, corpus = batch
        logging.disable(logging.WARNING)
        try:
            again, _ = cb.close_batch(str(corpus), window=5)
        finally:
            logging.disable(logging.NOTSET)
        # CP-SAT is pinned to one worker for exactly this reason: a match rate
        # that moves between runs is not a match rate.
        assert [(o.settlement_id, o.closed, o.exact) for o in outcomes] == \
               [(o.settlement_id, o.closed, o.exact) for o in again]
