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

