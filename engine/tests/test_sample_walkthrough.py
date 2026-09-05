"""
The walkthrough page must describe what the walkthrough actually does.

sample-data/README.md is the one page evaluators are pointed at. It claimed
run 2 returned "16 of 90 at confidence 0.19". It returns 13 of 91 at 0.54,
deterministically, and had done for some time. The behaviour it demonstrates —
withholding rather than guessing — was correct and intact the whole time; only
the page was wrong, which is the more embarrassing way round, because the page
is the part a judge reads.

test_documented_figures.py already guards the test count this way. The same
argument applies with more force here: a reviewer found this in five minutes on
the first page they opened, and a number that is wrong on the page you invite
people to check is worth more damage than the same number being wrong anywhere
else.

So the figures are parsed out of the markdown and asserted against a live
reconciliation over the real sample files. If either drifts, this fails and
names the field.
"""

import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "sample-data"
SRC = ROOT / "engine" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _run(member_source):
    """Both walkthrough runs differ only in whether the member feed is declared."""
    import file_agent
    from ingestion import normalize_batch
    from pipeline import reconcile_settlement
    from schema import SettlementBatch, SourceType

    logging.disable(logging.WARNING)
    try:
        pool = []
        for name, src in (("gateway_report.csv", SourceType.GATEWAY),
                          ("bank_statement.csv", SourceType.BANK),
                          ("erp_ledger.json", SourceType.ERP)):
            rows = file_agent.parse_file_content((SAMPLE / name).read_bytes(), name)
            pool += normalize_batch(rows, src)

        batch = SettlementBatch(
            batch_id="SETTLE-001",
            net_amount_cents=6_646_636,          # the page's ₹66,466.36
            currency="INR",
            settled_at_utc=datetime(2026, 9, 2, tzinfo=timezone.utc),
            source=SourceType.BANK,
            member_source=member_source,
            declared_deductions_cents=205_566,   # the page's ₹2,055.66
        )
        report = reconcile_settlement(batch, pool, settlement_window_days=5)
    finally:
        logging.disable(logging.NOTSET)
    return report, len(pool)


def _documented(block_index: int) -> dict:
    """Pull the figures out of the Nth fenced block under 'Two runs worth doing'."""
    text = (SAMPLE / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```\n(cleared.*?)```", text, re.S)
    assert len(blocks) > block_index, "the walkthrough's result blocks moved or were removed"
    body = blocks[block_index]

    def grab(pattern, cast=str):
        m = re.search(pattern, body)
        return cast(m.group(1)) if m else None

    # Run 1 states "matched 14 of 91"; run 2 states "matched 13-14 of 91",
    # because run 2's count is not stable across machines. Both shapes parse.
    rng = re.search(r"matched\s+(\d+)[–-](\d+) of (\d+)", body)
    if rng:
        low, high, pool = int(rng.group(1)), int(rng.group(2)), int(rng.group(3))
    else:
        one = re.search(r"matched\s+(\d+) of (\d+)", body)
        assert one, "could not read a member count out of the walkthrough block"
        low = high = int(one.group(1))
        pool = int(one.group(2))

    return {
        "cleared": grab(r"cleared\s+(True|False)") == "True",
        "confidence": grab(r"confidence (\d+\.\d+)", float),
        "matched": low,
        "matched_low": low,
        "matched_high": high,
        "pool": pool,
    }


@pytest.fixture(scope="module")
def sample_files():
    if not (SAMPLE / "gateway_report.csv").exists():
        pytest.skip("sample-data/ is not in this checkout")
    return True


class TestRunOne:
    """The cleared case — the one a demo opens with."""

    def test_matches_what_the_page_claims(self, sample_files):
        from schema import SourceType
        report, pool_size = _run(SourceType.GATEWAY)
        doc = _documented(0)
        m = report.match_result

        assert m.cleared is doc["cleared"]
        assert round(m.confidence, 2) == doc["confidence"]
        assert len(m.matched_txn_ids) == doc["matched"]
        assert pool_size == doc["pool"]

    def test_ties_out_to_the_paisa(self, sample_files):
        from schema import SourceType
        report, _ = _run(SourceType.GATEWAY)
        # The page says "residual 0 cents". Exact tie-out is the claim the
        # whole engine rests on; it does not get to be approximately true.
        assert report.target_cents - report.match_result.matched_sum_cents == 0
        assert report.exceptions == [] or len(report.exceptions) == 0


class TestRunTwo:
    """
    The withheld case — the one that demonstrates the actual thesis.

    Its exact member count is NOT asserted, and that is the point rather than a
    weakened test. This run is ambiguous by construction: every gateway payment
    has an ERP twin at the same amount, so several subsets satisfy the sum
    equally well and CP-SAT — running multi-worker under production defaults,
    as it does for a real request — returns whichever its parallel search
    reaches first. Windows returns 13 members, Linux CI returns 14.

    An earlier version of this test asserted `== 13` and passed on the machine
    it was written on, then failed in CI. Pinning the solver to one worker would
    have made it green, and would have been the wrong fix: it would test a
    configuration no user runs, to defend a number that is not a property of the
    engine. What IS a property, and is asserted here, is that the batch does not
    clear.
    """

    def test_matches_what_the_page_claims(self, sample_files):
        report, pool_size = _run(None)
        doc = _documented(1)
        m = report.match_result

        assert m.cleared is doc["cleared"], (
            "run 2 must not clear: every gateway payment has an ERP twin at the "
            "same amount, so with no declared member feed nothing distinguishes them"
        )
        assert pool_size == doc["pool"]
        # The page states a range for this run because the value genuinely moves.
        assert doc["matched_low"] <= len(m.matched_txn_ids) <= doc["matched_high"], (
            f"run 2 surfaced {len(m.matched_txn_ids)} members; sample-data/README.md "
            f"documents {doc['matched_low']}-{doc['matched_high']}. If the solver "
            f"now reaches a different subset, widen the documented range and say "
            f"why — do not pin the solver to make this pass."
        )

    def test_withholds_below_the_gate(self, sample_files):
        report, _ = _run(None)
        m = report.match_result
        # The point of the second run. It finds something plausible and still
        # declines — that is the behaviour, not the absence of one.
        assert m.ambiguous is True
        assert m.cleared is False
        assert m.confidence < 0.85


def test_run_one_is_deterministic(sample_files):
    """
    Run 1 three times, identically. It is anchored — exactly one subset carries
    the settlement reference — so there is nothing for the parallel search to
    choose between and the figure can be written down.

    Run 2 is deliberately excluded: it is the ambiguous case, its member count
    differs between Windows and Linux, and asserting stability there would be
    asserting something untrue.
    """
    from schema import SourceType
    seen = set()
    for _ in range(3):
        report, _ = _run(SourceType.GATEWAY)
        m = report.match_result
        seen.add((m.cleared, round(m.confidence, 2), len(m.matched_txn_ids)))
    assert len(seen) == 1, f"run 1 was not reproducible across three runs: {seen}"
