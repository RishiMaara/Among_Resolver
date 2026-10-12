"""
Every figure marked in the documentation matches the result file it comes
from (docs/figures.json, scripts/sync_figures.py).

The same figures sat in nine documents, typed into each by hand, and a
re-measurement meant nine edits; one missed edit is a document that
contradicts the evidence it cites.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import sync_figures  # noqa: E402


def test_every_marked_figure_matches_its_source():
    stale = sync_figures.problems(write=False)
    assert not stale, (
        "Documents disagree with their result files. Run "
        "`python engine/scripts/sync_figures.py` to rewrite them:\n" + "\n".join(stale)
    )


def test_the_registry_resolves_and_the_documents_use_it():
    shown = sync_figures.figures()
    assert shown, "figures registry lists no figures"
    text = "".join(d.read_text(encoding="utf-8") for d in sync_figures.documents())
    unused = [name for name in shown
              if f"<!--fig:{name}-->" not in text and f"<!--fig:{name}:" not in text]
    assert not unused, f"registered but never shown: {unused}"
