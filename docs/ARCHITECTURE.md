# AmongResolver — Architecture

A multi-source settlement reconciliation engine. Given a bank credit and feeds
from a payment gateway, a bank and an ERP, it identifies which transactions
compose that settlement, explains every decision, flags compliance problems
with the authority behind them, and produces the accounting entry a controller
would post.

**It never posts, and it never clears a batch it cannot justify.**

---

## The one design decision everything follows from

Reconciliation looks like an arithmetic problem — find the subset that sums to
the target — and it is not. We built it that way first and measured the result:

> **Subset-sum alone: 0.0% auto-clear accuracy across 120 scenarios.**

Not occasionally wrong. Unable to identify the right set at all. A solver may
use any subset size, so the number of subsets competing for one target is `2ⁿ`
— 1.2 × 10¹⁸ for a pool of only 60 — against a target that can take about
2 × 10⁶ distinct paise values. Millions of subsets hit the same rupee value.
A better solver does not fix an under-determined problem.

So the engine is built on a different premise:

> **Reconciliation is entity resolution first, arithmetic second.**
> Linkage identifies the members; the sum *verifies* them.

That reframe is what took accuracy from 0% to 94.6% on third-party data, and
it decides where every other component sits.

---

## Pipeline

```
  files (CSV / JSON, any schema)
        │
   ┌────▼─────────────────────────────────────────────┐
   │ Agent 0   File Understanding      file_agent.py  │
   │ Agent 0b  LLM header fallback  llm_header_mapper │  ← language
   └────┬─────────────────────────────────────────────┘
        │  rejects the file if a required field is missing
   ┌────▼─────────────────────────────────────────────┐
   │ Agent 1   Normalisation           ingestion.py   │
   └────┬─────────────────────────────────────────────┘
        │  integer cents, UTC, canonical refs
   ┌────▼─────────────────────────────────────────────┐
   │ Agent 7   Compliance          compliance_agent   │
   └────┬─────────────────────────────────────────────┘
        │  blocked transactions leave the pool
   ┌────▼─────────────────────────────────────────────┐
   │ Agent 2   Fee decomposition           fee_decomposition.py    │
   │ Agent 2b  Linkage                  linkage.py    │  ← the core idea
   └────┬─────────────────────────────────────────────┘
        │  50,000 candidates → 55
   ┌────▼─────────────────────────────────────────────┐
   │ Agent 3   Subset-sum (1:N, N:M)      subset_sum_nm.py │
   │ Agent 3b  Ambiguity tiebreak     fuzzy_match.py  │
   │ Agent 4   Greedy approx fallback fuzzy_match.py  │
   │ Agent 5   Exception diagnosis  exception_diagnosis  │
   │ Agent 6   Orchestration + governance             │
   └────┬─────────────────────────────────────────────┘
        │
   ┌────▼─────────────────────────────────────────────┐
   │ Agent 8   Cash position + posting  cash_position │
   │ Agent 9   Settlement Q&A         settlement_qa   │  ← language
   │ Agent 10  ERP Write-Back           erp_sync.py   │
   └──────────────────────────────────────────────────┘

*Also includes `razorpay_source.py`, which pulls `/v1/settlements` and
`/v1/settlements/recon/combined` via `scripts/pull_razorpay.py`, and
`webhook.py`, which receives signed `settlement.processed` events at
`POST /webhooks/razorpay`. The honest one-line summary of ingest is
**settlements are pushed, transactions are pulled**: a verified delivery
tells the engine a settlement has processed, but the payments it decomposes
into arrive on the gateway/bank/ERP feeds, which are still polled or
uploaded. So a delivery queues a settlement for reconciliation rather than
reconciling it — see `webhook.py`'s module docstring for why the boundary is
there. What it buys is real: the lag between a settlement landing and the
engine knowing drops from the poll interval to delivery latency.*

*The signature is the whole feature. An endpoint that accepts unsigned
settlement notifications is strictly worse than polling — polling at least
talks to an authenticated API, while an open webhook lets anyone who finds
the URL assert that a settlement of any amount has processed. Every delivery
must carry a valid HMAC-SHA256 over the raw body, compared with
`hmac.compare_digest`; an unset secret returns 503 rather than accepting
unauthenticated instructions about money, and replays are acknowledged
without being processed twice — on every instance, not just the one that saw
the first delivery: with `REDIS_URL` / `KV_URL` set, replay memory and the
pending queue live in the same shared store as the audit trail, and Redis's
atomic `SET NX` means two instances handed the same retry at once cannot both
treat it as new. Of the nineteen tests in `test_webhook.py`, ten are refusals
and five cover state shared across instances.*
```

`pipeline.py` is the single entry point. `audit.py` records every decision.

---

## Where AI is used, and where it is refused

Two places, both chosen on the same principle:

| | |
|---|---|
| **Agent 0b** — header mapping | Deciding that `booked_at` is a date is a **language** question. A hand-maintained synonym list cannot keep up with every bank's column names, and the failure is silent: an unmapped timestamp empties the field and the rows vanish. |
| **Agent 9** — Settlement Q&A | Explaining a reconciliation is a **language** question. Every figure comes from recorded state; the model explains, it does not compute. |

**The matching core is deliberately deterministic.** Deciding which
transactions compose a settlement is a **money** question. A hallucinated
sentence is embarrassing; a hallucinated amount is a loss. The zero-false-clear
result exists *because* those guards are rules a human can read and an auditor
can challenge.

Both AI paths are gated:

- **Agent 0b proposes; the deterministic layer decides.** It only runs when
  rules already failed, its output passes the same structural constraints (a
  fee column still cannot become `amount`) and the same rejection gate, and
  every LLM-derived mapping is flagged in the audit trail.
- **Agent 9 has no tools and no write path.** Transaction text is fenced as
  untrusted — memos arrive from uploaded files and are attacker-controllable —
  and the worst case is a misleading sentence beside correct structured numbers.

Both are off unless `GEMINI_API_KEY` is set. The engine runs identically
without them.

---

## The components that carry the result

### Agent 0 — File Understanding (`file_agent.py`)

A **gatekeeper**, not a guesser. Maps unfamiliar headers onto the internal
schema and **refuses the file** if `amount`, `timestamp`, or any identifier is
missing.

That refusal matters because the alternative is silence. A row with no
timestamp fails to parse during normalisation and is discarded — so an
unrecognised date column does not error, it deletes the source and the run
reports "nothing matched". On ReconRiver that lost **107 of 207 records** with
no error anywhere.

Assignment is best-first, one column per target. Fee/tax/discount columns can
never become `amount` — reconciling against fees produces a confidently wrong
answer that still looks arithmetically tidy. `ref_id` is multi-valued because
linkage tokenises it, so carrying both the order id *and* the settlement id
gives it the anchor it would otherwise never see.

### The transaction lifecycle (`orchestrator.py`, `subset_sum.py`)

Not every row in a feed is money that moved, and the ones that did do not all
move forwards.

**Statuses that never settled are dropped.** `status` was unmapped until
recently, so a FAILED payment entered the pool as spendable and could be named
as a settlement member. Twenty-nine states are now recognised as non-settling
across three groups: never went through (failed, declined, voided, reversed),
authorised but not captured (Stripe alone spells this four ways), and the bank
took it back or never posted it (returned, bounced, unposted, on_hold). Only
explicitly known states are dropped — a blank or unrecognised status is kept,
because most feeds carry none and inventing a reason to discard a payment is
the opposite of the point.

**Refunds are forced members, not choices.** A negative anchored to this
settlement happened and belongs; any set excluding it is wrong however well it
adds up. Left selectable, negatives create slack — twenty sales less three
refunds nets the same as seventeen sales alone — so nearly every settlement
containing a refund reported as ambiguous. Unanchored negatives stay optional,
because forcing one would assert membership the evidence does not support.

The same mechanism covers rolling reserve, chargebacks and partial refunds:
all are anchored negatives. `refunded` and `chargeback` on the ORIGINAL row are
deliberately NOT dropped — the clawback is its own row, and excluding both
would subtract the same reversal twice.

---

### Agent 2b — Linkage (`linkage.py`)

The component the architecture rests on. Four signals, weighted by how
forgeable each is:

| signal | weight |
|---|---|
| transaction names the settlement | 0.55 |
| shares a reference-token cluster | 0.25 |
| that cluster token also names the settlement | 0.15 |
| same amount in a different feed | 0.10 |

Anchoring matches by **containment on canonical forms**, not token overlap —
the same identifier is written `STL-2026-001`, `STL_2026_001` and `STL2026001`
by three systems on the same payment. Solving is **tiered**: anchored
candidates first, then strong links, then everything. A weak signal admitted
alongside strong ones does not add information, it adds degeneracy.

See [LINKAGE.md](LINKAGE.md) for the full reasoning and an honest pros/cons.

### Agent 8 — Cash position and posting (`cash_position.py`)

Turns a match into what a controller consumes: where the cash is, bucketed so
each bucket implies a **different next action**, and a balanced double-entry
proposal.

Two accounting choices worth stating: withheld tax is a **debit to a
receivable**, not an expense — it is money owed back. And the credit is the
gross **observed from matched transactions**, not reconstructed from net. If
those disagree the entry is **rejected with the discrepancy stated, not
plugged** — a book that balances because someone inserted a plug is worse than
one that visibly does not.

---

## Governance

**Nothing writes to a ledger.** Journal entries carry `status="proposed"` and
there is no code path that sets `"posted"`.

**Auto-clear requires evidence, not just arithmetic.** Three guards:

1. **Unanchored limit** — above **20** candidates with no anchor, an exact sum
   is a coincidence, not an identification. It was 25 until the realistic
   benchmark produced false clears at pools of 22 and 23; the guard was
   tightened to 20 and they stopped. `UNANCHORED_AUTOCLEAR_LIMIT` overrides it.
2. **Substitutability** — a member with an equal-amount twin in another feed
   outside the solved tier is not uniquely determined.
3. **Tie-out** — `matched gross − deductions − net` must equal zero. That is
   the difference between "the solver said yes" and "the money adds up".
4. **The 0.85 confidence gate applies unconditionally.** It used to exempt
   pools of 20 or fewer candidates entirely — a small pool with a weak
   reference-cluster signal (not "no evidence at all", which guard 1 already
   caught) could clear at a structural confidence around 0.22 with nothing
   withholding it. The gate now reads `result.confidence` regardless of pool
   size; a small pool gets no exemption from it.

These four are independent and any one of them can withhold a clear on its
own — which is why the false-clear rate has stayed at 0 even in benchmarks
where the raw confidence score itself is measurably not a well-calibrated
probability (see "Calibration" below). A confident-but-wrong proposal still
has to clear all four to actually post.

**Compliance rules declare their basis.** `statutory` / `regulatory_guidance` /
`internal_policy`, enforced by tests. The ₹5 crore ceiling that fires on the
demo is **not law** — no Indian statute caps a transaction at that value, and
the real duty is to *report* to FIU-IND, not block. Presenting it as statutory
would misrepresent the law to whoever relies on the output.

---

## Measured results

Each figure's command, file and history are in
[FINAL_TEST_REPORT.md](FINAL_TEST_REPORT.md); figures that were once wrong, and
why, are in [FAILURE_LOG.md](../FAILURE_LOG.md). The ones this design is judged
by:

| | |
|---|---|
| ReconRiver (third-party), references intact | 94.59% exact, 91.89% cleared and right, 0 false clears |
| ReconRiver, settlement references stripped | 24.32% exact; 56.76% with the payout cycle learned; 0 false clears |
| Batch close, 738 records, 60 settlements | 95.0% match rate, 0 false clears, 0 false alarms |
| Own benchmark, 120 scenarios | 92% cleared and right, 95% true set identified, 0 false clears |
| 50,000 records, one settlement | exact 55/55, precision and recall 1.0, 3.1–3.2 s to reconcile |
| Calibration, own benchmark, 180 (in-sample) | ECE 0.0902, all underconfidence; 138 of 138 right at or above the 0.85 gate |
| Calibration, ReconRiver, 74 (out-of-sample) | ECE 0.0824 raw, 0.0419 as shown; 42 of 42 right at or above the gate |

**Where the confidence number can be trusted.** No figure is higher than the
lowest accuracy measured for that kind of answer on any corpus, and no bucket
is overconfident on either calibration corpus, raw or as shown. At and above
the 0.85 gate every prediction was the exact true set, so the number errs low
there, which costs review time rather than money. Below it, an ambiguous set,
a fuzzy recovery bundle and a sum with no other evidence report 0.00: they were
right 4 of 12, 2 of 28 and 0 of 70 times, 0 of 212 and 1 of 27, across the
benchmark, ReconRiver and the 1,000-case suite. The calibrated figure shown
beside the raw one is never above it below the gate (FAILURE_LOG 58).

**Why the numbers are reproducible.** CP-SAT's parallel search is not
deterministic: where more than one subset satisfies the sum, whichever worker
finishes first wins, and three different figures for one ECE once circulated
for that reason. The benchmarks pin the solver to one worker, and three runs
of 180 scenarios differ in zero. Production keeps the parallel search, since
those cases report low confidence and never clear.

---

## The module that was split

`engine/src/main.py` was ~2,060 lines: 20 route handlers and about 30
functions of business logic that could not be tested without the API layer.
It is now `engine/src/api/` and the modules beside it:

| module | lines | holds |
|---|---:|---|
| `api/models.py` | 98 | request models, and the conversion to `SettlementBatch` |
| `api/presentation.py` | 536 | everything that turns a report into what a controller reads |
| `api/routes_decisions.py` | 336 | what a human decided, and the audit record of it |
| `api/routes_reports.py` | 204 | read-only regulator and auditor views |
| `api/routes_reconcile.py` | 317 | reconcile, multi, joint, upload, queue, settlements/detect — parsing only |
| `api/routes_ops.py` | 204 | the audit trail and its check, the demo, readiness |
| `api/uploads.py` | 147 | the upload size cap, and the one ingest path upload and queue share |
| `settlement_run.py` | 300 | what a run does after the solve: paid-out check, learning, investigator, open items, history |
| `main.py` | 160 | app construction, CORS, the two middlewares, route wiring |
| `orchestrator.py` | 461 | the two entry points, `reconcile_batch` and `reconcile_many` |
| `tiered_solve.py` | 510 | solving in evidence tiers, the substitutability guard, tiebreak, fuzzy recovery |
| `recon_report.py` | 208 | the report, its tie-out arithmetic, the fee audit on the matched set |
| `candidate_filters.py` | 104 | which records can be members: the settlement's currency, money that moved |
| `stores.py` | 67 | the one public door to Redis and SQLite, and the registry of every in-process fallback (`/health` reports what it holds) |
| `fee_audit.py` | 383 | gateway fee, GST-on-fee and settlement-shortfall checks; the entry point |
| `tax_audit.py` | 155 | e-commerce TDS (194-O / 393(1)) and GST TCS (Section 52), by payment date |
| `fee_model.py` | 111 | the finding types and fee-field helpers both share |

`models` and `presentation` import nothing from FastAPI beyond pydantic, so
they run without an app. The test that checks every upload goes through the
size cap used to read `main.py` alone, and would have passed vacuously once an
endpoint moved; it now scans every module under `src/`.

---

## Layers, enforced by tests

The engine keeps one flat folder of modules, and the layering is a set of rules
the build checks (`engine/tests/test_architecture.py`), not a diagram:

| Layer | Modules | May import |
|---|---|---|
| interface | `main`, `auth`, `api/*` | anything below |
| application | `settlement_run`, `pipeline`, `investigation_agent`, `razorpay_recon`, … | the money path, stores, models (where opt-in) |
| money path | `orchestrator` and every module it reaches | never a model, HTTP or `api/` |

1. The money path never reaches a model, FastAPI, Starlette or `api/`, even
   through a lazy import inside a function. `test_no_model_decides` checks the
   same property by behaviour; this checks it in the import graph.
2. Only the interface layer imports FastAPI or Starlette.
3. Nothing imports `main`.
4. Only the interface layer, and `settlement_run` (which shapes the response
   with `api.presentation`), import from `api/`.
5. No import cycles at load time.

CI also runs mypy (clean across `src/`), pylint for errors, and bandit
before the tests.

## Measured under load

`engine/scripts/load_test.py` drives one engine process in-process (ASGI, no
network, no model, SQLite store) at rising concurrency; the figures are in
`engine/docs/benchmarks/load_test.json`.

| request | 1 at a time | 4 at once | 8 at once |
|---|---|---|---|
| upload (sample payout, 3 files, 91 candidates) | 9.8/s, p95 110 ms | 10.0/s, p95 524 ms | 7.3/s, p95 1260 ms |
| Razorpay, five payouts checked five ways | 8.0/s, p95 158 ms | 8.9/s, p95 570 ms | 9.0/s, p95 1222 ms |
| `/health` | 477.6/s, p95 2 ms | 566.3/s, p95 11 ms | 637.1/s, p95 18 ms |

Zero errors at every level. One process tops out near ten reconciliations a
second: the solve is CPU-bound and Python runs it on one core at a time, so
beyond that requests queue rather than fail. More throughput is more
processes or instances, which is how Vercel serves it. The first run of this
test crashed: one SQLite connection was shared across the thread pool and
used by several stores at once. Each thread now has its own (FAILURE_LOG 45).

## How this scales beyond one process — a design, not shipped code

A streaming version of this engine was prototyped: Kafka topics for raw and
normalised transactions, reconciliation workers holding a Redis lock per
settlement, Prometheus counters for solver latency and auto-clear rate. None
of it is in this repository, and the reason is worth recording.

It was never wired to anything. No endpoint started a consumer, nothing
imported the workers, `kafka` and `prometheus_client` were missing from
`requirements.txt`, and the live deployment is serverless, which cannot run a
long-lived consumer or a metrics server at all. Code in `src/` that no request
can reach is not a feature — it is a claim, and this project's first
submission already paid for one of those (a webhook the README described and
the code did not contain). So the prototype was removed and the design is
written down here instead.

What already carries over to a multi-instance deployment, because it was
built for Vercel:

- **Shared state.** Audit trail, run history, webhook replay memory and the
  chargeback queue all live in Redis when `REDIS_URL`/`KV_URL` is set, so
  consecutive requests landing on different instances agree.
- **Idempotent ingest.** Webhook deliveries are de-duplicated with an atomic
  `SET NX`, so a retry reaching a second instance is still recognised.
- **Stateless reconciliation.** Every request carries its own inputs; no
  worker holds a settlement between calls.

What a streaming deployment would add, and what each piece is for:

| Piece | Why it would exist | Not needed yet because |
|---|---|---|
| Ingest topic | absorb bursts of gateway files | uploads are per request |
| Lock per settlement | stop two workers solving one batch | one request solves one batch |
| Metrics endpoint | alert on solver timeouts, clear-rate drift | `/health` + the audit trail |

## Known limitations

- **Below the gate, confidence is a ranking, not a probability** (above).
- **Sanctions screening is exact-match against a point-in-time list**, with a
  near spelling flagged for a person; no transliteration or date-of-birth
  matching (below).
- **Linkage weights are hand-set**, with a learned Fellegi-Sunter model beside
  them; a sweep puts them within 3 points of the best (FAILURE_LOG 2).
- **Where nothing ties records to a settlement, the engine declines.**
  Correct, but a declined settlement is still a person's work.
- **Model APIs can be unavailable or out of quota.** Every model use retries
  transient failures, then falls back to the deterministic result and says so.
- **Tamper-evidence stops at the hash chain.** A receipt proves the trail was
  not rewritten against the chain; a store the process itself cannot rewrite
  is a deployment concern.

---

## Running it

```bash
cd engine
pip install -r requirements.txt
uvicorn main:app --app-dir src --port 8001
```

```bash
python scripts/run_50k_stress_test.py     # scale + tie-out
python scripts/benchmark.py --declare-source   # accuracy, own data
python scripts/run_reconriver.py          # accuracy, third-party data
python scripts/pull_razorpay.py --month YYYY-MM   # live Razorpay settlements
python scripts/close_batch.py --generate  # batch close: match rate + exceptions
python scripts/calibration.py             # is the confidence real
python -m pytest tests/ -q                # 824 tests
```

Frontend: `npm run dev` (port 8080).


## Sanctions screening

`SANCTIONS_HIT` is the only rule that blocks funds on a statutory basis. Its
list is the UN Security Council Consolidated List, which India implements
through the UAPA Order: public, free and machine-readable.
`python scripts/fetch_sanctions_list.py` writes it to
`data/sanctions/un_consolidated.txt` with the source URL, the list's own
generation date and when it was retrieved; `SANCTIONS_LIST_PATH` points at a
different list. A snapshot is tracked so a deployed engine, which is built
from git, screens against the real list (3,422 identifiers); CI fetches a
fresh one, and a list older than 30 days is warned about by age.

`/compliance/rulebook` and `/compliance/attestation/{batch_id}` say which list
was used: `is_illustrative`, `entry_count`, `list_generated`, `retrieved` and
the match mode. With no list at all the engine screens four illustrative names
and says so at every startup, and `test_compliance.py` skips its real-list
check by name rather than pass quietly against them.

Names are folded to uppercase alphanumerics first, so `Al-Qaida`, `al qaida`
and `AL QAIDA` are one designation; the fetch script and the agent each fold,
and a test asserts the two agree. An exact match blocks; a near spelling (85%
similar) or a low-quality alias is flagged for a person. A transliteration
the list does not carry, or two people sharing a name, is not caught, and the
rulebook says so.
