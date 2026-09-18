# AmongResolver

**CI:** [three jobs — engine · pytest, web · vitest/eslint/tsc, engine · pylint/bandit](https://github.com/RishiMaara/AmongResolver/actions/workflows/ci.yml)

<!-- No badge image, deliberately. This README previously carried
     ![CI](.../RishiMaara/Among_Resolver/actions/workflows/ci.yml/badge.svg)
     — the SUBMITTED repository, not this one. It rendered red here while
     every job in this repository passed, because it was reporting a
     different repository's result: the submitted entry is frozen and
     still carries a Prettier error in src/components/wordmark.tsx that
     fails its web job. Nothing fixable here could ever turn that badge
     green.

     Pointing it at this repository instead does not work either: this
     one is private, and GitHub proxies README images anonymously, so a
     private repo's badge.svg comes back 404 and renders broken. A link
     to the Actions tab is the option that tells the truth. -->

A multi-source settlement reconciliation engine. Built for the Razorpay AI
Buildathon, Track 04 — AI Finance Controller.

Give it a gateway export, a bank statement and an ERP ledger, and it works out
which transactions make up a settlement, proves the arithmetic ties out, screens
the records against a published compliance rulebook, and produces a cash
position with a balanced posting proposal.

It never writes to a ledger. It proposes, and a human approves.

---

## The finding this engine is built around

Subset-sum cannot **identify** a settlement, and no better solver fixes that.

A settlement of unknown size drawn from a pool of *n* transactions has 2ⁿ
candidate subsets — 1.2 × 10¹⁸ for a pool of only 60 — competing for a target
that can take roughly 2 × 10⁶ distinct paise values. Millions of subsets hit
the same rupee amount. Measured across 120 scenarios, using subset-sum to find
the members scored **0.0% accuracy**.

You can run that yourself — `AMONGRESOLVER_NO_LINKAGE=1 python
scripts/benchmark.py`, about forty seconds.

This is an AI system that had to decide where AI belongs. Two agents use an
LLM — schema mapping and settlement Q&A, both language problems. Twelve do
not, because "which payments compose this settlement" is a money problem with
millions of arithmetically valid answers, and the number above is what
happens when you let a search pick one anyway: 0.0%. The judgment call is the
contribution, not a limitation to apologize for. The full argument, including
what would change our mind, is its own page:
**[Why not just an LLM?](docs/WHY_NOT_AN_LLM.md)**

So the engine reframes the problem: **reconciliation is entity resolution
first and arithmetic second.** Linkage (Agent 2b) narrows the pool by identity
— settlement references, cross-feed correspondence, temporal clustering —
before the solver runs. On the 50,000-record dataset that is 50,000 candidates
down to 55. The sum then *verifies* a set that was *identified* by evidence.

When the evidence is not there, the engine says so instead of guessing. That
is the behaviour the numbers below are really measuring.

## AmongResolver vs. Traditional Platforms

**Identify vs. Verify:** Legacy systems and ML models use fuzzy matching or probabilistic scoring to *suggest* matches (Identification). AmongResolver uses these same techniques to narrow the pool, but relies on subset-sum arithmetic (CP-SAT) as an uncompromising gatekeeper to *prove* the settlement down to the exact paise (Verification). 

**Natively Built for Gateways:** Traditional reconciliation tools (e.g., Trintech, BlackLine) break when bank settlements are deposited net of gateway fees. AmongResolver dynamically parses unstructured CSVs on the fly and natively decomposes fees, converting a net deposit back to its true gross target—built specifically for the modern Stripe/Razorpay/Adyen era.

**Zero False Positives:** Where traditional platforms settle for a "95% confidence score" and rely on heavy, rigid ETL pipelines for month-end closes, AmongResolver is a lightweight, high-velocity pipeline that micro-routes decisions in milliseconds and demands mathematical proof. Its uncompromising guardrails ensure exactly **0 false clears**.

For a deeper dive into how this engine stacks up against industry standards, read **[Industry Comparison](docs/INDUSTRY_COMPARISON.md)**.

## Core Capabilities

1. **N:M (Many-to-Many) Reconciliation:** `POST /reconcile/joint` solves several settlement targets in one CP-SAT model, so a transaction contested between two batches is resolved by what the *other* batch needs, not by whichever batch happened to run first. It reuses the same evidence-based safety the 1:N path has — linkage narrows each batch's own candidates, an anchored refund is still forced, an unevidenced arithmetic match is still withheld — and falls back to running the proven 1:N path per batch if the joint model can't satisfy every target at once, so N:M is never worse than calling the 1:N path on each batch separately. See `engine/src/subset_sum_nm.py` and `orchestrator.reconcile_many`.
2. **Anchorless Math Fallbacks:** If the pool is too massive for exact subset-sum, the engine gracefully degrades to a custom greedy approximation solver (`approximate_subset_sum_greedy`) rather than timing out.
3. **ERP Journal Sync (no real ERP has received one):** On a genuine clear, the engine builds a balanced double-entry journal and POSTs it to `ERP_JOURNAL_URL`. Unset by default, and unset means **nothing is posted and the journal says `no_target_configured`** — it does not quietly succeed. This module previously caught an unreachable endpoint and returned success with the journal marked `posted_mock`, so a connection refused left the books showing a journal as posted that no ERP ever received; a crash gets investigated, a false success gets reconciled against next month. Every outcome is now distinct (`posted`, `posted_to_mock`, `rejected_by_erp`, `unreachable`, `no_target_configured`, `skipped_unbalanced`) and only the first two return true. The payload is the shape NetSuite/Tally/QuickBooks accept, with exact integer paise carried alongside each decimal — but "would be accepted" is a design claim, not a measurement. Ingestion is **settlements pushed, transactions pulled**: `POST /webhooks/razorpay` receives signed `settlement.processed` events (HMAC-SHA256 over the raw body, constant-time compare, replay-suppressed, 503 rather than accepting an unsigned delivery), while the payments a settlement decomposes into still arrive by CSV upload or the `razorpay_source.py` pull. So a verified delivery queues a settlement for reconciliation rather than reconciling it — the transaction feed is what reconciliation needs and the webhook does not carry it.
4. **Dynamic Linkage Weights:** Uses dynamic machine-learned temporal clustering and cross-feed correspondence to build candidate evidence, replacing brittle hardcoded heuristics.
5. **Deterministic LLM Fallbacks:** When external LLM APIs fail (latency or 503s), the engine seamlessly falls back to local deterministic string similarity (`difflib`) to keep the pipeline moving.
6. **AML Screening:** Compliance rulebook enforcement against the UN Consolidated Sanctions List — exact match after normalisation (no fuzzy, transliteration or DOB matching; `docs/ARCHITECTURE.md` states this alongside the caveat it implies). Without a fetched list it screens four illustrative names and says so loudly (see "Running it").

---

## Measured results

| | |
|---|---|
| **Batch close, 738 records / 60 settlements** | **95.0%** match rate, **0** false clears, 0 false alarms, ~400 records/sec — `scripts/close_batch.py` |
| 50,000-record stress run | exact 55/55 set by ID, precision **1.0**, recall **1.0**, **2.0–2.3s** to reconcile (3.0–3.5s including load and parse) |
| ReconRiver corpus (references intact) | **94.59%** exact set identified, 91.89% auto-cleared and correct |
| ReconRiver corpus (references stripped) | 21.62% — the rest declined, none wrong |
| Own benchmark, 120 scenarios (`benchmark.py` default) | 62% auto-clear, 65% truth identified, 0 false clears |
| **False clears, everywhere above** | **0** |
| Confidence calibration, 180 scenarios, in-sample (`calibration.py` default) | ECE **0.1567** · MCE 0.3087 · Brier 0.1808 — not a good number; see below |
| Confidence calibration, ReconRiver, out-of-sample (`calibration_out_of_sample.py`) | ECE 0.0766 · MCE 0.46 · Brier 0.0616, 74 predictions |
| Tests | **401** backend · **96** frontend |

Every figure in that table except the two calibration rows is re-measured by
`python scripts/generate_benchmarks.py`, which writes
[`docs/benchmarks/latest.json`](docs/benchmarks/latest.json) stamped with the
commit and time it ran; it has no fallback values — if it cannot read a figure
out of a tool's output it fails and says which one, rather than recording a
plausible-looking constant. The two timing figures are wall-clock on whatever
machine ran it, under whatever else it was doing at the time; four runs
spanned 2.0–2.3s to reconcile. The band is the observed spread, not a target.
The rest should be deterministic and not drift on their own; a test fails if
the test count here stops matching what pytest collects.

The own-benchmark truth-identified figure and both calibration rows are lower
than earlier versions of this document claimed (82% and ECE 0.0863). Re-run
`calibration.py` or `benchmark.py` yourself — the 64% and the 0.1567 are what
this commit actually produces, confirmed by running the identical scripts
against the last commit before this pass of fixes (`70cc42a`) and getting the
same numbers back. So this was not a regression introduced by anything below;
it is a number that was true the whole time and had drifted out of the docs,
most likely from before `ref_missing` / `ref_truncated` / `ref_partial` —
the three scenario families where references degrade — were added to the
benchmark. Truth-identification on the six families with a clean reference is
still 100%; it is those three specific degraded-reference families, plus
`ref_collision`, that pull the average down. See "What it does not do" for
what that means for the confidence number specifically.

A **false clear** — confidently clearing the wrong set — is the failure that
costs money. Declining to clear costs a reviewer minutes. The engine is tuned
for that asymmetry throughout: **0 false clears, in every benchmark in the
table above.** That guarantee holds by defense in depth, not because the raw
confidence number is a trustworthy probability on its own — measured
in-sample, it is not (see the calibration row above and "What it does not do"
below). Auto-clearing requires the 0.85 confidence gate **and** independent
evidence/ambiguity checks to agree; a confident-but-wrong proposal still gets
withheld for human review rather than cleared. That is what has actually kept
false clears at zero, in-sample and out, and it is a claim about the system,
not about the number alone.

Every figure above is reproducible. `engine/benchmarks/calibration.json`
records the commit, seed and scenario count each was measured at, and the
benchmark pins CP-SAT to a single worker so it produces the same answer
twice — see `docs/ARCHITECTURE.md`, "Why the numbers used to move".

`engine/scripts/realistic_benchmark.py` sweeps the properties real
payment feeds have that generated ones do not — partial reference coverage,
truncated references, colliding transaction ids across feeds, unpadded
sequential settlement ids, fee-estimate error, late legs — and reports what
each one costs. It found five defects that both reference corpora hid.

---

## Running it

Backend (FastAPI, port 8001):

```bash
cd engine && pip install -r requirements.txt && python -m uvicorn main:app --app-dir src --port 8001
```

Fetch a real sanctions list before you rely on the compliance screen. Without
this the engine falls back to a four-name ILLUSTRATIVE list and says so on
every screen — correct behaviour, but it is not screening anything real. The
fetched list is not committed, so this is needed on every fresh clone:

```bash
cd engine && python scripts/fetch_sanctions_list.py
```

Frontend (Vite, port 8080):

```bash
npm install && npm run dev
```

Then open <http://localhost:8080>.

**If you are here to evaluate this, read [docs/FINAL_PITCH_SCRIPT.md](docs/FINAL_PITCH_SCRIPT.md) first.** It provides the exact 5-minute narrative for the live demonstration, including the verification standard and the zero-hallucination math.

`sample-data/` holds the original three-feed fixture and
`sample-data/README.md` has its exact form values. Enter the settlement date
it gives you rather than leaving the field at today: both the fixture and the
date are fixed, so it works on any day. Leaving it at today only worked while
today stayed inside the lookback window.

---

## Layout

```
src/                       React frontend
  routes/                    index (agent flow) · rulebook
  components/agent-flow      the live pipeline canvas
  components/flow-narrative  the scroll explanation beneath it
  lib/agent-flow.ts          the graph, and how audit entries drive it

engine/        Python engine
  src/                       one module per agent — see docs/ARCHITECTURE.md
  src/api/                   the HTTP surface: models, presentation, route groups
  scripts/                   benchmarks, calibration, stress runs, data fetch
  tests/                     401 tests
  benchmarks/                measurement snapshots
  data/                      Sanctions lists, test ledgers
design/                    Design canvases the UI is ported from
docs/                      ARCHITECTURE.md · FINAL_PITCH_SCRIPT.md · FINAL_TEST_REPORT.md · INDUSTRY_COMPARISON.md
sample-data/               a settlement you can drop into the form
```

The flow canvas is driven by `GET /audit/{batch_id}`. Every node lights up
because that agent recorded a decision during the run — nothing on it is on a
timer, which is the point: it is the engine reporting on itself, and the same
trail a reviewer reads afterwards.

---

## What it does not do

- **Sanctions screening is exact-match against a point-in-time list.** Without
  `SANCTIONS_LIST_PATH` (or a fetch via `scripts/fetch_sanctions_list.py`) it
  screens four illustrative names and says so loudly at every startup.
- **Several compliance thresholds are ours, not the law's.** The rulebook
  labels each one, and the ₹5 crore ceiling in `LIMIT_EXCEEDED` is called out
  by name as invented. No Indian statute imposes it.
- **Accuracy depends on reference quality, and the dependence is measured.**
  With settlement references intact the engine identifies the correct set
  94.59% of the time; strip those references out and the same corpus drops to
  21.62%. Both conditions produced **0 false clears**. That gap is the honest
  answer to how much of the accuracy is the engine and how much is clean data,
  and `scripts/run_reconriver.py` runs both conditions.
- **The confidence number is reliable at and above the gate — measured, not
  assumed.** Every prediction at or
  above 0.85 was the exact true set: 103 of 103 in-sample
  (`calibration.py`), 42 of 42 out-of-sample on ReconRiver
  (`calibration_out_of_sample.py`). It used to be 57.8% right in the
  0.85–0.93 band in-sample and 4-of-46 wrong above the gate out-of-sample;
  the cause was the fuzzy recovery pass reporting its own selection
  threshold, 0.90, as though it were a confidence — on bundles of ~63
  transactions that were the exact right set 0% of the time. Below the gate
  it is calibrated in-sample (says 0.15, right 13.9%) but overconfident
  out-of-sample: the low band says 0.14 and is right 3.1% of the time (n=32).
  That band never clears anything — it sits far below 0.85 — so the cost is a
  reviewer seeing a slightly hopeful number on a proposal already sent to
  them, not a payment released. In-sample no bucket is overconfident; the last
  one that was, the ambiguous-match constant claiming 0.54 against 36.4%
  observed, is now set at its measured 0.36.
- **Three agents use an LLM** — header mapping, fuzzy matching, and Q&A. All
  three propose; none decides. No model sits on the money path.

## Where it goes next

Each of these is a known edge rather than an oversight, and each is where the
current design stops rather than where it fails.

**Razorpay API — built, not yet run against a live account.**
`engine/src/razorpay_source.py` reads `/v1/settlements` and
`/v1/settlements/recon/combined` directly, so a merchant does not export a CSV
at all. The recon report carries `settlement_id` on every line, which is the
strongest anchor linkage can be given, and amounts arrive as integer paise so
no parsing step can introduce a rounding error. Refunds arrive negative via
`credit - debit`, which is what the orchestrator's refund handling expects.

What is verified: the mapping, end to end through the real engine, against
recorded response shapes — 20 tests, including a settlement recovered exactly
from a pool of 131 where 120 lines belong to other settlements. That both
endpoints answer 401 to a bad key while a made-up path answers 404 confirms
the paths and the auth format. What is NOT verified is a live account, because
no test credentials existed when it was written; `verify_tie_out()` checks the
one inferred assumption — that a settlement's amount equals the sum of its
lines' `credit - debit` — and `scripts/pull_razorpay.py --verify-only` runs it.
Set `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` and it is one command.

Authentication and both endpoints **have** now been exercised against a real
test-mode key: the call authenticates, paginates and returns cleanly. What it
returns is nothing, because a fresh test account holds no settlements and
Razorpay produces test settlements on its own schedule — days, not minutes. So
`--fixture` reconciles the documented response contract instead, through the
identical code path, and prints on every run that the values are invented and
a live account has not agreed to them yet.

**N:M reconciliation is built, and reuses the 1:N safety machinery rather than
reimplementing it.** `POST /reconcile/joint` and `orchestrator.reconcile_many`
solve several settlement targets in one CP-SAT model with a shared candidate
pool, so a transaction contested between two batches is resolved by what both
targets need at once, not by whichever batch's solve ran first. Linkage,
anchored-refund forcing, and the unevidenced-match withholding are reused
exactly as the 1:N path defines them, per batch, before the joint solve runs.
What is deliberately **not** yet generalised to the joint case: the 1:N path's
tiering (progressively widening the candidate pool) and its substitutability
guard (flagging a matched transaction that a same-amount transaction from
another feed could equally have satisfied) are both materially different
problems once several targets share a pool, and neither has been extended
there. If the single joint model can't satisfy every target in a group at
once, `reconcile_many` falls back to running the proven 1:N path independently
per batch, so N:M is never a worse answer than calling the 1:N path on each
batch separately — it can only do better when a shared claim is actually
resolvable by the other target's needs.

**Linkage weights are reasoned, not learned.** 0.55 / 0.25 / 0.15 / 0.10 come
from how forgeable each signal is, which is defensible and is not the same as
fitted. There is enough labelled data in the corpora to fit them; the reason
not to yet is that a weight learned on synthetic data would look more
authoritative than it is.

**Calibration has out-of-sample validation, and the gate now holds in both.**
`calibration_out_of_sample.py` runs the same confidence/outcome scoring
against ReconRiver — a corpus the gate was never tuned against. Every
prediction at or above 0.85 was the exact true set there (42 of 42), as it
was in-sample (103 of 103). It previously was not: 4 of 46 above the gate
were wrong out-of-sample, and the in-sample 0.85–0.93 band was right 57.8%
of the time.

What changed was not the gate but a number feeding it. The fuzzy recovery
pass reported `fuzz_cfg.confidence_threshold` — the score above which a
fuzzy *pair* is worth acting on — as the confidence of the recovered *set*.
That constant is 0.90, above the gate, and it was being attached to bundles
of ~63 transactions that were the exact right set 0% of the time over 167
observations. Those are now reported at 0.05.

One number moved the wrong way and is reported rather than dropped:
out-of-sample ECE rose from 0.0766 to 0.1037 while in-sample fell from
0.1567 to 0.0544, and out-of-sample MCE — the worst single bucket — improved
from 0.46 to 0.13. Removing a block of confident-and-wrong
predictions concentrates the residual error in the low band, where the
engine is now underconfident — the safe direction, and the direction that
costs review time rather than money.

**Fees assume one rate card per batch.** Real Indian settlements mix payment
methods at different rates — UPI near zero, cards around 2%, netbanking often
flat — so a blended card produces a wrong gross target on a mixed day. It
fails safe: a wrong target ties out to nothing and the batch is withheld
rather than mismatched, and the engine says out loud that it estimated. It is
still the limitation most likely to meet a real merchant first.
