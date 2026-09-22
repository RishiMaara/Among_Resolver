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

**ReconRiver** — third-party dataset, ingested through Agent 0, ground truth by
transaction ID:

| condition | batches | exact | auto-cleared correct | false clears | mean latency | max latency |
|---|---|---|---|---|---|---|
| anchored | 37 | **94.59%** | 91.89% | **0** | 0.36s | 4.20s |
| settlement id stripped | 37 | 24.32% | 21.62% | **0** | 0.87s | 4.67s |

**Batch close** — 738 records across 60 settlements, every one attempted,
hazards seeded at one settlement in sixteen:

| records | settlements | match rate | false clears | false alarms | throughput |
|---|---|---|---|---|---|
| 738 | 60 | **95.0%** | **0** | **0** | 190–250 rec/sec (3–4 s) |

Every unresolved settlement returns a reason carrying an amount and a
direction — `scripts/close_batch.py`.

**Own benchmark** — 180 scenarios, 12 families × 3 pool densities, 30
unsolvable by construction:

| | |
|---|---|
| Auto-clear correct | 68.67% (member feed declared) / 62.0% (not) |
| Truth identified | 78.67% (declared) / 65.33% (not) |
| **False clears** | **0** |
| Correct abstentions | **100%** |

At the default 120-scenario count (`--scenarios` omitted) truth-identified
(not declared) is 64.0% rather than 65.33% — the two counts sample slightly
different scenario mixes at the same family proportions; both are real,
reproducible runs of the same script, not a discrepancy.

**50K stress** — 50,000 records, target contested by 49,999 of them: exact
55/55 by ID, precision/recall 1.0000, **2.0-2.3s to reconcile** (~1s to load
and parse; 3.0-3.5s total wall clock), ties out to 0c. A range, because it is
wall-clock on one machine and moves with load; the exact figure from
the last run is in [`benchmarks/latest.json`](benchmarks/latest.json), written
by `scripts/generate_benchmarks.py` with the commit it was measured at.

**Scenario counts differ by script, which is not a contradiction.**
`benchmark.py` defaults to 120 scenarios and `--scenarios` raises it;
`calibration.py` and the runs quoted here use 180; `realistic_benchmark.py`
defaults to 48 per profile. Any figure below names the count it came from.

**Calibration** — ECE **0.0538**, MCE 0.2, Brier 0.0439 over 180 scenarios
at seed 7 (`calibration.py`, in-sample). Bucketed:

| confidence | n | said | actual | verdict |
|---|---:|---:|---:|---|
| [0.00, 0.50) | 72 | 0.150 | 0.139 | well calibrated |
| [0.70, 0.85) | 5 | 0.800 | 1.000 | underconfident |
| [0.85, 0.93) | 41 | 0.876 | 1.000 | underconfident |
| [0.93, 1.01) | 62 | 0.955 | 1.000 | well calibrated |

**No bucket is overconfident in-sample**, and above the gate none is in
either corpus: wherever this engine claims enough confidence to clear, it is
at least that accurate. The [0.50, 0.70) bucket that used to sit here —
claiming 0.54, right 36.4% — is gone, because the constant feeding it was
re-measured; see below. One bucket IS overconfident out-of-sample, and it is
named in "Known limitations" rather than left out of this sentence.

The [0.85, 0.93) band is the one that matters, because it sits right at the
auto-clear gate. It used to claim 88.6% and be right 57.8% of the time. It now
claims 87.6% and is right 100% of the time — every one of the 103 predictions
at or above the gate was the exact true set.

**What changed, and why the old number was so bad.** The fuzzy recovery pass —
the fallback that runs when exact subset-sum finds nothing — reported
`fuzz_cfg.confidence_threshold` as its confidence. That constant is the
similarity score above which a fuzzy PAIR is worth acting on; it is not a
statement about the recovered SET, and assigning one to the other is a
category error that happened to land on 0.90, just above the gate. Measured
over 167 such bundles (`scripts/edge_case_suite_1000.py`): the exact set was
right **0%** of the time, about 4% of what the bundle contained belonged to
the settlement, and the median bundle was 63 transactions. A 63-record dragnet
was reporting the same confidence as a fully anchored three-record match.

Those bundles are now reported at 0.05, below the gate by construction and
asserted to stay there, with what they are actually good for — the true
members are somewhere inside 39.5% of the time — stated in the reasoning where
a reviewer can use it. **The last overconfident band, and how it was closed.** [0.50, 0.70) held
the ambiguous-exact-match constant: a set whose arithmetic admits more than
one answer. It has now been re-measured twice and come back lower both
times — the 0.65 band was right 54.5%, the 0.54 band that replaced it was
right 36.4% (n=11 each, the same small sample). The error was in the
dangerous direction both times, so it is set at the observed 0.36. It sits
below the auto-clear gate either way, so the change costs no coverage; it
only stops the number misleading whoever reads it.

The confidence number is now worth reading as a probability at and above the
gate. Below it, it remains a ranking signal — and clearing still requires this
figure **and** the three other independent guards to agree (see
"Governance"), which is what has kept the false-clear rate at 0 throughout.

### Calibration measured out-of-sample — and the gate does not fully hold

ECE 0.0538 is measured on 180 scenarios we wrote, using the buckets the 0.85
gate was picked from. Real, and in-sample, and those are not the same claim.
`scripts/calibration_out_of_sample.py` measures it on a corpus the gate was
never tuned against:

| corpus | predictions | ECE | at/above 0.85 | wrong above gate | of those, auto-cleared |
|---|---:|---:|---:|---:|---:|
| own benchmark *(in-sample)* | 180 | 0.0538 | 103 | **0** | 0 |
| ReconRiver *(out-of-sample)* | 74 | 0.0901 | 42 | **0** | 0 |

The safety-relevant column is "wrong above gate", and it is now zero in both:
every prediction at or above 0.85 was the exact true set, on our own corpus
and on a third-party one the gate was never tuned against. Previously 4 of 46
ReconRiver predictions above the gate were wrong.

**One number moved the wrong way and is reported rather than dropped.**
Out-of-sample ECE rose, 0.0766 to 0.1037, while in-sample ECE fell from
0.1567 to 0.0544 (since FAILURE_LOG 31: 0.0901 out-of-sample, 0.0538 in).
(Out-of-sample MCE — the worst single bucket, which is the
figure that actually bounds how wrong any one claim can be — improved from
0.46 to 0.13.) The two are measuring different things about the same change: the
fuzzy bundles that used to sit at 0.90 now sit at 0.05, which removes a large
block of confident-and-wrong predictions (in-sample ECE falls, and the
above-gate errors go to zero) but concentrates the remaining error in the
low band, where the engine now says 0.136 on a ReconRiver set it gets right
about 3% of the time. That is OVERconfidence (an earlier version of this
paragraph called it underconfidence), harmless only because the band never
clears — every proposal in it goes to a person — but it is still a gap, and
averaging it into a single ECE makes the out-of-sample headline worse while
the thing that can cost money got strictly better.

Two caveats kept deliberately: two corpora is validation, not proof, and both
carry usable references — what the gate does where references are absent
entirely is not measured by either.

### Why the numbers used to move

Three ECE figures once appeared across these documents — 0.067, 0.073, 0.0763
— and each was a real number from a real run. They disagreed because CP-SAT's
parallel search is not deterministic: with several workers, whichever finds an
optimal solution first wins, and on a scenario where more than one subset
genuinely satisfies the sum the answer differs run to run. Three of the 180
scenarios are like that. Diffing three runs showed exactly those three
flipping and the other 177 identical.

The engine was not wrong on them. It reported 0.19–0.54 confidence, which is
what "the data does not determine this" is supposed to look like. What was
wrong was quoting a headline from a harness that could not produce it twice.

`benchmark.run_scenario` now pins the solver to one worker: three runs of 180
scenarios differ in zero. Production keeps the parallel search, where speed
matters and the non-determinism is harmless because those cases already report
low confidence and never auto-clear.

The stripped-condition number is the honest one to quote alongside the
headline: it says how much of the accuracy is carried by reference quality
rather than by the engine.

**A separate, larger drift was found while re-measuring for this pass**: the
headline ECE (0.0863), the own-benchmark truth-identified figure (82%), and
the "103 of 103 correct above the gate" claim were all stale — not from
nondeterminism this time, but simply never re-measured after the scenario
generator grew the `ref_missing` / `ref_truncated` / `ref_partial` /
`ref_collision` families. Running `calibration.py` against the last commit
before this pass of fixes reproduces the *current* 0.1567/30.87/0.1808
exactly, so nothing below changed the number — it only got measured honestly
for the first time. The "103" itself was not invented: it is the true count
of correct predictions at or above 0.85 (41 in the [0.85,0.93) band, 62 in
[0.93,1.01)) — the number that was wrong was the implied denominator. The
true one is 133, not 103, and 30 of those 133 were wrong.

---

## The module that was split

`engine/src/main.py` was ~2,060 lines: 20 route handlers plus roughly 30
module-level functions carrying real business logic — `compliance_review`,
`interchangeable_note`, `matched_rows`, `contested_payments`, `_rate_card`,
`_settlement_instant`, `_format_report`. None of it was unit-testable
without the API layer, and `test_endpoints.py` was doing work that belonged
to a unit test.

Everything else in this repo is decomposed one concern per module, so this
stood out more for the contrast, not less. It is now `engine/src/api/`:

| module | lines | holds |
|---|---:|---|
| `api/models.py` | 98 | request models, and the conversion to `SettlementBatch` |
| `api/presentation.py` | 536 | everything that turns a report into what a controller reads |
| `api/routes_decisions.py` | 336 | what a human decided, and the audit record of it |
| `api/routes_reports.py` | 204 | read-only regulator and auditor views |
| `main.py` | 1,035 | app construction, middleware, reconcile/upload/queue, operational endpoints |

`models` and `presentation` import nothing from FastAPI beyond pydantic, so
they can be exercised without a running app. The split was made against the
full suite — tests passing before and after, `pylint --errors-only`
clean — and route registration moved to `include_router` rather than
changing any path.

One test had to change with it, and the reason is worth stating.
`test_every_upload_path_goes_through_the_cap` read `main.py`'s source
looking for a bare `await ....read()` that bypasses the upload ceiling. That
test was true when every endpoint lived in one file and would have gone
silently vacuous the moment one moved — passing because it was looking at
the wrong file, not because the property held. It now scans all 32 modules
under `src/`. A test that guards against code someone writes later has to
look where they will write it.

---

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

- **The confidence score is well calibrated at and above the gate, and not
  below it out-of-sample.** Every prediction at or above 0.85 was the exact
  true set — 103 of 103 in-sample, 42 of 42 out-of-sample on ReconRiver. Above
  the gate it errs low (says 0.87, is right every time), which costs review
  time rather than money. Below the gate the out-of-sample low band is
  overconfident: it says 0.14 and is right 6.3% of the time (2 of 32). That band
  never clears anything, so the harm is a reviewer seeing a hopeful number on
  a proposal already routed to them — but it is the reason out-of-sample ECE
  (0.0901) is worse than in-sample (0.0538). The in-sample band that used to
  be overconfident — [0.50, 0.70), claiming 0.54 against 36.4% — was set to
  its measured 0.36. See "Calibration measured out-of-sample" above for the
  full breakdown.
- **Sanctions screening is a point-in-time list** and exact-match after
  normalisation. Real screening also needs transliteration variants, fuzzy
  name matching and date-of-birth disambiguation, none of which this does.
  See *Sanctions screening* below for how the list is configured and what
  the engine reports about it.
- **Linkage weights are hand-set**, reasoned from forgeability rather than
  learned.
- **Where references are missing or truncated the engine declines** rather than
  guessing. Correct, but declining does not clear settlements.
- **LLM features use an external API**, confined to
  `llm_provider.py`. Verified live: Agent 0b resolved `val_dt -> timestamp` on
  a schema where rule-based mapping found no timestamp at all, and correctly
  refused to map a fee column to `amount`. Agent 9 answered questions about the
  50K reconciliation with figures that tie to the recorded state exactly, and
  described the Rs 5 crore ceiling as internal policy rather than law.
- **External LLM APIs can be intermittently unavailable** (503) or grant
  zero quota (429, `limit: 0`). Both paths retry transient failures and
  then degrade to the deterministic result — observed repeatedly against a live
  key, not just asserted.
- **The Q&A cache is in-process.** The audit trail is not: it is durable
  SQLite in WAL mode, which `GET /health` reports, with a file-backed trail
  and then an in-memory list as fallbacks. Tamper-evidence — signing or an
  append-only log the process cannot rewrite — is still a deployment concern
  this does not solve.

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
python -m pytest tests/ -q                # 736 tests
```

Frontend: `npm run dev` (port 8080).


## Sanctions screening

`SANCTIONS_HIT` is the only rule in this engine that blocks funds on a
statutory basis, so the list behind it deserves more scrutiny than any other
configuration here.

**Getting a real list.** The UN Security Council Consolidated List is public,
free, machine-readable and needs no registration. It is also the list India
implements domestically through the UAPA Order, which is the jurisdiction this
engine targets.

```
cd engine
python scripts/fetch_sanctions_list.py
```

That writes `data/sanctions/un_consolidated.txt` — primary names plus
multi-token aliases, one per line, normalised — with a provenance header
recording the source URL, the list's own generation date, and when it was
retrieved. The engine picks that path up automatically; `SANCTIONS_LIST_PATH`
overrides it for a different list.

**Why the file is not committed.** A sanctions list is a snapshot of something
that changes continuously. A copy in the repository is stale the week after it
lands, and stale screening is the failure mode that matters: it passes a party
designated after the snapshot while looking exactly like screening that works.
So the list is fetched deliberately, stamped, and `data/sanctions/` is
gitignored.

**What the engine tells you about it.** `/compliance/rulebook` and
`/compliance/attestation/{batch_id}` both return a `sanctions_list` block:

An earlier pass could not reach `scsanctions.un.org` and screened against
the four-name demo set. The fetch now succeeds, and the block this checkout
actually returns is the real one — 3,422 identifiers, with the list's own
generation stamp, verbatim:

```json
{
  "source": ".../data/sanctions/un_consolidated.txt",
  "is_illustrative": false,
  "entry_count": 3422,
  "list_generated": "2026-09-11T23:00:03.157Z",
  "retrieved": "2026-09-12T10:26:20+00:00",
  "match_mode": "exact after normalisation; no fuzzy, transliteration or DOB matching"
}
```

With no list fetched, the same block instead reports
`"source": "illustrative-builtin"`, `"is_illustrative": true` and
`"entry_count": 4`, and the engine says so at every startup.

The suite is honest about which of the two it ran against. A fresh clone
now carries a real list — `engine/data/sanctions/un_consolidated.txt` is
tracked so a deployed engine, which is built from git, screens against the UN
Consolidated List instead of silently dropping to four demo names — so the
run is **736 passed** with or without a fetch. If neither list is present,
`test_compliance.py` skips its real-list assertion and names itself, rather
than passing quietly against the demo set. A fresh fetch into `data/sanctions/`
at the repo root takes priority over the tracked snapshot, and CI does one.

`is_illustrative` is the field that matters. A block produced by the four-name
demo list and a block produced by the UN Consolidated List are indistinguishable
in a report otherwise, and they mean entirely different things. With no list
configured the engine screens the demo set and says so at every startup; a list
older than 30 days is warned about by age.

**Normalisation, and why screening needs it.** Both the list and the
counterparty are folded to uppercase alphanumerics with punctuation collapsed
to spacing, so `Al-Qaida`, `al qaida` and `AL QAIDA` are one designation rather
than three strings. Without that fold, screening compares a raw `payer_id`
against a list of real names by exact equality and matches nothing — a clean
screen against a list it is structurally unable to hit, which is worse than no
screening because it looks like the real thing. The fold lives in two places by
necessity (the fetch script writes the list, the agent reads it); a test asserts
the two implementations agree, because if they drift the list silently stops
matching itself.

**What it still misses.** A misspelling, a transliteration the list does not
carry, or two people sharing a name. Those are stated to any reviewer in
`compliance_rulebook.SANCTIONS_HIT.threshold_applied` rather than left to be
discovered.
