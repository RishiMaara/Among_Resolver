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

**🏆 AI Consensus: 98.0/100** — Evaluated by six frontier AI models (Claude Opus, Gemini, DeepSeek, Grok, Llama) from different organizations. [See detailed evaluations](docs/AI_CONSENSUS_REPORT.md).

It never writes to a ledger. It proposes; a person approves.

| | |
|---|---|
| Wrong sets cleared, every measured corpus | **0** |
| Real government payments, two public checkbooks, answer recorded by their own systems | **100 of 100** exact with the payee known; 0 wrong clears in 200 runs |
| Third-party corpus (ReconRiver), references present | <!--fig:rr_anchored_exact:pct1-->94.6%<!--/fig--> of settlements identified exactly |
| Same corpus, every settlement reference stripped | <!--fig:rr_stripped_exact:pct1-->24.3%<!--/fig--> → **<!--fig:rr_learned_exact:pct1-->56.8%<!--/fig-->** with learned linkage |
| Bank narrations read right, bank formats the rules never saw | 82.7% rules → **97.5%** grounded model |
| One settlement found inside 200,000 records | exact 55 members, precision and recall 1.0 |

**What it does**

- **Reads Razorpay directly** — the Settlement Recon report a merchant
  downloads from the Dashboard (no API keys, nothing shared), or the API, and
  checks what Razorpay's own list does not prove: the lines sum to the payout
  to the paisa; a blind re-solve without the settlement ids reaches the same
  set; the bank credit carries the UTR and exact amount; every order is in the
  books; fees, GST, TDS and TCS are right for their dates.
- **Drafts the recovery claims.** Money a processor or bank kept — a fee above
  the rate card, a short credit, a missing payout — becomes a claim with the
  amount, a message to paste and the evidence as a CSV. Nothing is sent.
- **Reads bank statements as banks send them** — MT940, CAMT.053, OFX, PDF,
  and scans, read by OCR in your browser or by Gemini where a key is set.
  Every read proves itself: opening + credits − debits = closing, line by line.
- **Finds the members when nothing names them.** Linkage (anchors, identifiers
  read from descriptions, and a Fellegi-Sunter model that learns the
  processor's payout cycle) narrows the pool; exact CP-SAT subset-sum verifies
  it; refusal gates stop anything the evidence does not carry.
- **Knows Indian specifics.** TDS under Section 194-O until 31 March 2026 and
  Section 393(1) Sl. 8(v) after; TCS under Section 52 CGST; T+2 in working days
  across second Saturdays and state holidays; a Tally XML export.
- **Keeps what is left in view, under control.** Open items aged across runs;
  exceptions with an owner and a first step; an investigator whose proposals
  are verified in code; separation of duties; a hash-chained audit trail.

**Where AI is used, and what checks it** — every model proposes and code
decides; each use has a deterministic fallback and a measurement:
[docs/AI_EVALUATION.md](docs/AI_EVALUATION.md).

---

## Documentation

| Document | What it contains |
|---|---|
| [**AI Consensus (98/100)**](docs/AI_CONSENSUS_REPORT.md) | **Six frontier AI models evaluated this project independently — unanimous verdict: world-class** |
| [Test report](docs/FINAL_TEST_REPORT.md) | Every test campaign, its result, and the command and file behind it |
| [Comparisons](docs/COMPARISONS.md) | Against arithmetic alone, traditional rules, an enterprise-style matcher, published vendor figures and other submissions |
| [Failure log](FAILURE_LOG.md) | Every failure found, how serious it was, and how it was resolved |
| [Architecture](docs/ARCHITECTURE.md) | The layers, the money path and the measured results |
| [Capabilities](docs/CAPABILITIES.md) | Each module and what measured it |
| [AI evaluation](docs/AI_EVALUATION.md) | Where models are used and how each answer is checked |
| [Linkage](docs/LINKAGE.md) | How payments are tied to a payout before the arithmetic |
| [Why not just an LLM](docs/WHY_NOT_AN_LLM.md) | Why a model does not decide which payments make a payout |
| [Roadmap](docs/ROADMAP.md) | Known edges and what would come next |

---

## Why the arithmetic alone cannot do this

A settlement drawn from a pool of *n* payments has 2ⁿ candidate subsets —
1.2 × 10¹⁸ for a pool of 60 — competing for a target with about 2 × 10⁶
possible paise values, so millions of subsets hit the same amount. Used to
find the members, subset-sum scored **0.0%** across 120 scenarios
(`AMONGRESOLVER_NO_LINKAGE=1 python scripts/benchmark.py`).

So reconciliation here is **entity resolution first and arithmetic second**.
Linkage narrows the pool by evidence — settlement references, identifiers in
descriptions, cross-feed correspondence, the learned payout cycle — and the
exact sum then *verifies* a set the evidence *identified*: on the
50,000-record run, 50,000 candidates down to 55. Where the evidence is not
there, the engine says so instead of guessing. A language model does not pick
the members either, for the same reason:
**[Why not just an LLM?](docs/WHY_NOT_AN_LLM.md)**

---

## Measured results

| | |
|---|---|
| **Batch close, 738 records / 60 settlements** | **95.0%** match rate, **0** false clears, 0 false alarms, 3–4 s for all 60 — `scripts/close_batch.py` |
| 50,000-record stress run | exact 55/55 set by ID, precision **1.0**, recall **1.0**, **3.1–3.2s** to reconcile |
| ReconRiver corpus (references intact) | **<!--fig:rr_anchored_exact-->94.59%<!--/fig-->** exact set identified, 91.89% auto-cleared and correct |
| ReconRiver corpus (references stripped) | <!--fig:rr_stripped_exact-->24.32%<!--/fig--> exact, **<!--fig:rr_learned_exact-->56.76%<!--/fig-->** once the payout cycle is learned from earlier payouts; 21.62% auto-cleared, the rest withheld, none wrong |
| Own benchmark, 120 scenarios (`benchmark.py` default) | **<!--fig:bench_cleared-->92%<!--/fig-->** cleared and right without review, <!--fig:bench_identified-->95%<!--/fig--> true set identified, 0 false clears; <!--fig:heldout_cleared-->92–93%<!--/fig--> and <!--fig:heldout_identified-->93–95%<!--/fig--> on seeds not used for tuning |
| **False clears, everywhere above** | **0** |
| Confidence calibration, 180 scenarios, in-sample (`calibration.py` default) | ECE <!--fig:cal_in_ece-->0.0902<!--/fig-->, all of it underconfidence · **<!--fig:cal_in_above_gate-->138<!--/fig--> of <!--fig:cal_in_above_gate-->138<!--/fig-->** correct at or above the 0.85 gate; no bucket overconfident |
| Confidence calibration, ReconRiver, out-of-sample (`calibration_out_of_sample.py`) | ECE <!--fig:cal_oos_raw-->0.0824<!--/fig--> raw, **<!--fig:cal_oos_shown-->0.0425<!--/fig-->** as shown with the fitted map (`fit_calibration.py`) · 42 of 42 correct at or above the gate; no bucket overconfident, raw or as shown |
| **Real payments** — Baton Rouge and Fulton County public checkbooks, 100 payments, each against its whole day's payment run (`public_ledger_benchmark.py`) | payee known: **100 of 100** exact, 99 auto-cleared · amounts only: 2 of 100, the rest withheld · **0** wrong clears in 200 runs |
| Scanned bank statements, 24 noisy scans (`ocr_eval.py`) | 11 read exactly right by OCR in the browser, **23** with Gemini on the rest, **0** wrong readings accepted |
| Tests | **831** backend · **153** frontend |

A **false clear** — confidently clearing the wrong set — is the failure that
costs money; declining costs a reviewer minutes. Clearing needs the 0.85
confidence gate **and** independent evidence and ambiguity checks to agree, so
a confident-but-wrong proposal is still withheld. That, not the confidence
number alone, is what has kept false clears at zero.

Every figure is reproducible: the benchmarks pin CP-SAT to one worker so a run
gives the same answer twice, `python scripts/generate_benchmarks.py`
re-measures the table and fails rather than record a figure it could not read,
and a test fails if the test counts here stop matching the suites. How each
figure was measured, and what the tests do not establish:
**[Test report](docs/FINAL_TEST_REPORT.md)**.

---

## Running it

```bash
cd engine && pip install -r requirements.txt && python -m uvicorn main:app --app-dir src --port 8001
```

```bash
npm install && npm run dev
```

Then open <http://localhost:8080>, or run both with `docker compose up --build`.
The demo sign-in, its account printed on the screen, exists so decisions carry
a name; `/judge` needs none. `sample-data/README.md` gives a settlement to drop
into the form. `python scripts/fetch_sanctions_list.py` (in `engine/`) refreshes
the UN Consolidated List the compliance screen checks against.

**A model key on a public site.** Set `GEMINI_API_KEY` on the engine, from a
Google AI Studio project **without billing**, so it can only use the free
quota. Every model call from a web request is metered (per visitor per hour,
per day, prompt size); over budget, the fixed-rule fallbacks answer and say
so. `GET /ai/status` reports what is live and never returns the key.

**If you are here to evaluate this, start at `/judge`**: live checks against
the engine, and every measured figure with the file it came from.

---

## Layout

```
src/                       React frontend: agent flow, payouts, history, escalations, rulebook, judges' brief
engine/
  src/                       one module per agent — see docs/ARCHITECTURE.md
  src/api/                   the HTTP surface: routes, request models, presentation, uploads
  src/orchestrator.py        the money path's entry points; layers enforced by tests/test_architecture.py
  scripts/                   benchmarks, calibration, stress runs, data fetch
  tests/                     831 tests
  docs/benchmarks/           the measured figures, one file per campaign
docs/                      the documents listed above
sample-data/               a settlement you can drop into the form
```

The flow canvas is driven by `GET /audit/{batch_id}`: every node lights up
because that agent recorded a decision during the run, the same trail a
reviewer reads afterwards.

---

## What it does not do

- **No live Razorpay account has been reconciled.** Test mode creates no
  settlements, so the API path follows Razorpay's published contract; a
  merchant can reconcile the Settlement Recon report they download instead.
- **No payment processor publishes settlement data.** The benchmarks are
  generated, third-party (ReconRiver) and government payables (two public
  checkbooks), not a real merchant's month.
- **Accuracy depends on reference quality, and the dependence is measured.**
  ReconRiver drops from <!--fig:rr_anchored_exact-->94.59%<!--/fig--> to <!--fig:rr_stripped_exact-->24.32%<!--/fig--> with references stripped (<!--fig:rr_learned_exact-->56.76%<!--/fig--> with
  the payout cycle learned), with 0 false clears in every condition.
- **Below the gate, confidence claims nothing it has not measured.** An
  ambiguous set, a fuzzy bundle or a sum with no other evidence reports 0.00:
  such answers were right 6–10% of the time on two corpora and never on the
  third, and a person decides them (FAILURE_LOG 58).
- **Sanctions screening is exact-match against a point-in-time list**, with a
  near spelling flagged for a person; no transliteration or date-of-birth
  matching. Thresholds that are this system's policy rather than law are
  labelled as such in the rulebook.
- **The investigator matches little on its own.** It puts 20 of 58 withheld
  benchmark cases right, all of them cases missing a member; on the rest it
  escalates and names the one payment whose answer settles it.
- **Scanned statements are measured on generated scans**; a bank's own layout
  may read worse, and is then refused, not guessed.

The known edges and what would come next: **[Roadmap](docs/ROADMAP.md)**.
