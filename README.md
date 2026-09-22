# AmongResolver

[![CI](https://github.com/RishiMaara/Among_Resolver/actions/workflows/ci.yml/badge.svg)](https://github.com/RishiMaara/Among_Resolver/actions/workflows/ci.yml)

**Settlement reconciliation for Indian payments.** It works out which payments
make up each payout, proves the answer to the paisa, and refuses to clear
what it cannot prove — then says what a person should do next. Across every
corpus it has been measured on, it has never cleared a wrong set.

Built for the Razorpay AI Buildathon, Track 04 — AI Finance Controller.
**[Live demo](https://among-resolver.vercel.app/)** ·
**[Judge it in five minutes](https://among-resolver.vercel.app/judge)** — live
checks against the engine, and every figure below with the file it came from.

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

## Core Capabilities

1. **N:M (Many-to-Many) Reconciliation:** `POST /reconcile/joint` solves several settlement targets in one CP-SAT model, so a transaction contested between two batches is resolved by what the *other* batch needs, not by whichever batch happened to run first. It reuses the same evidence-based safety the 1:N path has — linkage narrows each batch's own candidates, an anchored refund is still forced, an unevidenced arithmetic match is still withheld — and falls back to running the proven 1:N path per batch if the joint model can't satisfy every target at once, so N:M is never worse than calling the 1:N path on each batch separately. See `engine/src/subset_sum_nm.py` and `orchestrator.reconcile_many`.
2. **Anchorless Math Fallbacks:** If the pool is too massive for exact subset-sum, the engine gracefully degrades to a custom greedy approximation solver (`approximate_subset_sum_greedy`) rather than timing out.
3. **ERP Journal Sync (no real ERP has received one):** On a genuine clear, the engine builds a balanced double-entry journal and POSTs it to `ERP_JOURNAL_URL`. Unset by default, and unset means **nothing is posted and the journal says `no_target_configured`** — it does not quietly succeed. This module previously caught an unreachable endpoint and returned success with the journal marked `posted_mock`, so a connection refused left the books showing a journal as posted that no ERP ever received; a crash gets investigated, a false success gets reconciled against next month. Every outcome is now distinct (`posted`, `posted_to_mock`, `rejected_by_erp`, `unreachable`, `no_target_configured`, `skipped_unbalanced`) and only the first two return true. The payload is the shape NetSuite/Tally/QuickBooks accept, with exact integer paise carried alongside each decimal — but "would be accepted" is a design claim, not a measurement. Ingestion is **settlements pushed, transactions pulled**: `POST /webhooks/razorpay` receives signed `settlement.processed` events (HMAC-SHA256 over the raw body, constant-time compare, replay-suppressed across every instance through the shared Redis store, 503 rather than accepting an unsigned delivery), while the payments a settlement decomposes into still arrive by CSV upload or the `razorpay_source.py` pull. So a verified delivery queues a settlement for reconciliation rather than reconciling it — the transaction feed is what reconciliation needs and the webhook does not carry it.
4. **Learned Linkage Weights:** A Fellegi-Sunter model (`linkage_em.py`) with m-probabilities learned by EM from anchored records, or from the processor's payout cycle as read off verified clears — beside the reasoned 0.55/0.25/0.15/0.10 weights, which remain unfitted. An earlier version of this line claimed "machine-learned" weights that were in fact word counts (FAILURE_LOG 21).
5. **Deterministic LLM Fallbacks:** When external LLM APIs fail (latency or 503s), the engine seamlessly falls back to local deterministic string similarity (`difflib`) to keep the pipeline moving.
6. **AML Screening:** Compliance rulebook enforcement against the UN Consolidated Sanctions List — exact match after normalisation (no fuzzy, transliteration or DOB matching; `docs/ARCHITECTURE.md` states this alongside the caveat it implies). Without a fetched list it screens four illustrative names and says so loudly (see "Running it").

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
| Tests | **733** backend · **136** frontend |

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
  src/api/                   the HTTP surface: models, presentation, route groups
  scripts/                   benchmarks, calibration, stress runs, data fetch
  tests/                     733 tests
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
- **Two agents call an LLM (Gemini)** — header mapping, when rules cannot find
  a required column, and settlement Q&A. Both are off unless `GEMINI_API_KEY`
  is set. Fuzzy matching *can* use a small embedding model (`all-MiniLM-L6-v2`)
  when `sentence-transformers` is installed; the shipped build does not install
  it, so in practice that path is lexical. All of them propose; none decides,
  and no model sits on the money path.
- **The AI's answers are checked, not just instructed.** Every figure,
  transaction id and date in a Q&A answer is traced back to the settlement's
  recorded results before it is shown (`grounding_check.py`). One that does
  not trace means the answer is withheld, logged, and replaced with the
  engine's own summary. It matches by value, not meaning — a real number on
  the wrong noun passes — and the tests pin that edge.
- **Exceptions are ranked by the money waiting on them.** Each carries its
  rupees at stake, the list is sorted largest first, and the results screen
  states the total value waiting on review and its share of the settlement —
  a payment named by two exceptions is counted once in that total.
- **Fees and tax are audited per payment method** (`fee_audit.py`), and the
  findings come back in every response under `fee_audit`: UPI, cards,
  netbanking and wallets against a contract rate card, GST at 18% on the fee
  rather than the principal, e-commerce TDS and GST TCS. Each payment is
  checked under the law **on its own date** (`india_tax.py`): TDS cites
  Section 194-O until 31 March 2026 and Section 393(1) Table Sl. 8(v) of the
  Income-tax Act 2025 from 1 April, at 0.1% (1% before October 2024); TCS
  under Section 52 CGST is 0.5% from 10 July 2024, on supplies net of
  returns, and is checked only when the data reports it. A batch straddling
  1 April cites both Acts. Razorpay's `fee` column includes its GST and is
  read that way. `POST /tax/itc-check` compares a month's deducted GST with
  the gateway's invoice: what can be claimed as input tax credit, and what
  needs a debit or credit note first. Whether any of this applies depends on
  the merchant's arrangement, so every check is configurable; it is
  arithmetic, not tax advice.
- **Open items, carried across runs** (`GET /open-items`). Every
  reconciliation adds the payments it saw that are not yet paid out, the
  refunds not yet deducted and the payouts it withheld, and closes whatever a
  cleared settlement took. What is left is aged in **working days** against a
  T+2 due date — Sundays, second and fourth Saturdays and the state's bank
  holidays skipped (`india_calendar.py`, Maharashtra by default, correctable
  by environment variable without a release). A card payment captured on
  Friday 11 September 2026 is due Wednesday the 16th, not Sunday the 13th;
  `GET /calendar/due` shows every skipped day and why.
- **Separation of duties on posting approvals.** Whoever accepted a match —
  by confirming the batch or accepting the oldest-first convention — cannot
  approve the posting that rests on it; the attempt is refused and recorded.
  The rule reads the audit trail, so it holds across instances. Identity is
  the name a reviewer types, so it stops the accident and the habit, not a
  determined person; with real user identity it keys on a user id instead.
- **Chargebacks add a row; they never edit a closed settlement.** Filing one
  (`POST /chargebacks`) emits a negative reversal that waits until a later
  payout absorbs it — opt-in per reconciliation, and only reversals filed
  inside that settlement's window are taken, so one the payout could not
  absorb is never dropped from the queue.
- **What linkage buys, measured** (`scripts/baseline_comparison.py`). The same
  engine with linkage disabled identifies the true set in 0% of 120 benchmark
  scenarios; with it, 65%, with zero wrong approvals in both arms. This is
  deterministic entity resolution, not AI — the script says so, because it
  was first written claiming otherwise.
- **Learned linkage for payouts with no reference** (`linkage_em.py`,
  `settlement_cycle.py`). A Fellegi-Sunter model — the method behind Splink —
  with m-probabilities learned by EM from anchored records, or from the
  processor's settlement cycle as read off earlier verified clears (T+1, T+2:
  the engine learns which). On ReconRiver with every settlement id stripped,
  exact sets identified rise from **24.3% to 56.8%**, zero false clears, with
  the cycle learned only from other scenarios. They arrive as proposals, not
  auto-clears: seven measured cases do not justify releasing money. Every
  response carries what the model learned (`summary.learned_linkage`).
- **Razorpay as the primary feed** (`razorpay_recon.py`,
  `POST /razorpay/reconcile` live with keys, `/razorpay/reconcile/upload`
  with saved API responses). The Settlement Recon API already says which
  payments, refunds and adjustments each payout contains, so re-deriving that
  by subset-sum would be solving a solved problem worse. The engine checks
  what the list does not prove: the lines sum to the payout **to the paisa**;
  a **blind re-solve** with settlement ids stripped and capture times only —
  the payout cycle learned from the *other* settlements, never the one being
  checked — reaches the same set; the bank credit carries the settlement's
  UTR and exact amount; every order is in the books; fees, GST, TDS and TCS
  are right for their dates. On the sample (`public/sample-data/razorpay/`,
  invented values in Razorpay's published shapes) three payouts verify, one
  has a 2.5% card fee and an unbooked order, one arrived ₹10 short at the
  bank, and the blind solve reaches Razorpay's exact set on all five. Not
  run against a live account: test mode creates no settlements. A merchant can
  instead upload the **Settlement Recon report they download from the
  Dashboard** (`settlement_report.csv` in the sample is the same payouts in
  that form) — no keys, and on a self-hosted engine nothing leaves their
  machine. Without a settlements list each payout's amount is its lines' sum,
  so the tie-out is marked as by construction and the bank credit (UTR and
  amount) is the independent check; a report read in the wrong unit is off by
  100x from its bank credit and is not verified.
- **Bank statements as banks send them** (`statement_parsers.py`): SWIFT
  MT940, ISO 20022 CAMT.053, OFX and text PDFs, straight into the bank slot of
  any reconciliation, or `POST /statements/parse` to check the read on its own.
  Every parse proves itself with the rule the bank guarantees — opening +
  credits − debits = closing, and the running balance line by line (the
  Golden Rule of the open-source `bankstatementparser`, whose approach this
  follows without its lxml/pandas<3 dependencies). On a PDF the running balance
  also decides which column an amount was in. A statement that does not
  balance is refused with the arithmetic shown, never partly used. A scanned
  PDF or a photo is read by Tesseract.js in the browser (`src/lib/scan-ocr.ts`,
  pdf.js to render the page) and the text goes through the same parser; if
  it does not balance, Gemini reads the file itself (`statement_ocr.py`) and
  is held to the same check. On 24 deliberately noisy scans the browser read
  11 exactly right and Gemini 12 of the other 13; no wrong reading was ever
  accepted (`scripts/ocr_eval.py`, `docs/benchmarks/ocr_eval.json`). Samples of
  one account in all four formats, and as a scan, are in
  `public/sample-data/statements/`.
- **Exceptions filed the way a finance team routes them**
  (`exception_taxonomy.py`): in transit, missing in books, unidentified
  receipt, booked but not at the gateway, duplicate, split or partial, amount
  mismatch, compliance hold — each exception carries its category, whose desk
  it goes to and the first thing to do, and the summary counts them with the
  money at stake. Fixed rules, so a misfiled break can be read and corrected.
- **The approved posting, as a Tally import file**
  (`POST /settlement/{batch_id}/export/tally`). Tally XML (ENVELOPE → VOUCHER),
  debits deemed positive with negative amounts as Tally expects, ledger names
  from the company's own chart of accounts. Produced only for a balanced
  journal a person approved — and separation of duties means the approver is
  not whoever accepted the match — and recorded in the audit trail with who
  exported it. The engine still posts nothing; Tally does, on import.
- **AI where it measured better, and a check in code wherever it is used**
  — [docs/AI_EVALUATION.md](docs/AI_EVALUATION.md) has every figure and the
  script behind it. Bank narrations (UTR, settlement ref, payer, rail): a
  grounded model reads 97.5% of fields right on bank formats the rules never
  saw, against 82.7% for the rules, and no value it returns is kept unless it
  appears in the narration. Withheld settlements: an investigator proposes one
  typed action — match, wait, request a document, write off rounding,
  escalate — and a verifier checks the arithmetic, the ledger, the calendar,
  the evidence and every figure in the reason before a reviewer sees it; on
  58 withheld benchmark cases it puts 14 right, verified proposals in front
  of a reviewer against 2 from fixed rules, with 4 wrong ones getting through
  where the evidence itself misleads. Questions about a result: 44 of 44 on a
  small set, facts from the record and refusals where the record cannot
  answer. Confidence is shown calibrated as well as raw (out-of-sample ECE
  0.090 → 0.026), and reviewer decisions flag drift. The first investigator
  measurement read the benchmark's labels and was thrown away; the write-up
  says how.
- **A tamper-evident audit trail.** Every entry carries the SHA-256 of the
  entry before it; edit a word, delete a line or reorder two and
  `GET /audit/{batch_id}/verify` names the first entry that no longer checks
  out. A chain cannot see its own tail cut off, so every reconciliation also
  returns its head hash (`audit_head`) as a receipt, and verifying against the
  receipt catches truncation. Tamper-evident, not tamper-proof: someone with
  write access can rebuild a chain, but not one that matches a receipt
  somebody else already holds.
- **Scale.** 200,000 records in one settlement: the exact 55-transaction
  true set, precision and recall 1.0, no exceptions
  (`scripts/run_scale_proof.py`). Throughput measured 13,700–26,900
  records/second across two runs on the same laptop — load on the machine
  moves it by 2x, so the range is the honest figure.

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
predictions concentrates the residual error in the low band, which is
OVERconfident — it says 0.14 and is right 6.3% of the time (2 of 32; it was
1 of 32 before FAILURE_LOG 31, which also brought out-of-sample ECE to
0.0901). An earlier
version of this paragraph called that underconfidence; it is the opposite.
It costs nothing only because that band never clears anything: every
proposal in it goes to a person. The calibrated figure shown beside it
(`calibration_map.py`) says 0.09, which is closer to the truth.

**Fees assume one rate card per batch.** Real Indian settlements mix payment
methods at different rates — UPI near zero, cards around 2%, netbanking often
flat — so a blended card produces a wrong gross target on a mixed day. It
fails safe: a wrong target ties out to nothing and the batch is withheld
rather than mismatched, and the engine says out loud that it estimated. It is
still the limitation most likely to meet a real merchant first.
