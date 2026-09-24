# FAILURE_LOG

Known weaknesses, measured edge cases, and failure modes.
No marketing language. Only engineering truth.

Every entry states what fails, how severely, whether the engine fails safe
or fails dangerous, and what mitigates it. An entry that reads like a sales
pitch has been written wrong.

---

## 1. Subset-sum alone cannot identify a settlement

Severity: Fatal (by design)
Fails safe: Yes — the engine declines, it does not guess

Measured: 0.0% auto-clear accuracy across 120 scenarios when linkage
is disabled (AMONGRESOLVER_NO_LINKAGE=1). Not occasionally wrong — unable
to identify the correct set at all.

For a pool of 60 transactions with 5 true members, there are 5.5 million
candidate subsets. Millions sum to the same rupee value as the true set.
The solver returns one; it is almost never the right one.

Mitigation: Linkage (Agent 2b) narrows the pool before the solver runs.
This is the architecture's core design decision, not an optimisation.

Reproduce:
    AMONGRESOLVER_NO_LINKAGE=1 python scripts/benchmark.py

---

## 2. Linkage weights are hand-set, not learned

Severity: Moderate
Fails safe: Yes — wrong weights degrade accuracy, they do not produce false clears

The four linkage weights (0.55, 0.25, 0.15, 0.10) are reasoned from signal
forgeability, not fitted to data. There is enough labelled data in the corpora
to learn them. The reason not to is that weights learned on synthetic data
would look more authoritative than they are.

Measured: sweeping all four weights across their ranges shows the current
values are within 3pp of the best achievable on the benchmark. That is a
measurement on the training set, not a generalisation claim.

Reproduce:
    python scripts/sweep_linkage_weights.py

Update (2026-09-21): the gap is now narrower where it mattered. The hand
weights still rank candidates, but a Fellegi-Sunter model (`linkage_em.py`)
learns m- and u-probabilities per pool — from anchored records by EM, or from
the settlement cycle of earlier verified clears — and its best-supported
cohort is solved as its own tier. On ReconRiver with references stripped,
exact sets identified went from 21.6% to 56.8% with zero false clears
(`scripts/learned_linkage_benchmark.py`). The hand weights themselves remain
unfitted, for the reason above.

---

## 3. Accuracy depends on reference quality, and the dependence is large

Severity: High
Fails safe: Yes — the engine declines, never guesses

Measured on the ReconRiver third-party corpus:
- References intact: 94.59% auto-clear accuracy, 0 false clears
- References stripped: 21.62% auto-clear accuracy, 0 false clears

That 73pp gap is the honest answer to how much of the accuracy is the
engine and how much is clean data. When references are missing or
truncated, the engine mostly abstains.

Reproduce:
    python scripts/run_reconriver.py

---

## 4. Calibration is in-sample

Severity: Moderate
Fails safe: Partially — the gate is conservative, but the claim is stronger than the evidence

ECE 0.0863 is measured on 180 scenarios we wrote, using the same scenario
families the 0.85 gate was picked from. It is a real measurement of a real
property, and it is in-sample.

Out-of-sample validation on ReconRiver: ECE 0.0906, 27/27 correct above
the 0.85 gate. That is encouraging and insufficient: two corpora is
validation, not proof.

Reproduce:
    python scripts/calibration.py
    python scripts/calibration_out_of_sample.py

---

## 5. Fees assume one rate card per batch

Severity: High (for mixed-method days)
Fails safe: Yes — a wrong target ties out to nothing and the batch is withheld

Real Indian settlements mix payment methods at different rates: UPI near
zero, cards around 2%, netbanking often flat. A blended rate card produces
a wrong gross target on a mixed-method day.

Mitigation: The fee audit module (fee_audit.py) now audits per-transaction
fees against a method-aware rate card post-reconciliation, catching
overcharges and GST miscalculations. The blended-rate limitation still
affects the matching target itself.

This is the limitation most likely to meet a real merchant first.

---

## 6. Sanctions screening is exact-match only

Severity: Moderate
Fails safe: Yes — a missed screen is reported as unscreened, not as clean

The compliance agent screens against the UN Consolidated Sanctions List
using exact match after normalisation. It does not handle:
- Transliteration variants (Arabic/Cyrillic names)
- Fuzzy name matching (misspellings)
- Date-of-birth disambiguation (two people with the same name)

These gaps are stated to any reviewer in the rulebook's
threshold_applied field.

---

## 7. Q&A grounding check traces by value, not meaning

Severity: Low
Fails safe: Yes — ungrounded answers are withheld, not shown

The grounding check (grounding_check.py) catches invented numbers by
tracing every figure in the model's answer back to the recorded data.
Two limitations:

- A figure is traced by VALUE, not by meaning. "3 exceptions" passes
  if a 3 appears anywhere in the results, even as a match count.
- Numbers planted in uploaded files reach the grounding and count as
  traceable. The Q&A prompt fences that text as untrusted; the check
  does not second-guess it.

It is a strong filter, not a proof.

---

## 8. Fuzzy recovery bundles have low precision

Severity: Low (by design — they are proposals, not answers)
Fails safe: Yes — fuzzy bundles cannot auto-clear (asserted)

Measured on 1,050 scored batches (edge_case_suite_1000.py):
- 167 fuzzy_semantic results
- Exact set == truth: 0.0%
- Mean precision: 0.041 (4% of the bundle is the answer)
- Mean recall: 0.692
- Truth fully inside set: 39.5%
- Median bundle size: 63 transactions

The confidence is capped at 0.05, which is below the 0.85 auto-clear
gate by assertion. A fuzzy bundle is a place to start looking, not a
claim.

---

## 9. N:M reconciliation is structurally different

Severity: Moderate
Fails safe: Yes — unsupported cases are declined

Where several ledger entries and several bank credits net off against
each other, the 1:N model (one target, one subset) does not describe the
problem. The N:M solver (subset_sum_nm.py) exists but has not been
validated on real-world N:M settlements.

---

## 10. The 0.85 auto-clear gate generalises on two corpora only

Severity: Moderate
Fails safe: Conservative direction — the gate is more likely to be too strict than too lenient

Measured at-or-above 0.85:
- Own benchmark: 103/103 correct
- ReconRiver: 27/27 correct

Both corpora carry usable references. A corpus with systematically
degraded references has not been tested against the gate, and that is
where it would be most likely to fail.

---

## 11. External LLM APIs are unreliable

Severity: Low
Fails safe: Yes — all LLM paths degrade to deterministic fallbacks

Observed repeatedly against live keys:
- 503 Service Unavailable from Gemini
- 429 with limit: 0 (zero-quota)
- Timeouts exceeding 30s

The engine retries transient failures and falls back to rule-based header
mapping (Agent 0) and deterministic summary (Agent 9). The reconciliation
result is identical with or without the LLM.

---

## 12. An enterprise layer that nothing called

Severity: High (to credibility), none (to results)
Fails safe: Yes — no request reached it, which was the problem

An "enterprise" pass added RBAC with maker/checker roles, a central
database module, Kafka ingestion and reconciliation workers, a revenue
leakage engine and Prometheus telemetry. Checked by import: no endpoint or
pipeline reached any of it. `kafka` and `prometheus_client` were not in
`requirements.txt`, so the workers could not even import on CI or Vercel.
The RBAC module held the current user in a module-level global — in a web
server, a race that can attribute one person's approval to another.

Mitigation: removed. The two ideas worth having were rebuilt on what the
engine already runs — separation of duties on posting approvals, read from
the audit trail (`four_eyes.py`), and chargebacks as new reversal rows taken
in by a later settlement (`routes_chargebacks.py`) — each with tests on the
endpoint a reviewer actually calls. The streaming design is written up in
ARCHITECTURE.md as design, not left in `src/` as a claim.

---

## 13. A benchmark labelled AI that measured none

Severity: High (to credibility)
Fails safe: Yes — the numbers were right; the label was not

`baseline_comparison.py` reported "AI-augmented vs rules-only" and asked
"what does the AI actually buy you?". Its two arms were linkage on and
linkage off. Linkage is deterministic, and the one model on the matching
side (the LLM header mapper) never runs in that benchmark, because the
scenarios are built as objects rather than parsed from files.

Mitigation: relabelled to what it measures — linkage — with the correction
recorded in the script's docstring. The result stands and is strong: 0% to
65% truth identified, zero wrong approvals in both arms.

---

## 14. A tax rate two years out of date

Severity: Medium
Fails safe: No — it would raise false findings against correct merchants

The fee audit's Section 194-O TDS default was 1%. The Finance (No. 2) Act
2024 cut it to 0.1% from 1 October 2024, so the check expected ten times the
correct withholding and would flag merchants who had been charged correctly.

Mitigation: default is 10 bps, with a test that fails if it drifts back.
Rate, threshold and the check are configurable, and the docstring says this
is arithmetic against configured numbers, not tax advice.

---

## 15. A chargeback marked taken that never took part

Severity: High (money owed back could be lost)
Fails safe: No — found before release, by its own test

The first chargeback wiring added every pending reversal to a settlement's
pool and then marked them all taken. The settlement-window filter dropped
any reversal outside the window a moment later, so one filed after the
settlement closed was silently removed from the queue — lost from that batch
and every later one. Reversals were also stamped with the time the API was
called rather than when the dispute was filed.

Mitigation: reversals carry their filing time, and only those inside the
settlement's window join the pool or leave the queue. The test that caught
it (`test_a_reversal_outside_the_window_is_not_taken_and_not_lost`) stays.

---

## 16. Razorpay's tax counted twice

Severity: Medium (only instant settlements carry a settlement fee)
Fails safe: Yes — a wrong target withholds; it cannot mismatch

Razorpay's `fees` on a settlement already includes the GST on it; `tax`
states that GST again. Its reference for an instant settlement: 200000
requested, fees 590 of which tax 90, 199410 settled. `settlement_to_batch`
added `fees + tax`, so every such target sat 90 paise from anything that
sums. The recon fixtures had the same misreading — they built `credit` as
amount − fee − tax, where Razorpay's own example is amount − fee.

Mitigation: the deduction is `fees`; the fee audit reads the fee before tax
as `fee − tax`; a test pins the 200000/590/90 reference. The recorded-shape
fixture `pull_razorpay.py --fixture` reads had the misreading too, on 17
lines, and is corrected; its tie-out never noticed, because `credit` was
right and `credit` is all the arithmetic uses.

---

## 17. A fee sum over the wrong set

Severity: High (would silently move a settlement's target)
Fails safe: No

`fee_decomposition` had a third path, since the first commit and never
tested: if candidates carried `fee_amount_cents`, sum it over the candidates
and call the target declared. The candidates are the whole windowed pool —
other settlements' payments and decoys included — and which of them are
members is exactly what has not been solved yet. No source filled the key,
so it never ran; the day the Razorpay feed filled it, a clean settlement
stopped tying out, and the recon test caught it.

Mitigation: removed. Per-row fees are audited after the match, on the
members, which is the only set they can honestly be summed over.

---

## 18. A fee audit nobody could see

Severity: Medium (to credibility)
Fails safe: Yes — nothing acted on it, which was the problem

The fee and tax audit ran on every cleared match and its findings were
stored on the report object, and no response returned them. The README
described per-method fee, GST and TDS checks; an API user or a reviewer in
the UI could see none of it.

Mitigation: every reconciliation now returns `fee_audit` — summary and
findings, each statutory one with its citation — with a test on the upload
endpoint that fails if it disappears again.

---

## 19. EM that learned nothing, measured before it shipped

Severity: Low (caught in development)
Fails safe: Yes — it declined rather than guessed

The first learned-linkage model ran Fellegi-Sunter EM unsupervised on each
pool. On ReconRiver with references stripped it collapsed to "no members" on
every settlement: with only one comparison left that differs between records
(capture lag), a two-component mixture is not identifiable, and smoothing on
the comparisons every record shared pushed lambda to its floor.

Mitigation: the model fits only when something identifies members — anchors
in the pool, or the settlement cycle learned from verified clears — and
otherwise declines, which a test pins. Constant comparisons are dropped, and
without anchors lambda stays at the arithmetic's estimate.

---

## 20. A tiebreak that overruled the evidence

Severity: Medium (a right proposal replaced by a wrong one)
Fails safe: Yes — the batch was withheld either way; the proposal was wrong

A settlement found exactly by the learned cohort — 15 payments, to the paisa
— was withheld at the confidence gate, which is correct. The ambiguity
tiebreak then ran, scored reference and memo similarity across the whole
window, and swapped in a 20-payment set. In a pool whose references are gone
that similarity is noise, and it replaced evidence with a preference.

Mitigation: the tiebreak leaves a proposal alone when every member is in the
learned cohort. Exact sets identified on the stripped benchmark rose from 15
to 21 of 37 with the fix.

---

## 21. "Machine-learned weights" that counted words

Severity: Medium (to credibility)
Fails safe: Yes — off by default

LINKAGE.md said the linkage engine "now uses dynamic, machine-learned weights
that adapt to the density of the candidate pool". The code behind it, off
unless ENABLE_DYNAMIC_WEIGHTS=1, counted how often two method names appeared
in the audit log and nudged two weights by a fixed step. Nothing measured it
and it was not learning.

Mitigation: removed; `dynamic_weights.py` now holds only the environment
overrides the weight sweep uses. The learned model that exists is
`linkage_em.py`, with its benchmark and its limits written next to it.

---

## 22. Refunds audited as sales

Severity: Low
Fails safe: Yes — a spurious finding, never a wrong clear

The fee audit ran every row with a fee field through the rate card, so a
refund — fee 0, because nothing is charged on money going back — came out as
a card "fee undercharge" of 2% of the refund. Found on the first run of the
Razorpay sample, where every payout carries a refund.

Mitigation: rows that are refunds or negative are skipped; a test on the
sample fails if a refund appears among the findings again.

---

## 23. A trail-clearing helper that cleared the wrong tier

Severity: Low (tests only)
Fails safe: Yes

`audit.clear_trail` removed a batch's entries from Redis, files and memory,
and not from SQLite — the tier that actually serves. Tests that cleared a
trail and counted entries were counting old ones, which is why so many tests
took to minting a fresh batch id each. Found while adding the hash chain,
whose tamper tests need a trail that really is empty.

Mitigation: it deletes SQLite rows too, and the chain head with them.

---

## 24. The narration reader in the wrong order

Severity: Low (caught by its own evaluation)
Fails safe: Yes — both orders keep only grounded values

The narration reader was designed regex-first, the model filling only the
fields the regex left empty. Measured on bank formats the regex was not
written for, that order scored 94.0% and model-first 97.5%: a regex that
returns the wrong payer leaves no gap for the model to fill, so its mistakes
survive.

Mitigation: model first, regex where the model found nothing, and the table
that decided it is in the module's docstring.

---

## 25. A verifier stricter than the engine it checks

Severity: Medium (a right answer would have been rejected)
Fails safe: Yes — it rejected, it did not approve

The investigator's verifier demanded a match sum to the target to the paisa.
The engine itself accepts within a tolerance, because a target rebuilt from
an estimated fee can land a paisa off the members' true sum — and the
verifier rejected the TRUE set in 4 of 38 solvable benchmark cases. It also
rejected 18 sound proposals whose reasons quoted a set's total, because the
case never stated set totals.

Mitigation: the verifier holds a proposal to the engine's own tolerance, and
the case states each set's total. A test asserts the true set passes.

---

## 26. The model read the benchmark's labels

Severity: High (to credibility) — a published figure would have been false
Fails safe: Yes — found before anything was published

The first investigator evaluation gave the model the benchmark's cases as
they are. The benchmark names its true members `S19_TRUE_0`, names each
settlement after the failure it simulates (`..._out_of_window_...`), and
writes memos like "decoy" and "unrelated payment". The model's reasons cited
"the true legs (S19_TRUE_0 to S19_TRUE_3)". Its 46.6% was a measurement of
reading labels.

Mitigation: the case the model sees in evaluation replaces every id and the
settlement's name with opaque aliases and removes reference and memo text,
keeping a per-record flag for whether the reference names the settlement.
Re-measured: 34.5% right. AI_EVALUATION.md states what the redacted run can
and cannot show.

---

## 27. An arithmetic tie passed as a finding

Severity: High (wrong sets in front of reviewers as verified proposals)
Fails safe: No — a rubber stamp would have approved them

With labels gone, the model often chose between sets that reach the same
target on arithmetic alone. Each such choice passed every check the verifier
had — in the pool, once each, right sum — and 11 wrong sets would have
reached reviewers as verified proposals from 58 cases.

Mitigation: a match that ties another listed set must carry more evidence
than every rival (records whose reference names the settlement), or it is
escalated with the sets listed. Wrong verified matches fell from 11 to 4; 3
right ones were demoted with them, correctly, because nothing distinguished
them from their rivals.

---

## 28. A calibration map that returned coin flips

Severity: Medium (a reviewer-facing figure would have been arbitrary)
Fails safe: Yes — the gate never reads it

The first isotonic fit kept tied confidences as separate points. The engine
emits a handful of discrete values, so ties are the rule, and a lookup at
0.05 returned whichever single outcome sorted first. The corrected fit then
mapped seven right out of seven to a calibrated 1.0 — "certain", beside a
proposal the engine had withheld.

Mitigation: ties are pooled before the fit, blocks are Laplace-smoothed
((right + 1) / (n + 2)) with monotonicity restored, and values between
blocks are interpolated. Tests pin all three.

---

## 29. Overconfidence called underconfidence

Severity: Low (to credibility)
Fails safe: Yes — the band never clears

Three documents described the lowest confidence band out-of-sample as
"underconfident — the safe direction". Its own numbers say the opposite: it
states 0.14 and is right 3.1% of the time. It is harmless only because no
proposal in that band is ever cleared. The README headline table also still
showed a calibration row from before the fix it described (ECE 0.0766, 4 of
46 wrong above the gate) beside prose describing the fix.

Mitigation: the wording says overconfident, the headline rows are the
current measurement (in-sample ECE 0.0544 and 103 of 103 above the gate;
out-of-sample 0.1037 raw, 0.040 calibrated, 42 of 42), and each report now
carries a calibrated figure beside the raw one.

---

## 30. The investigator saw part of the engine's proposal

Severity: High (to credibility) — a published figure was measured on it
Fails safe: Partly — truncated sets were rejected, but so were sound ones,
and a set that counted payments twice passed

When no member feed is declared, the engine searches every feed, but the
investigator's case kept only the gateway feed's records. The engine's
proposal reached the case with its ledger-side records silently dropped: on
the demo's withheld preset, 4 of 13. The verifier then rejected that set as
₹47,620 off the target, a shortfall the dropped records explained. Shown in
full, the same set exposed a second fault. It counts four orders twice, once
as the gateway payment and once as its ledger booking, and still reaches the
target; the verifier had no check for that and would have passed it.

Found by running the demo's withheld preset with an investigation, not by a
test: every test built its case from a declared feed.

Mitigation: the case shows every record the engine proposed, with its feed,
and states the member feed, marked as assumed when none was declared. The
verifier rejects records from outside the member feed and any payment
counted in two feeds, naming the pairs. The investigator evaluation was
re-run on the corrected cases, with the model's answers keyed by the content
of the case so an answer to the old case could not be scored against the new
one: 14 right, verified proposals of 58 (was 16), 4 wrong ones passing the
verifier (unchanged), fixed rules 2 of 58. The model answered 54 of 58; the
other 4 are scored as the rules scored them, which is what production does.

CI then failed the new test that the true set still passes. Its parallel
solver picked a different engine set than a laptop did, and that set's
ledger copies name the settlement as well, so on the verifier's evidence
rule the engine's set tied the true one and got it rejected. Dropping sets
that reach outside the member feed from the comparison fixed CI and let 4
more wrong proposals through in the evaluation, because their evidence is
real: it belongs to the gateway payments they copy. So a rival is now read
as the payments it stands for, with each ledger copy translated to its
gateway twin by reference and amount. With that change the true set passes
on any machine, and the figures above are unchanged.

---

## 31. The tiebreak ignored the declared member feed

Severity: Medium (impossible sets in front of reviewers)
Fails safe: Yes — the tiebreak only runs on results that are withheld

Linkage confines the solve to the declared member feed. The tiebreak that
chooses between equally valid sets searched every feed. On the demo sample,
under a batch id its references do not name, it chose 18 records: 11 were
ledger copies, and 7 of those doubled a gateway payment already in the set.
Nothing cleared, but a reviewer was shown a set that could never be right.

Mitigation: the tiebreak searches the declared member feed only. Every
figure it could move was re-measured. ReconRiver with references stripped:
exact sets 8 → 9 of 37 (21.6% → 24.3%), with learned linkage unchanged at
56.8%. Calibration: in-sample ECE 0.0544 → 0.0538; out-of-sample 0.1037 →
0.0901 raw and 0.040 → 0.026 calibrated. The lowest band is now right 2 of
32 times instead of 1, which also raises out-of-sample Brier, 0.0206 →
0.029. Auto-clears, false clears and every prediction above the gate are
unchanged.

---

## 32. A payment and its own ledger entry filed as a duplicate

Severity: Low (wrong advice on a withheld run)
Fails safe: Yes — advice only; nothing acts on it

The matcher calls any two records with the same reference and amount a
duplicate. A gateway payment and its ledger booking are exactly that, and
the exception taxonomy filed them under "Duplicate record — remove the
duplicate at the source". On the demo's withheld preset, that was 77
exceptions telling a reviewer to delete correct ledger entries.

Mitigation: one record in each of two feeds is filed as "Booked, not in this
payout" and owned by nobody yet. Two in the same feed is still a duplicate.
A test pins both.

---

## 33. The test suite could spend a real model key

Severity: Medium (a user's quota spent by tests; nondeterministic results)
Fails safe: Yes — the calls only read, but they were real and paid for

main.py loads engine/.env, so on a machine with a Gemini key every test that
reached a model path made a real call. Found when a scanned-statement test
failed because Gemini answered "no rows" instead of the test's expected
refusal. How many earlier runs spent quota this way is not known.

Mitigation: an autouse fixture in tests/conftest.py blanks the key for every
test. Tests that need a model fake one explicitly.

---

## 34. The bank upload refused bank statements

Severity: Medium (a shipped feature unreachable from the main screen)
Fails safe: Yes — nothing was misread; files could not be chosen

MT940, CAMT.053, OFX and PDF statements were supported by the engine and
listed in the README, but the upload box's file picker offered only .csv and
.json, so they could be dragged in but not chosen.

Mitigation: the bank box accepts statement formats and scans (PNG, JPEG,
image-only PDF), and says so.

---

## 35. A Dr/Cr column broke the text-statement parser

Severity: Low (a common layout refused, not misread)
Fails safe: Yes — the rows were not recognised, so nothing was used

Statements with one amount column and a Dr/Cr marker column print the
marker between the amount and the balance ("2,80,368.48 Cr 18,51,778.47").
The row pattern allowed a marker only at the end of the line, so every row of
that layout was unrecognised — found when OCR of such scans returned "no
rows" on all eight in the evaluation, with the text itself read correctly.

Mitigation: a marker may follow the amount. The sign still comes from the
running balance, as it did; the marker is only skipped. Browser OCR then read
3 of the 8 right, and the rest were real misreads, refused.

---

## 36. A payee code glued to an invoice number lost its anchor

Severity: Medium (real payments withheld that the evidence identified)
Fails safe: Yes — every one was withheld; none was cleared wrong

Found by the first benchmark on real data (two public government
checkbooks). Ingestion stores a reference with its separators stripped, so
"VNDE960709B38-4277809164" became one run in which a payee code ending in a
digit runs straight into a numeric invoice number. The anchor rule rightly
refuses an id followed by more digits — that is what keeps SETTLE-1 from
anchoring SETTLE-10 — so the payee never anchored. With the payee readable
the engine found every single-payment vendor's invoices exactly (39 of 39);
glued to a numeric invoice number, 15 of 51.

Mitigation: ingestion keeps the reference as written (extra["ref_raw"]), and
an id also anchors where it equals whole separator-delimited pieces of it;
SETTLE-1 still does not anchor SETTLE-10-ORD. Re-measured: 100 of 100 exact
with the payee known, 0 wrong clears.

The first run of the fix leaked. Benchmarks that strip the settlement id
rewrote the canonical reference and left the raw copy, which still named the
settlement: ReconRiver's "references stripped" rose from 24% to 95%. A raw
reference is now used only while it still canonicalises to the stored one,
and the stripping drops it; every ReconRiver figure then matched its
committed value exactly.

## 37. The investigator's rules proposed sets bound to lose

Severity: Medium (withheld settlements left with no usable proposal)
Fails safe: Yes — the verifier stopped them; none reached a reviewer

The rules behind the investigator proposed the engine's own set, the one the
engine had just withheld, and the verifier stopped nearly all of them: fixed
rules put 2 right answers of 58 in front of a reviewer. The cases showed why
the model did little better: the true set was among the listed alternatives
in only 5 of the 38 solvable ones, so neither could choose it.

Mitigation: a case now lists up to two more alternatives built around the
records the evidence ties to the settlement. The rules match only a set the
evidence singles out, wait on a timing gap, ask for the source where nothing
reaches the target, and otherwise escalate. The model gets one retry, told
which checks failed in the same aliases it sees; then the rules' answer
stands if it verifies, or the case is escalated. A rejected proposal never
reaches a reviewer.

Re-measured: fixed rules 16 right and verified of 58 (was 2), 0 wrong through
(was 3); the investigator as deployed 22 (was 14), 3 wrong through (was 4).
Of the 22, 20 are cases missing a member; on the 38 solvable cases it finds
the exact set twice. The gain is safe escalation, not better matching.

## 38. A payment id shared by two feeds joined the matched set

Severity: Medium (a correct clear reported as not tying; fees audited on the wrong record)
Fails safe: Partly — the set cleared was right; what was reported about it was not

Found by reading the code for a review, then reproduced. The solver
identifies records by feed and id (linkage.txn_key), but a result
carried only the bare ids, and everything after the solve — the tie-out, the
fee audit, the cash position, the rows a reviewer sees, the paid-out ledger —
found its records again by bare id. A gateway payment "1001" and an unrelated
ERP line "1001" were then both "matched": a settlement that cleared exactly
reported that it did not tie, by the ERP line's Rs 77.77.

Mitigation: a result carries `matched_keys` ("gateway:1001") beside its ids,
every reader resolves through `linkage.members_of`, and the paid-out ledger
and the cross-batch check key on them. Test: test_review_fixes.py.

## 39. A uniqueness check that ran out of time was reported as proof

Severity: Low (confidence overstated; the evidence gates still applied)
Fails safe: Mostly — linkage confidence still gated the clear

CP-SAT answers INFEASIBLE when it proves no second subset exists and UNKNOWN
when its time runs out first. The probe treated both as "no alternate", so a
check that never finished reported the arithmetic at 1.0 — a uniqueness no
one had established. It now records which it was; a timed-out probe reports
the arithmetic at 0.90 and says so, in the 1:N and the joint solve alike.

## 40. A batch cleared while a third of its payments were already paid out

Severity: Medium (a payment could be counted into two settlements)
Fails safe: No — the clear stood, with a warning under it

The paid-out check withheld a batch only when at least half its payments had
been cleared into an earlier settlement. Below that it cleared and printed a
warning — the arrangement its own comment warned against. A payment cannot
fund two settlements, which is the rule the cross-batch check already applied
inside one run. Now any such payment withholds the clear, on the single
upload and in the queue.

## 41. What the review found around the edges

Severity: Low
Fails safe: Yes

- The paid-out ledger had no shared store: on serverless it was per-instance
  memory even with Redis configured. It now uses Redis when one is set.
- The audit trail wrote to Redis first but read SQLite first, so a batch with
  older local rows came back stale. Reads now follow the write order.
- `/reconcile/upload` and `/reconcile/queue` ran the CPU-bound solve on the
  event loop, stalling every other request on the worker. They run it in the
  thread pool now; a parse failure in the queue is a 400, as on the upload.
- `/demo` ran a 10,000-record solve for anyone. It is off on Vercel unless
  ENABLE_DEMO_ENDPOINT=1.
- A reviewer's name could carry the separation-of-duties marker's own
  brackets and bars. They are stripped from names now.

## 42. A test kept passing after the code it guarded had moved

Severity: Low (a guard that could no longer fail)
Fails safe: Yes, until the next endpoint

test_every_upload_path_goes_through_the_cap reads main.py's source for bare
upload reads. Splitting main.py (1,160 lines to 160: routes to api/, what a
run does to settlement_run.py) moved every upload handler out of the one file
it read, and it went on passing because there was nothing left to find. It
now reads main.py and every module under api/, and asserts it found an
upload handler at all. The split also merged the upload and queue ingest
paths, which had drifted (the queue gave a 500 on an unreadable file and
dropped the timezone and currency notes), and gave each of 29 silent
`except Exception` handlers a log line, or, for parse probes, a narrower
exception.

## 43. The paid-out ledger stopped answering the callers that asked by bare id

Severity: Medium (a payment already paid out could look unpaid)
Fails safe: Partly — the single upload and the queue asked by key and still withheld

The fix for FAILURE_LOG 38 records a clear under the collision-safe key
("gateway:1001"). Three callers still asked by bare id: open items (so a
payment an earlier settlement took could be listed as unsettled), the
investigator's already-paid-out check, and the FIFO acceptance. Found while
fixing what a first full type-check (mypy, 107 errors) flagged, by reading
every caller of the ledger. The ledger now answers for a payment in either
form — key or bare id — so no caller has to know how it was recorded; a bare
id matching another feed's key reads as a conflict, which withholds.
test_settled_ledger_aliases.py.

The same type-check found an annotation naming a class that was never
imported (left by the file_agent split), a date that could be None reaching
arithmetic, and a bank statement with no balance on its first line able to crash
the opening-balance derivation. All fixed; mypy is clean and now runs in CI.

## 44. The verifier compared a match only with the sets it was shown

Severity: Medium (a wrong match could reach a reviewer marked as verified)
Fails safe: No — three passed in the evaluation

The investigator's verifier refused a match that did not carry more
evidence than every LISTED rival set. The listed sets are a sample. In the
ref_collision cases twenty unrelated records carry the settlement's own
reference; the model picked a set of them that out-evidenced everything
listed while another set, just as well evidenced, sat unlisted in the pool.
Three such wrong matches reached a reviewer marked as verified.

Mitigation: one bounded solve asks whether ANY other member-feed set reaches
the target with at least as much evidence; finding one, or running out of
time, refuses the match. Re-measured by replaying the recorded model answers:
0 wrong matches through (was 3), 20 of 58 right (was 22). The two lost were
picks between equally evidenced sets that happened to be right, which a
reviewer should not be shown as verified. The demo's agent match still
passes: its 14 payments are the only set with that evidence.

## 45. One SQLite connection shared across the thread pool

Severity: High on a self-hosted deployment (a crash under concurrent requests)
Fails safe: No — the request failed with a 500

Found by the first load test. FastAPI runs sync work on a thread pool, and the
engine opened one sqlite3 connection with check_same_thread=False for every
thread to share. The audit trail serialised its own writes, but the paid-out
ledger, open items and the settlement cycle used the same connection with no
common lock; at four concurrent uploads it failed with SystemError inside
sqlite3. The deployed site was not exposed, because on Vercel those stores
are in Redis; any single-host deployment was.

Mitigation: one connection per thread (audit._get_db, reached through
stores.durable_db); SQLite in WAL mode serialises writers between
connections. The load test then ran every level with zero errors, and
test_stores.py hammers the ledger from eight threads at once.

The same work gave the Razorpay route the shared ingest path: its own copy
turned a ledger with no recognisable columns into a 500 rather than the 422
every other upload gives.

## 46. Two wrong clears in a blind test written after the engine

Severity: Critical (a wrong set cleared automatically, at confidence 0.91)
Fails safe: No

A blind adversarial test was written for the final review from scratch, with
its own generator, ground truth and a count of every subset that ties, none
of it seen by the engine before. 237 settlements in 23 families. Two cleared
the wrong set, both in one family: every member but one names the settlement,
the member that belongs is missing from the feed, and an unrelated payment of
exactly its amount carries no reference at all. The learned-cohort tier (by
capture lag) and the unconstrained fallback (all anchors used) each admitted
that payment, and nothing after them asked whether it had any reference.

This is the gap a comment in recon_gates already described: blocking every
partly anchored match "cost ten right answers to stop two wrong ones", so only
matches that left anchors unused were blocked. A match that uses every anchor
and completes the total with a record carrying no reference was still let
through, and it is evidence-identical to a stranger standing in for a missing
member.

Mitigation: when a matched set names the settlement, every other member must
share an informative reference token with the members that do (one not
carried by most unrelated records, so "ORDER" does not count). Otherwise the
set is proposed, not cleared: withheld_reason unreferenced_member, the record
named in the summary, and the investigator's question asks about that one
payment. Re-measured: the blind test's 2 wrong clears become 0; the
engine's own benchmarks do not move (120 scenarios 65% identified, 62%
auto-cleared; 1,000 edge cases 73.24% auto-cleared, 0 wrong; every
should-clear still clears), because a member that lost its settlement id
there still shares its batch reference. test_blind_findings.py pins both
directions.

The test is engine/scripts/blind_test.py; its final results, on the
original seed and a fresh one, are docs/benchmarks/blind_test_*.json, and
scripts/enterprise_baseline.py runs two enterprise-style matchers on the
same cases (enterprise_comparison.json).

## 47. A reconcile run tried to post the journal before anyone approved it

Severity: High on a deployment with ERP_JOURNAL_URL set; Medium on the demo
Fails safe: No where a URL was set; the demo only mislabelled

Every cleared run called erp_sync.push_to_erp while building the report,
before the journal approval that the page, the docs and separation of duties
all promise comes first. With no URL set it posted nothing, but it rewrote the
journal's status to no_target_configured, and the page shows any status other
than "proposed" as Rejected and hides Approve and the Tally export. So on the
live site every cleared settlement showed its posting proposal as Rejected.
Found by clicking through the deployed site, not by a test: the component
tests fed the panel a journal already marked "proposed".

Mitigation: a reconcile run proposes the journal and sends it nowhere;
erp_sync is the adapter an approval workflow calls. The page labels only a
status of "rejected" as Rejected. test_blind_findings pins that a clear
returns a journal awaiting approval.

## 48. Four smaller faults the same test and click-through found

Severity: Medium to Low
Fails safe: Yes (refusals and mislabels, no wrong clear)

- An export that repeats a row exactly (same id, amount, time, reference,
  status), as overlapping date-range exports do, made every such settlement
  ambiguous between the two copies: 12 of 12 refused. Exact repeats are now
  read once, with a note; a repeated id with any field different is still
  kept twice, and still refused.
- A blank amount cell reads as 0, and a zero-amount record naming the
  settlement ties with or without it: three one-answer settlements were
  called ambiguous. Zero amounts are now excluded from the pool with the
  failed and uncaptured ones.
- The duplicate-transaction control compared records across feeds, so on
  the sample every gateway payment and its ERP ledger line, 90 records, were
  flagged as duplicates. It now compares within one feed.
- The cash position listed the settlement's own bank credit as "In bank,
  unexplained" beside the clear that explained it. When cleared, the credit
  named by the batch id, or the only one of exactly the net amount, is
  counted as explained and a note says which.

## 49. Common customer names blocked whole payouts, and the page said there was no fuzzy matching

Severity: High (a legitimate payout held on a name alone)
Fails safe: Yes (nothing cleared wrongly; payouts were withheld)

Checked while weighing a proposal to let a model downgrade sanctions hits
(rejected: a model must not clear a sanctions finding). Screening forty
common names against the real UN list blocked nine, among them ABDUL RAHMAN,
ABDUL KARIM and MUHAMMAD YUNUS on exact aliases and MOHAMMAD ALI through an
85%-similarity match to MOHAMMAD WALI. The rulebook, the page and /health all
said matching was exact with no fuzzy matching; the code blocked on fuzzy
hits. A blocked payment leaves the pool, so one customer's name withheld the
whole payout.

Mitigation: an exact hit on a primary name or an alias the UN does not rate
"Low" still blocks (SANCTIONS_HIT). A near spelling, or a name the list
carries only as a low-quality alias (427 of them, now written beside the list
by fetch_sanctions_list.py), is SANCTIONS_POTENTIAL_MATCH: flagged, HIGH,
statutory, never closed by the engine, and the payout still reconciles. The
rulebook, scope note and /health now describe what the code does. What stays
blocked is still a name match: ABDUL RAHMAN is a good-quality alias. There is
no date-of-birth or transliteration disambiguation.

## 50. The payout cycle was learned in calendar days

Severity: Medium (weaker timing evidence exactly around weekends and holidays)
Fails safe: Yes

Razorpay pays T+2 working days, so a Friday capture before a second Saturday,
a Sunday and a holiday is paid five calendar days later, and the learned
histogram smeared one cycle over lags 2 to 5. Switching to working days
outright took the third-party ReconRiver benchmark from 56.76% to 43.24%
exact, because its generator pays on calendar days. Both units are now
learned per merchant and the sharper (lower entropy) is used; ReconRiver
stays at 56.76% with 0 wrong clears, and a T+2-working-day merchant is
learned in working days (test_settlement_cycle_units).

## 51. What the engine noticed never reached the page

Severity: Medium
Fails safe: Yes

The website dropped ingestion_notes, so "chose column X over Y — verify",
"read as INR", "assumed UTC" never reached the person reconciling. One
unreadable amount also refused the whole file, which the blind comparison
showed cost 9 of 237 settlements that rule-based tools reconcile by skipping
the row, and the upload form had no way to enter a declared FX rate.

Mitigation: the result shows the notes under "What we noticed in your
files". Up to a tenth of rows with an unreadable amount are set aside and
named rather than refusing the file; the rest clears only if it ties
exactly, so a set-aside member withholds. The form takes FX rates, a foreign
payment left out for want of one is named with the fix, and a declared rate
more than 3% from the ECB reference for the day is named too (advisory,
offline-safe, never on the money path).

## 52. A file over the hosted limit failed with a parse error

Severity: Low (a confusing message, nothing wrong reconciled)
Fails safe: Yes

Found by the limits test (scripts/limits_test.py, docs/benchmarks/limits.json),
which pushed each dimension until the engine stopped: 200,000 payments in one
payout and 2,000,000 records in one file both reconciled exactly, with no
wrong clear and no crash anywhere. The hosted engine sits behind Vercel's
4.5 MB request limit: 60,000 rows (3.75 MB) cleared in 13 s, and 80,000 rows
(5.01 MB) were refused by Vercel with a plain-text 413 before the engine saw
them. The upload page read that reply as JSON and showed the parse error.

Mitigation: the page checks the files' size before sending and says what the
limit is and why; a non-JSON error reply is read as a sentence, and a 413 as
the size limit (lib/api.ts, tested).
