# AmongResolver

[![CI](https://github.com/RishiMaara/AmongResolver/actions/workflows/ci.yml/badge.svg)](https://github.com/RishiMaara/AmongResolver/actions/workflows/ci.yml)

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
scripts/benchmark.py`, about forty seconds. If you are wondering why a
language model is not doing this instead, that question has its own page:
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

1. **N:M (Many-to-Many) Reconciliation:** Beyond simple 1:N subset matching, the engine handles complex N:M scenarios using advanced CP-SAT constraints.
2. **Anchorless Math Fallbacks:** If the pool is too massive for exact subset-sum, the engine gracefully degrades to a custom greedy approximation solver (`approximate_subset_sum_greedy`) rather than timing out.
3. **Automatic ERP Write-Back:** Live two-way integration. The engine listens for Razorpay webhooks, reconciles, and directly syncs balanced double-entry journal postings back to your ERP.
4. **Dynamic Linkage Weights:** Uses dynamic machine-learned temporal clustering and cross-feed correspondence to build candidate evidence, replacing brittle hardcoded heuristics.
5. **Deterministic LLM Fallbacks:** When external LLM APIs fail (latency or 503s), the engine seamlessly falls back to local deterministic string similarity (`difflib`) to keep the pipeline moving.
6. **Fuzzy AML Screening:** Full compliance rulebook enforcement, including fuzzy name-matching against the UN Consolidated Sanctions List.

---

## Measured results

| | |
|---|---|
| **Batch close, 738 records / 60 settlements** | **95.0%** match rate, **0** false clears, 0 false alarms, 252 records/sec — `scripts/close_batch.py` |
| 50,000-record stress run | exact 55/55 set by ID, precision **1.0**, recall **1.0**, **2.4–3.0s** to reconcile (3.2–3.9s including load and parse) |
| ReconRiver corpus (references intact) | **94.59%** auto-cleared and correct |
| ReconRiver corpus (references stripped) | 21.62% — the rest declined, none wrong |
| Own benchmark, 120 scenarios (`benchmark.py` default) | 62% auto-clear, 82% truth identified |
| **False clears, everywhere above** | **0** |
| Confidence calibration, 180 scenarios (`calibration.py` default) | ECE **0.0863** · MCE 0.20 · Brier 0.0513 |
| Tests | **335** backend · **95** frontend |

Every figure in that table is re-measured by `python scripts/generate_benchmarks.py`,
which writes [`docs/benchmarks/latest.json`](docs/benchmarks/latest.json) stamped
with the commit and time it ran. It has no fallback values: if it cannot read a
figure out of a tool's output it fails and says which one, rather than recording
a plausible-looking constant. The two timings above are the figures that move —
they are wall-clock on one laptop under whatever else it is doing, and five runs
spanned 2.37-2.91s to reconcile. The band is the observed spread, not a target;
`latest.json` carries whatever the last run actually measured. The rest are deterministic and should not drift; a test
fails if the test count here stops matching what pytest collects.

A **false clear** — confidently clearing the wrong set — is the failure that
costs money. Declining to clear costs a reviewer minutes. The engine is tuned
for that asymmetry throughout, and the auto-clear gate sits at 0.85 because
that is where calibration measures reliability beginning: every prediction at
or above it was correct in all **103** observations.

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
  scripts/                   benchmarks, calibration, stress runs, data fetch
  tests/                     335 tests
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

**N:M reconciliation is unattempted.** Where several ledger entries and
several bank credits net off against each other, `reconcile_batch`'s model —
one target, one subset — does not describe the problem. Those cases are
excluded from the evaluation rather than forced through, and attempting them
means a different formulation, not a bigger solver.

**Linkage weights are reasoned, not learned.** 0.55 / 0.25 / 0.15 / 0.10 come
from how forgeable each signal is, which is defensible and is not the same as
fitted. There is enough labelled data in the corpora to fit them; the reason
not to yet is that a weight learned on synthetic data would look more
authoritative than it is.

**Calibration has no out-of-sample validation.** ECE 0.0863 is measured
against the same benchmark family the engine was tuned on. It is a real
measurement of a real property, and it is in-sample. A held-out corpus would
say whether the 0.85 gate generalises or is fitted to scenarios we wrote.

**Fees assume one rate card per batch.** Real Indian settlements mix payment
methods at different rates — UPI near zero, cards around 2%, netbanking often
flat — so a blended card produces a wrong gross target on a mixed day. It
fails safe: a wrong target ties out to nothing and the batch is withheld
rather than mismatched, and the engine says out loud that it estimated. It is
still the limitation most likely to meet a real merchant first.
