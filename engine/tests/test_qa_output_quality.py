"""
The Q&A answer has to be readable, and it has to be finished.

Two faults were reported together and had nothing to do with each other.

Asterisks on screen. The results panel renders the answer with
`whitespace-pre-wrap`, which is deliberate — it preserves paragraphs without
letting model output become markup. But the prompt never said what format to
answer in, so the model wrote markdown out of habit and `**Cleared.**` reached
the user as four asterisks in the middle of a sentence about money.

Sentences that stopped halfway. `gemini-flash-latest` is a 2.5-series model
with thinking on by default, and thinking tokens are charged against
max_output_tokens. Measured against the live API: 639 thinking tokens to
produce an 85-token answer — 88% of the budget gone before a word was written.
With a whole settlement report as grounding the model thinks harder, runs out,
and the answer is cut mid-word. Nothing checked finish_reason, so a truncated
string was returned as though it were complete.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import settlement_qa as qa  # noqa: E402


class TestNoMarkdownReachesTheScreen:
    def test_bold_is_unwrapped_not_deleted(self):
        # The word must survive; only the asterisks go.
        assert qa.strip_markdown("**Cleared.** It tied out.") == "Cleared. It tied out."

    def test_backticks_are_removed(self):
        assert qa.strip_markdown("`summary.cleared` is true") == "summary.cleared is true"

    def test_bullets_become_plain_lines(self):
        out = qa.strip_markdown("Totals:\n- 14 payments\n- residual 0")
        assert "- " not in out
        assert "14 payments" in out and "residual 0" in out

    def test_headings_lose_their_hashes(self):
        assert qa.strip_markdown("## Fees\nThey were 2,055.66.").startswith("Fees")

    def test_italics_are_unwrapped(self):
        assert qa.strip_markdown("that is *not* a legal threshold") == \
            "that is not a legal threshold"

    def test_a_lone_asterisk_in_prose_is_left_alone(self):
        # Not everything with an asterisk is markdown, and mangling a real
        # sentence to tidy formatting would be the worse bug.
        text = "The 2 * 3 calculation is unrelated."
        assert qa.strip_markdown(text) == text

    def test_a_clean_answer_is_returned_unchanged(self):
        text = "This settlement cleared. Fourteen payments tie out to zero paise."
        assert qa.strip_markdown(text) == text

    def test_amounts_and_ids_survive_intact(self):
        # The whole point of the answer is its figures.
        out = qa.strip_markdown("**Matched:** 14, gross `6852202` cents, id `pay_001`.")
        assert "14" in out and "6852202" in out and "pay_001" in out


class TestTruncationIsNotPassedOffAsComplete:
    def test_a_cut_off_answer_is_flagged(self, monkeypatch):
        """
        finish_reason MAX_TOKENS must not return silently. The caller cannot
        otherwise distinguish a short complete answer from one that stopped
        halfway, and a half sentence about money reads as a system fault.
        """
        import llm_provider

        class FakeCandidate:
            finish_reason = "FinishReason.MAX_TOKENS"

        class FakeResponse:
            text = "The settlement cleared because the fourteen payments sum to"
            candidates = [FakeCandidate()]

        class FakeModels:
            def generate_content(self, **_):
                return FakeResponse()

        class FakeClient:
            models = FakeModels()

        # genai is imported inside generate(), so the module itself is the
        # patch target rather than any attribute on llm_provider.
        from google import genai

        monkeypatch.setattr(llm_provider, "api_key", lambda: "k")
        monkeypatch.setattr(genai, "Client", lambda **_: FakeClient())
        out = llm_provider.generate("q", max_output_tokens=10)
        assert out is not None
        assert "truncated" in out.lower()

    def test_thinking_is_disabled_by_default(self):
        """
        The default has to be no thinking, not merely a supported option.
        Neither agent here reasons — one maps a column name, the other restates
        figures it was handed — and leaving thinking on spends the answer's
        budget on a trace nobody reads.
        """
        import inspect

        import llm_provider
        sig = inspect.signature(llm_provider.generate)
        assert sig.parameters["thinking_budget"].default == 0


class TestThePromptAsksForProse:
    def test_it_forbids_markdown_explicitly(self):
        # Stripping is the safety net; the prompt is the fix. If this rule is
        # ever dropped, the stripper starts carrying weight it was not meant to.
        p = qa.SYSTEM_PROMPT.lower()
        assert "markdown" in p
        assert "plain prose" in p

    def test_it_asks_for_complete_sentences(self):
        assert "finish every sentence" in qa.SYSTEM_PROMPT.lower()
