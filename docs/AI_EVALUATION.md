# Where this engine uses AI, and what each use measured

Every model in this engine proposes; code decides. Each use below has a
deterministic fallback, a check that runs in code rather than in a prompt,
and a measurement with the script that reproduces it. Model runs used
`gemini-flash-latest` at temperature 0 and spent about 180 calls in total.
Two passes of the same run differed by about half a point, so small
differences below are noise.

| Use | Without the model | With it | The check in code |
|---|---|---|---|
| Reading bank narrations | regex: 88.5% / 82.7% | 97.7% / 97.5% | every value must appear in the narration |
| Investigating withheld settlements | rules: 2 of 58 right and verified | 16 of 58 | a verifier: arithmetic, ledger, calendar, evidence, grounding |
| Answering questions about a result | — | 44 of 44 | every figure and id must trace to the recorded result |
| Linkage where references are gone | 24.3% of sets found | 56.8% | the solver: exact sums only, 0 false clears |
| Confidence calibration | ECE 0.104 out-of-sample | 0.040 | isotonic, and the auto-clear gate never reads it |

The last two are statistical models, not language models, and are in the
table because they are the other place the engine learns from data.

## Reading bank narrations — `narration_reader.py`

A narration holds the UTR, the gateway's settlement reference, the payer and
the rail, in each bank's own format, cut off at 40-70 characters.
`scripts/narration_eval.py` generates 240 narrations from nine bank formats:
the four the regex was written against, and five it never saw. They are
damaged the way statements damage them. A field's truth is only what
survives the damage, so a UTR cut in half has "none" as its right answer.

| | formats the regex knows | formats it doesn't |
|---|---:|---:|
| regex only | 88.5% | 82.7% |
| regex first, model fills gaps | 96.9% | 94.0% |
| **model first, regex fills gaps** (shipped) | **97.7%** | **97.5%** |

The model saw no examples of either set. Its answers passed through the
grounding rule, which dropped 14 values that were not in the narration. The
order that ships is the one that measured best, which is not the order first
written: a regex that returns the *wrong* payer leaves no gap for the model
to fill (FAILURE_LOG 24). Ingestion stays regex-only and deterministic. The
model reads only when `POST /narrations/read` asks it to.

## Investigating withheld settlements — `investigation_agent.py`

For a settlement the engine would not clear, the investigator gathers the
case: the engine's own set, up to three other sets that reach the target,
exceptions by category, and the next working day. All of these tools are
read-only and are run for it. It then proposes one typed action: match,
wait, request a document, write off rounding, or escalate. A verifier checks
every proposal before a reviewer sees it:
- the set is in the pool, each record once, in the right currency;
- nothing in it was already paid out elsewhere;
- it sums to the target within the engine's own tolerance;
- a wait ends on a working day;
- a write-off is the actual residual and at most ₹1;
- the reason's figures trace to the case;
- a set that ties another on arithmetic carries more evidence than its rival.

`scripts/investigation_eval.py` runs on the 58 benchmark settlements the
engine withheld: 38 solvable, 20 where a member is missing.

| | rules | model |
|---|---:|---:|
| right action | 3 | 20 |
| right, verified, reaching a reviewer | 2 | **16** |
| wrong match reaching a reviewer, no verifier | 35 | 11 |
| wrong match reaching a reviewer, with verifier | 3 | **4** |

**What this measures, and what it doesn't.** The model sees ids and the
settlement's name as opaque aliases, with reference and memo text removed,
because the benchmark's labels live there. Its members are named
`S19_TRUE_0` and its memos say "decoy". The first run, before the
redaction, read the labels and scored 46.6%; that number is void
(FAILURE_LOG 26). This measures reasoning over amounts, dates and linkage
evidence. It cannot measure reading real narrations, which the product also
gives the model. The 4 wrong matches that pass are sets where noise shares
the settlement's reference, so the evidence itself points the wrong way. A
verified proposal is still only a proposal: accepting it goes through the
reviewer decision, where separation of duties applies.

## Answering questions — `settlement_qa.py`

`scripts/qa_eval.py` asks 44 questions: 4 recorded results × 11 questions.
- **Fact questions:** did it clear, how many matched, the target, the
  confidence, the exceptions, the residual, and why it cleared or was
  withheld.
- **Questions to decline:** a customer's phone number, a forecast, an order
  to approve and release funds, and an instruction planted in the question
  as if it were data.

29 of 29 facts were answered from the record, and 15 of 15 declines were
declined or refused, including the planted instruction, which the model
named as suspicious. The grounding check withheld nothing on this set
because it had nothing to catch. Its catches are pinned by adversarial
tests (`test_grounding_check.py`). This is a small set, and a correct one;
it is not proof the model is always right. That is why the check exists.

## Linkage where references are gone — `linkage_em.py`

A Fellegi-Sunter model (the method behind Splink). Its m-probabilities come
from EM over anchored records, or from the processor's settlement cycle
learned from verified clears. On ReconRiver with every settlement id
stripped, exact sets rose from 24.3% to 56.8%, with the cycle learned only
from other scenarios. False clears stayed at 0, and nothing changed on this
project's own benchmark or on the 1,050-case edge suite. These new matches
arrive as proposals, below the gate: 7 of 7 measured is too few to release
money on. See LINKAGE.md.

## Calibrated confidence — `calibration_map.py`

This is isotonic regression (pool-adjacent-violators, Laplace-smoothed),
reported beside the raw confidence and never read by the auto-clear gate.
`scripts/fit_calibration.py`:

| fitted on → judged on | ECE raw | ECE calibrated | low band said / right |
|---|---:|---:|---|
| benchmark → ReconRiver | 0.104 | **0.040** | 0.14 → 0.09 / 0.03 |
| ReconRiver → benchmark | 0.054 | 0.069 | 0.15 → 0.25 / 0.14 |

Fitted on the larger corpus, it transfers. Fitted on the 74-prediction one,
it does not. So the shipped map uses both, and reviewers keep it honest:
every confirmed or rejected batch is recorded as an outcome, and
`GET /calibration` reports any band where 20 or more decisions disagree
with the map by 15 points or more.
