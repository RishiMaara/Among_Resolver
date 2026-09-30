# AmongResolver: Test Report

This document records every test campaign run against AmongResolver, what each
one was designed to establish, the result, and where the evidence lives. Every
figure below comes from a committed script and, where one exists, a committed
result file under `engine/docs/benchmarks/`. The commands to reproduce each
result are given with it.

Last full run: 24 September 2026, on the code deployed at
https://among-resolver.vercel.app and https://among-resolver-engine.vercel.app.

Related documents:

- `docs/COMPARISONS.md` compares these results with traditional matching
  methods, an enterprise-style matcher and published vendor figures.
- `FAILURE_LOG.md` records every defect these tests found and how each was
  resolved.

---

## Summary

| Campaign | What it establishes | Result |
|---|---|---|
| Backend test suite | Behaviour of every module, including every past defect | 815 of 815 pass |
| Frontend test suite | Page behaviour, and that published figures match their files | 151 of 151 pass |
| Static analysis | Types, lint errors, security | mypy clean on 78 files; pylint and bandit report no issues |
| Blind adversarial test | Behaviour on cases written after the engine, with an independent answer key | 133 correct clears, 0 wrong, on two seeds of 237 settlements |
| 1,000-case edge suite | Behaviour across 29 hazard families and joint (N:M) runs | 1,050 batches scored, 0 wrong clears, 0 crashes |
| 50-case live edge suite | The HTTP path end to end, including rejected files | 50 of 50 behave as specified, 0 wrong clears |
| Limits test | Where the engine stops being able to prove an answer | 200,000 payments in one payout and 2,000,000 records in one file proved exactly; 0 wrong clears |
| Accuracy benchmarks | Identification rate with and without each component | Linkage 0% to 84%; references stripped 24.32% to 56.76%; 0 wrong clears |
| Engine tuning | Identification with damaged references, checked on seeds not used for tuning | 63-65% to 82-85% on two held-out seeds; 0 wrong clears |
| Real public data | Two government checkbooks with a recorded answer | 50 of 50 and 50 of 50 exact |
| Live model evaluation | The investigator agent against a live model | 0 wrong proposals passed the verifier |
| Live website verification | Every page and control on the deployed site | All controls work; six defects found and fixed |

No campaign produced a wrong clear on the current code. The blind test's first
run, on the code as it stood before the final review, produced two; both are
fixed and recorded in `FAILURE_LOG.md`, entry 46.

---

## 1. Automated suites

### 1.1 Backend

The backend suite has 815 tests. It covers file parsing and header mapping,
normalisation to integer paise and UTC, linkage, the subset-sum solver, the
refusal gates, fee and tax audit, compliance screening, the investigator and
its verifier, the paid-out ledger, the audit chain, every API route, and a
regression test for every defect in the failure log.

```
cd engine
python -m pytest -q
```

Result: 806 passed, 0 failed. The suite runs offline: a fixture in
`tests/conftest.py` blanks the model keys and the ECB reference-rate lookup, so
no test depends on a network or spends a model quota.

The UN sanctions list is tracked in the repository
(`engine/data/sanctions/un_consolidated.txt`, 3,422 identifiers, generated
23 September 2026), so a fresh clone runs the same tests as the deployed
engine. `python engine/scripts/fetch_sanctions_list.py` refreshes it.

### 1.2 Frontend

```
npx vitest run --config vitest.config.ts
```

Result: 151 passed, 0 failed. Two of these suites protect the documentation
itself: `src/lib/measured.test.ts` fails if any figure on the judges' brief
differs from the benchmark file it cites, and
`src/lib/documented-figures.test.ts` fails if the README's test counts differ
from what the suites actually collect. The backend has the same guard in
`engine/tests/test_documented_figures.py`.

### 1.3 Static analysis and continuous integration

| Check | Command | Result |
|---|---|---|
| Types | `python -m mypy` (config `engine/mypy.ini`) | No issues in 78 source files |
| Lint errors | `python -m pylint src --errors-only --disable=import-error` | No errors |
| Security | `python -m bandit -r src -ll` | No issues identified |
| Frontend types | `npx tsc --noEmit` | Clean |
| Frontend lint | `npx eslint .` | Clean |
| Production build | `npx vite build` | Succeeds |

Every check above except the production build runs on every push in
`.github/workflows/ci.yml`, after fetching the current sanctions list.

`scripts/prove_claims.py` re-derives six documented claims from scratch (test
count, lint, security scan, the 50,000-record stress result and the benchmark's
zero false clears) and reports each as verified or not. Result: 6 of 6
verified.

---

## 2. Blind adversarial test

Script: `engine/scripts/blind_test.py`.
Results: `engine/docs/benchmarks/blind_test_20260923.json` and
`blind_test_777001.json`.

### 2.1 Design

The test was written during the final review, after the engine, with its own
generator and its own answer key; nothing was taken from the engine's own
benchmarks. It contains 237 settlements in 23 families, each built with its
ground truth. Where arithmetic alone decides the answer, every subset of the
pool that ties to the target is counted independently (meet in the middle), so
a case described as having one answer provably has one.

Each settlement is uploaded through `/reconcile/upload`, the endpoint the
website uses, with the investigator enabled and the model disabled so that
results are repeatable. Every outcome is classified against the truth:

- **Correct clear**: cleared with exactly the true set.
- **Wrong clear**: cleared with any other set, or cleared when no set ties.
- **Refused**: withheld for a person, with the reason stated.
- **Rejected**: the file refused with a named reason.

```
cd engine
python scripts/blind_test.py --seed 20260923
python scripts/blind_test.py --seed 777001
```

### 2.2 Results over the review

| Run | Correct clears | Wrong clears | Refused | Files rejected |
|---|---|---|---|---|
| First run, original seed, before fixes | 109 | 2 | 117 | 9 |
| After the first fixes, original seed | 124 | 0 | 104 | 9 |
| Final code, original seed | 133 | 0 | 104 | 0 |
| Final code, fresh seed 777001 | 133 | 0 | 104 | 0 |
| After tuning (section 9), seeds 777001, 20260923 and 20260930 | 133 each | 0 | 104 each | 0 |

The first run found two wrong clears (a payment with no reference completing a
set whose other members named the settlement) and two unnecessary refusals
(exact duplicate export rows, and blank amounts read as zero). The later rise
from 124 to 133 comes from setting unreadable amount rows aside instead of
rejecting the whole file.

### 2.3 Final results by family (fresh seed)

| Family | Cases | Expected | Correct clears | Wrong clears |
|---|---|---|---|---|
| References name the settlement | 12 | clear | 12 | 0 |
| Named, among 20 to 150 other settlements' payments | 12 | clear | 12 | 0 |
| Settlement id written with other separators or case | 12 | clear | 12 | 0 |
| No references, only one set ties | 12 | clear if unique | 0 (refused) | 0 |
| No references, fixed-price catalogue | 12 | refuse | 0 | 0 |
| Two identical unreferenced payments, either fills | 12 | refuse | 0 | 0 |
| Bank credit short | 12 | refuse | 0 | 0 |
| Bank credit over | 12 | refuse | 0 | 0 |
| A member missing from the feed | 12 | refuse | 0 | 0 |
| A member missing, a stranger of the same amount present | 12 | refuse | 0 | 0 |
| Refunds netted in the payout | 12 | clear | 12 | 0 |
| Failed, pending and authorised rows naming the payout | 12 | clear | 12 | 0 |
| A row exported twice | 12 | clear or refuse | 12 | 0 |
| First of two settlements sharing a payment | 12 | clear | 12 | 0 |
| Second settlement re-using a paid-out payment | 12 | refuse | 0 | 0 |
| Foreign currency, rate declared | 4 | clear | 4 | 0 |
| Foreign currency, no rate | 4 | refuse | 0 | 0 |
| Foreign currency, rate 1% wrong | 4 | refuse | 0 | 0 |
| IST timestamps near midnight | 12 | clear | 12 | 0 |
| A named member with a same-amount stranger | 12 | clear | 12 | 0 |
| An unreadable amount in one row | 12 | clear or reject | 12 | 0 |
| Deductions estimated from a rate card | 6 | clear or refuse | 6 | 0 |
| Scale, 150 to 800 members in up to 6,800 rows | 3 | clear | 3 | 0 |

Of the 104 refusals, 92 are cases that must not clear. The other 12 are the
"no references, only one set ties" family: the engine declines to clear a
payout on arithmetic alone even when the file admits one answer, because a
unique total within one file does not prove the file is complete.

The same run scores two traditional baselines, reported in
`docs/COMPARISONS.md`.

---

## 3. Edge-case suites

### 3.1 The 1,000-case suite

Script: `engine/scripts/edge_case_suite_1000.py`. Summary figures in
`engine/benchmarks/edge_1000.json`.

It generates 1,000 cases (962 single-batch cases in 29 families and 38 joint
N:M cases) and scores 1,050 batches in-process.

| Measure | Result |
|---|---|
| Batches scored | 1,050 |
| Wrong clears | 0 |
| Crashes or exceptions | 0 |
| Payments cleared into two settlements | 0 |
| Foreign-currency rows inside an INR match | 0 |
| Auto-cleared | 769 (73.24%), all correct |
| Must-not-clear cases held | 100% (278) |
| Should-clear cases cleared | 100% (234) |
| Latency p50 / p95 / max | 0.008 s / 0.085 s / 3.005 s |

### 3.2 The 50-case live suite

Script: `engine/scripts/edge_case_suite.py`, run against a live engine.

Fifty deliberately awkward files: headers under a title banner, Indian lakh
grouping, amounts in brackets, four date formats in one column, epoch
timestamps, composite keys, ragged rows, empty files, JSON instead of CSV,
refunds, rolling reserves, chargebacks, failed and uncaptured payments, cash
over the reporting threshold and a structuring pattern.

Result on the final code: 50 of 50 behave as specified (32 cleared, 11
withheld, 7 rejected with a named reason), 0 wrong clears, and 17 of 20 agents
exercised. During the review this suite also caught a regression introduced by
a fix of our own (duplicate rows merged on mapped fields, which collapsed ten
genuine payments into one); the fix was redone before release.

---

## 4. Limits test

Script: `engine/scripts/limits_test.py`. Results: `engine/docs/benchmarks/limits.json`.

This test pushes one dimension at a time until the engine can no longer prove
an answer, and records the correct answer beside what the engine returned.
Past its edge the engine is expected to refuse; a wrong clear anywhere is a
failure of the engine.

### 4.1 Payments in one payout

Every payment names the settlement; 5,000 other settlements' payments share
the pool.

| Payments in the payout | Result | Time |
|---|---|---|
| 1,000 | exact | 0.22 s |
| 10,000 | exact | 0.91 s |
| 25,000 | exact | 2.10 s |
| 50,000 | exact | 5.44 s |
| 100,000 | exact | 8.29 s |
| 200,000 | exact | 16.75 s |

Linkage keeps every payment that names the settlement and caps only the
unnamed candidates, at 400, which is why the payout size is not bounded by
that cap.

### 4.2 Records in one file

`engine/scripts/run_scale_proof.py --records N`: one 55-payment payout inside
a three-source pool, parsed from files and reconciled.

| Records | Total time | Solve time | Result |
|---|---|---|---|
| 200,000 | 10.66 s | 7.42 s | exact, precision and recall 1.0 |
| 500,000 | 33.06 s | 23.70 s | exact |
| 1,000,000 | 85.48 s | 58.86 s | exact |
| 2,000,000 | 165.54 s | 129.09 s | exact |

Measured on one development machine (12 logical CPUs).

### 4.3 The hosted engine

The deployed engine runs on Vercel, which limits a request to about 4.5 MB and
60 seconds.

| Upload | Result |
|---|---|
| 60,000 rows, 3.75 MB, one 2,000-payment payout | Cleared exactly, all 2,000 matched and tied out, in 13.1 s |
| 80,000 rows, 5.01 MB | Refused by Vercel with 413 before reaching the engine |

The upload page now checks the size before sending and explains the limit
(`FAILURE_LOG.md`, entry 52).

### 4.4 Tolerance, estimated fees and missing references

| Condition | Result |
|---|---|
| Credit short by 0, 3 or 5 paise | Cleared, with the shortfall shown as the residual |
| Credit short by 6, 10 or 100 paise | Refused |
| Deductions estimated from a rate card, estimate within 5 paise (10, 25, 50, 400, 800 payments) | Cleared |
| Estimate off by 6, 10 or 29 paise (100, 200, 1,600 payments) | Refused |
| No payment carries a reference, only one set ties (pools of 4 to 24) | Refused, 7 of 7 |

The tolerance is 5 paise and it is not exceeded. Where deductions are not
declared, per-payment fee rounding moves the estimated target, and the engine
refuses once the error passes the tolerance; declaring the deductions from the
settlement advice removes the problem.

Across the limits test: 0 wrong clears and 0 crashes.

---

## 5. Accuracy benchmarks

Each figure below is shown on the judges' brief and checked against its file
by `src/lib/measured.test.ts`.

| Benchmark | Script | File | Result |
|---|---|---|---|
| Linkage against arithmetic alone, 120 scenarios | `baseline_comparison.py` | `baseline_comparison.json` | True sets identified 0% to 84%; auto-cleared 0% to 62%; 0 wrong clears in both arms |
| Third-party data with references stripped (ReconRiver, 37 settlements) | `learned_linkage_benchmark.py` | `learned_linkage.json` | Exact sets 24.32% to 56.76% with a learned payout cycle; 0 wrong clears |
| Bank narrations in formats the rules never saw | `narration_eval.py` | `narration_eval.json` | Accuracy 82.71% (rules) to 97.50% (model first, grounded) |
| Investigator on withheld settlements (58, of which 38 have a right answer) | `investigation_eval.py` | `investigation_eval.json` | Right, verified proposals 16 (rules) to 20 (as deployed); 0 wrong proposals past the verifier |
| Confidence calibration, out of sample | `fit_calibration.py` | `calibration_fit.json` | Expected calibration error 0.0901 to 0.0262 |
| Baton Rouge checkbook, 50 real payments | `public_ledger_benchmark.py` | `public_ledgers.json` | Exact invoice sets 2 (amounts only) to 50 (with payee); 0 wrong clears |
| Fulton County checkbook, 50 real payments | same | same | 0 to 50; 0 wrong clears |
| Noisy scanned statements, 24 | `ocr_eval.py` | `ocr_eval.json` | Read exactly 11 (browser OCR) to 23 (with the model); 0 wrong readings accepted |
| Settlement Q&A, 29 fact questions | `qa_eval.py` | `qa_eval.json` | 29 answered from the record; 15 of 15 declined where the record cannot answer |
| One settlement inside 200,000 records | `run_scale_proof.py` | `scale_proof.json` | 55 members found exactly in 10.66 s (0.053 s per 1,000 records) |

The ReconRiver and linkage benchmarks were re-run after the final changes on
24 September 2026 and did not move. After the tuning of 30 September
(section 9), linkage rose from 65% to 84% and ReconRiver did not move. With references intact, the ReconRiver
corpus clears 94.59% with 0 wrong clears (`FAILURE_LOG.md`, entry 3).

### 5.1 The 120-scenario accuracy benchmark by family

`engine/scripts/benchmark.py` runs 12 hazard families at three pool densities.

| Family | Truth found | Wrong clears | Abstentions |
|---|---|---|---|
| clean, near_collision, exact_collision, duplicate_amounts, wide_spread, large_subset | 100% each | 0 | 0 |
| missing_leg, out_of_window (unsolvable by design) | not applicable | 0 | 10 of 10 each |
| ref_missing | 100% (0% before the tuning in section 9) | 0 | 0 |
| ref_truncated | 90% (0% before) | 0 | 0 |
| ref_collision | 50% | 0 | 0 |
| ref_partial | 0%; 100% with the member feed declared | 0 | 0 |

Where a clean settlement reference exists the engine identifies the true set
every time. Where the reference column is missing or cut short but the
description still carries the settlement's number, it now finds the set and
proposes it; it clears only with a record that names the settlement. Where
unrelated payments carry the settlement's number too, half the sets are still
missed, and the engine declines rather than guesses.

### 5.2 The 50,000-record stress test

`engine/scripts/run_50k_stress_test.py`: 50,000 records across bank (15,000),
gateway (25,000) and ERP (10,000) feeds, with 55 true members.

| Measure | Result |
|---|---|
| Exact set by transaction id | Yes, 55 of 55 |
| Precision and recall | 1.0 and 1.0 |
| Parse and normalise | 0.97 s |
| Reconciliation | 2.24 s |
| Total | 3.22 s |

Timings are one run on one machine and vary with load.

### 5.3 Load

`engine/scripts/load_test.py`, results in `engine/docs/benchmarks/load_test.json`:
one engine process, in-process ASGI, no model, at concurrency 1, 4 and 8. About
ten uploads per second per process, with 0 errors at every level.

---

## 6. Live model evaluation

During the final review the investigator was run with a live Gemini model on
the 20 hardest withheld cases from the blind test (twin payments, fixed-price
catalogues, short credits and missing members).

| Measure | Result |
|---|---|
| Cases | 20 |
| Final action | Escalated to a person in 20 of 20 |
| Decided by the model | 11 |
| Decided by the rules fallback after the model failed or timed out | 9 |
| Wrong proposals that passed the verifier | 0 |
| Settlements cleared by the model | 0 (the model cannot clear) |
| Latency | median 10.9 s, maximum 42.5 s |

The latency figure reflects model overload at the time of the run: the
provider fallback chain and the per-request budget absorbed it. This run's
harness was not committed; the committed, repeatable measurement of the
investigator is `investigation_eval.json` (section 5).

---

## 7. Live website verification

On 24 September 2026 every page and control of the deployed site was exercised
in a browser, signed in with the demo account.

| Page | Controls exercised | Result |
|---|---|---|
| Home (reconcile) | Both samples, Run Engine, agent nodes, replay speeds, theme, hide panel, reset, fee terms, confirm decision, settlement Q&A | All work |
| Judges' brief | The three demonstrations together and singly, the four further demonstrations, upload links | All work |
| History | List and filter | Work |
| Escalations | Queue display | Works |
| Rulebook | Basis filter | Works |
| Payouts | Both samples, check payouts, due-date explanations, closed items | All work |

Defects found by this walk-through, all fixed and recorded in `FAILURE_LOG.md`:

1. Every cleared settlement showed its posting proposal as "Rejected" and hid
   the approval and Tally export, because a reconcile run pushed the journal
   to an ERP before any approval (entry 47).
2. The settlement's own bank credit appeared as "In bank, unexplained" (48).
3. Every gateway payment and its ERP ledger line, 90 records, were flagged as
   duplicates (48).
4. Notes the engine writes about the files never reached the page (51).
5. A file over the hosting limit produced a parse error instead of a message (52).
6. The payouts page read "Read as a API response" (grammar).

After the fixes, the full approval chain was exercised against the engine: a
confirmation by one reviewer, a refused approval by the same reviewer
(separation of duties), an approval by a second reviewer, and a Tally XML
export.

---

## 8. Feature checks

These checks exercise specific behaviours with fixed inputs.

**Joint N:M reconciliation.** Two settlements, A (Rs 65.00) and B (Rs 60.00),
share a pool in which one payment (GW3, Rs 25) carries a reference naming
both. Only A can reach its target with GW3. Solved jointly through
`/reconcile/joint`, A clears with GW1 and GW3 and B with GW4 and GW5, each at
confidence 0.97. The contested payment is assigned by what both targets need,
not by processing order.

**No exact subset.** Where the target is Rs 300.00 and the closest subset sums
to Rs 299.99, the engine does not clear; it reports the closest set as a
proposal for review, with the reason.

**ERP journal adapter.** For an approved journal (Rs 97 net, Rs 2 fees, Rs 1
tax), `erp_sync.push_to_erp` produces a balanced double-entry payload with
exact integer paise beside each decimal amount. A reconcile run never calls
it; no live ERP has received a journal.

**Model outage.** When the model returns 503 or 429 during header mapping, the
engine falls back to deterministic mapping and the reconciliation result is
unchanged.

**Recovery claims.** On the sample Razorpay payouts, the payout check drafts two
claims: Rs 43.09 of fees above the rate card, to Razorpay Support, marked as
resting on the rate card with a note to check negotiated pricing first; and a
Rs 10.00 short bank credit, to the bank, marked as resting on the data alone,
citing the UTR and the credit. A fee below the rate card is not a claim.
Nothing is sent; each claim is a message and a CSV of evidence to download
(`engine/tests/test_recovery_claims.py`, `src/routes/payouts.test.tsx`).

---

## 9. Engine tuning, 30 September 2026

The engine was tuned where the tests above showed it losing answers, and the
tuning was checked on data it had not seen.

**Where it lost answers.** The blind test is at its ceiling in every family but
one: small pools with no reference, where the engine finds the right set every
time and proposes it rather than clears it. That rule is what stopped the two
wrong clears of entry 46 and was left alone. On ReconRiver with references
stripped, the remaining misses are ties: up to 34 payouts a day with no
reference, where more than one set of payments sums to the credit. The
generator interleaves payouts in a fixed pattern that would identify them, and
it was deliberately not used, since it describes the generator rather than any
processor. Every miss in the 120-scenario benchmark was in the damaged-reference
families.

**What changed.**

1. Linkage reads identifier numbers from a record's description: runs of four
   or more digits, never words, years, whole dates or the record's own amount.
   Words were excluded because the benchmark's descriptions carry label words
   ("Settlement", "Unrelated"), and clustering on them would read the answer key
   (entry 26 records that mistake once already). A description containing the
   settlement's full id anchors a record, except on the feed the settlement
   itself arrived on.
2. With no anchor, the member feed is inferred from the records that carry the
   settlement's own number, as it already was from anchors.
3. The solver tries those records first, after the anchors and before the
   learned payout cycle.

**How it was checked.** Seeds 7 and 101 were looked at while fixing and count as
tuning data. Seeds 202 and 303 were never looked at. The "before" figures come
from an untouched copy of the previous commit. A control run erases every
description.

| 120 scenarios, true sets identified | Before | After | Wrong clears |
|---|---|---|---|
| Seed 7 (tuning) | 65% | 84% | 0 |
| Seed 101 (tuning) | 66% | 85% | 0 |
| Seed 202 (held out) | 63% | 82% | 0 |
| Seed 303 (held out) | 65% | 85% | 0 |
| Seed 7, member feed declared | 80% | 95% | 0 |
| Seed 7, every description erased (control) | 65% | 65% | 0 |

The held-out seeds gain as much as the tuning seeds, and with the descriptions
erased the result returns exactly to 65%, so the gain comes from identifiers
carried in descriptions and from nothing else. Missing and truncated references
went from 0% to 90-100%; colliding references are unchanged (30-50%); partial
references reach 100% only when the member feed is declared.

The auto-clear rate did not move (62-64%). A set identified only by description
numbers is proposed for one-click approval, not cleared: clearing still needs
a record that names the settlement, or a pool of 20 or fewer candidates.

**Nothing else moved.** Blind test, three seeds including a new one (20260930):
133 correct, 0 wrong, 104 refused on each. ReconRiver: 94.59% with references,
56.76% without, 0 wrong clears. The 1,000-case edge suite: 0 wrong clears.
Backend 815 and frontend 151 tests pass.

**Speed.** Every reconciliation looked up earlier runs of its own batch by
opening and decoding every run record ever written: 3,305 files and 3.4
seconds a run, 41 of 44 seconds of a profiled test, and slower with every run.
It now filters on the file name first and takes 0.025 seconds. A blind-test
seed fell from about eleven minutes to about one, and the backend suite runs in
99 seconds. The history page, which sorted by file name, now lists runs newest
first (`FAILURE_LOG.md`, entry 53).

**The page during a run.** Measured in a browser with the CPU slowed four
times, as on a laptop on battery, from pressing Run on the sample to the
report: the main thread was blocked for 17.8 seconds, in 88 long tasks, with
86 frames slower than 50 ms. The history fix above took it to 4.3 seconds. The
rest was the page itself: the run clock was page state set ten times a second,
and the progress poll replaced the audit trail every 350 ms whether or not it
had grown, so the whole page and the agent canvas re-rendered continuously.
The clock now ticks in its own component and an unchanged trail is kept:
1.2 seconds blocked, 6 long tasks, 6 slow frames. The largest remaining pause
is the report being drawn once, at the end (`FAILURE_LOG.md`, entry 55;
`src/components/elapsed-clock.test.tsx`).

---

## 10. What these tests do not establish

- No live merchant's settlements have been reconciled. Razorpay test mode makes
  no settlements, so the Razorpay API path is built to the published contract
  and tested on recorded responses and on the dashboard's recon report format.
- Most corpora are generated. The two government checkbooks are real, but
  neither is a payment gateway's data; no processor publishes settlement data.
- The limits and scale figures come from one development machine; production
  capacity on other hardware has not been measured.
- The investigator was measured with reference and memo text removed, because
  the benchmark's labels live there; reading real narrations is measured
  separately (section 5) but not inside the investigator.
- Sanctions screening has no transliteration or date-of-birth disambiguation.
