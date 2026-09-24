# AmongResolver

[![CI](https://github.com/RishiMaara/Among_Resolver/actions/workflows/ci.yml/badge.svg)](https://github.com/RishiMaara/Among_Resolver/actions/workflows/ci.yml)

**Settlement reconciliation for Indian payments.** It works out which payments
make up each payout, proves the answer to the paisa, and refuses to clear
what it cannot prove — then says what a person should do next. On every
corpus it has been measured on it clears no wrong set, including a blind test
written after it, whose two wrong clears were fixed and logged (FAILURE_LOG 46).

Built for the Razorpay AI Buildathon, Track 04 — AI Finance Controller.
**[Live demo](https://among-resolver.vercel.app/)** ·
**[Judges' brief](https://among-resolver.vercel.app/judge)** — live
demonstrations against the engine, and every figure below with the file it came from.

It never writes to a ledger. It proposes; a person approves.

| | |
|---|---|
| Wrong sets cleared, every measured corpus | **0** |
| Real government payments, two public checkbooks, answer recorded by their own systems | **100 of 100** exact with the payee known; 0 wrong clears in 200 runs |
| Third-party corpus (ReconRiver), references present | 94.6% of settlements identified exactly |
| Same corpus, every settlement reference stripped | 24.3% → **56.8%** with learned linkage |
| Bank narrations read right, bank formats the rules never saw | 82.7% rules → **97.5%** grounded model |
| One settlement found inside 200,000 records | exact 55 members, precision and recall 1.0 |

**What it does**

- **Reads Razorpay directly** — the Settlement Recon report a merchant
  downloads from the Dashboard (no API keys, nothing shared), or the API. It
  names each payout's members, so the engine checks what that list does not
  prove: the lines sum
  to the payout to the paisa; a blind re-solve without the settlement ids
  reaches the same set; the bank credit carries the UTR and exact amount;
  every order is in the books; fees, GST, TDS and TCS are right for their
  dates.
- **Reads bank statements as banks send them** — MT940, CAMT.053, OFX, PDF,
  and scans: a scan is read by OCR in your browser (Tesseract.js, free, the
  file never leaves your machine to be read) or by Gemini where a key is set.
  Every read proves itself: opening + credits − debits = closing, line by
  line, and a misread digit is refused with the line named.
- **Finds the members when nothing names them.** Linkage (anchors, and a
  Fellegi-Sunter model that learns the processor's payout cycle) narrows
  the pool; exact CP-SAT subset-sum verifies it; four refusal gates stop
  anything the evidence does not carry.
- **Knows Indian specifics.** TDS under Section 194-O until 31 March 2026 and
  Section 393(1) Sl. 8(v) after; TCS under Section 52 CGST; input tax credit
  against the gateway's invoice; T+2 in working days across second Saturdays
  and state holidays; a Tally XML export of the approved posting.
- **Keeps what is left in view.** Open items carried across runs and aged;
  exceptions filed by finance category with an owner and a first step; an
  investigator that proposes what to do with a withheld payout, verified in
  code before anyone sees it.
- **Controls.** Separation of duties, a hash-chained audit trail with
  receipts, approval-gated exports, and a failure log of everything that went
  wrong ([FAILURE_LOG.md](FAILURE_LOG.md)).

**Where AI is used, and what checks it** — every model proposes and code
decides; each use has a deterministic fallback and a measurement:
[docs/AI_EVALUATION.md](docs/AI_EVALUATION.md).

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

This is an AI system that had to decide where AI belongs. Language models
read language here — column headers, bank narrations, scanned statements,
questions about a result — and propose an action for a settlement the engine
withheld; every
one of those outputs is checked in code before it counts. None of them
decides which payments compose a settlement, because that is a money problem
with millions of arithmetically valid answers, and the number above is what
happens when you let a search pick one anyway: 0.0%. The full argument,
including what would change our mind:
**[Why not just an LLM?](docs/WHY_NOT_AN_LLM.md)**

So the engine reframes the problem: **reconciliation is entity resolution
first and arithmetic second.** Linkage (Agent 2b) narrows the pool by identity
— settlement references, cross-feed correspondence, temporal clustering —
before the solver runs. On the 50,000-record dataset that is 50,000 candidates
down to 55. The sum then *verifies* a set that was *identified* by evidence.

When the evidence is not there, the engine says so instead of guessing. That
is the behaviour the numbers below are really measuring.

## How it differs from matching by score

**Identify, then verify.** Fuzzy matching and probabilistic scoring are how
reconciliation tools usually *suggest* matches. This engine uses the same
ideas to narrow the pool, then requires an exact subset-sum (CP-SAT) to
*prove* the settlement to the paisa, and withholds whatever it cannot prove.

**Built for net settlements.** Gateway payouts arrive net of fees and tax.
The engine reconstructs the gross target from declared deductions or a rate
card, and when it has to estimate, it says so in the report.

**Refusal is a feature.** A confident wrong answer costs money; a withheld
one costs review time. The engine is built to pay the second to avoid the
first — 0 false clears everywhere it has been measured.

More on how this compares with established tools, and what it does not yet
do that they do: **[Industry Comparison](docs/INDUSTRY_COMPARISON.md)**.

Part by part — every module, its check, and the measurement behind it:
**[Capabilities](docs/CAPABILITIES.md)**.

---

## Measured results

| | |
|---|---|
| **Batch close, 738 records / 60 settlements** | **95.0%** match rate, **0** false clears, 0 false alarms, 3–4 s for all 60 (about 190–250 records/sec) — `scripts/close_batch.py` |
| 50,000-record stress run | exact 55/55 set by ID, precision **1.0**, recall **1.0**, **3.1–3.2s** to reconcile (about 4.0s including load and parse) |
| ReconRiver corpus (references intact) | **94.59%** exact set identified, 91.89% auto-cleared and correct |
| ReconRiver corpus (references stripped) | 24.32% exact, **56.76%** once the payout cycle is learned from earlier payouts; 21.62% auto-cleared, the rest withheld, none wrong |
| Own benchmark, 120 scenarios (`benchmark.py` default) | 62% auto-clear, 65% truth identified, 0 false clears |
| **False clears, everywhere above** | **0** |
| Confidence calibration, 180 scenarios, in-sample (`calibration.py` default) | ECE 0.0538 · MCE 0.20 · Brier 0.0439 — 103 of 103 correct at or above the 0.85 gate |
| Confidence calibration, ReconRiver, out-of-sample (`calibration_out_of_sample.py`) | ECE 0.0901 raw, **0.026** calibrated (`fit_calibration.py`) · 42 of 42 correct above the gate, 74 predictions |
| **Real payments** — Baton Rouge and Fulton County public checkbooks, 100 payments, each against its whole day's payment run (`public_ledger_benchmark.py`) | payee known: **100 of 100** exact, 99 auto-cleared · amounts only: 2 of 100, the rest withheld · **0** wrong clears in 200 runs |
| Scanned bank statements, 24 noisy scans (`ocr_eval.py`) | 11 read exactly right by OCR in the browser, **23** with Gemini on the rest, **0** wrong readings accepted |
| Tests | **806** backend · **147** frontend |

Every figure in that table except the two calibration rows is re-measured by
`python scripts/generate_benchmarks.py`, which writes
[`docs/benchmarks/latest.json`](docs/benchmarks/latest.json) stamped with the
commit and time it ran; it has no fallback values — if it cannot read a figure
out of a tool's output it fails and says which one, rather than recording a
plausible-looking constant. The two timing figures are wall-clock on whatever
machine ran it, under whatever else it was doing at the time; three runs on
21 September spanned 3.1–3.2s to reconcile (earlier passes measured
2.0–2.3s). The band is the observed spread, not a target.
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

Then open <http://localhost:8080>. The demo sign-in — its account is printed
on the screen — exists so decisions carry a name. `/judge` needs no sign-in;
`/payouts` checks Razorpay payouts and lists what is still waiting.

Or run both in containers: `docker compose up --build` (engine on 8001, app
on 8080).

**A model key on a public site.** Set `GEMINI_API_KEY` in the engine's
environment (on Vercel: the engine project → Settings → Environment
Variables), from a Google AI Studio project **without billing**, so it can
only ever use the free daily quota. Every model call made while serving a web
request is metered first — `MODEL_CALLS_PER_CLIENT_PER_HOUR` (default 20),
`MODEL_CALLS_PER_DAY` (default 200), `MODEL_MAX_PROMPT_CHARS` (default
60,000), counted across instances when Redis is configured. Over budget, the
fixed-rule fallbacks answer and the response says so. `GET /ai/status` reports
what is live, and never returns the key. Scans need no key at all: the app
reads them with Tesseract.js in the browser, which loads its engine and
English data from jsDelivr the first time.

**If you are here to evaluate this, start at `/judge`**: live checks against the
engine, and every measured figure with the file it came from. Then
[docs/FINAL_PITCH_SCRIPT.md](docs/FINAL_PITCH_SCRIPT.md) has the five-minute
narrative for a live demonstration.

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
  src/api/                   the HTTP surface: routes, request models, presentation, uploads
  src/settlement_run.py      what a run does after the solve — no HTTP in it
  src/orchestrator.py        the money path's entry points; layers enforced by
                             tests/test_architecture.py; mypy-clean
  scripts/                   benchmarks, calibration, stress runs, data fetch
  tests/                     806 tests
  benchmarks/                measurement snapshots
  data/                      Sanctions lists, test ledgers
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
  24.32% — 56.76% once the processor's payout cycle is learned from earlier
  payouts. Every condition produced **0 false clears**. That gap is the honest
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
  out-of-sample: the low band says 0.14 and is right 6.3% of the time (2 of 32).
  That band never clears anything — it sits far below 0.85 — so the cost is a
  reviewer seeing a slightly hopeful number on a proposal already sent to
  them, not a payment released. In-sample no bucket is overconfident; the last
  one that was, the ambiguous-match constant claiming 0.54 against 36.4%
  observed, is now set at its measured 0.36.
- **No live Razorpay account has been reconciled.** Test mode creates no
  settlements, so the API path is built to Razorpay's published contract; a
  merchant can reconcile the Settlement Recon report they download instead.
- **No payment processor publishes settlement data.** The real-data benchmark
  is government payables (two public checkbooks), not card or UPI payouts.
- **The investigator matches little on its own.** It puts 20 of 58 withheld
  benchmark cases right, all of them cases missing a member, where the answer
  is to wait or ask. On the 38 solvable ones it escalates, because none
  carries evidence that singles out one set, and no wrong match passes its
  verifier. Each escalation names the one payment whose answer settles the
  tie (2.46 answers on average) (`docs/AI_EVALUATION.md`).
- **Scanned statements are measured on generated scans**, in three layouts;
  a bank's own layout may read worse, and is then refused, not guessed.

What each part does, and what measured it, is in
**[Capabilities](docs/CAPABILITIES.md)**; the known edges and what would come
next, in **[Where it goes next](docs/ROADMAP.md)**.
