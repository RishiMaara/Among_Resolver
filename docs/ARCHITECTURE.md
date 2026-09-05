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

*Also includes `razorpay_source.py` for live Razorpay webhook ingestion and automated pipeline triggering.*
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

**Compliance rules declare their basis.** `statutory` / `regulatory_guidance` /
`internal_policy`, enforced by tests. The ₹5 crore ceiling that fires on the
demo is **not law** — no Indian statute caps a transaction at that value, and
the real duty is to *report* to FIU-IND, not block. Presenting it as statutory
would misrepresent the law to whoever relies on the output.

---

## Measured results

**ReconRiver** — third-party dataset, ingested through Agent 0, ground truth by
transaction ID:

| condition | batches | exact | false clears | latency |
|---|---|---|---|---|
| anchored | 37 | **94.59%** | **0** | 0.49s |
| settlement id stripped | 37 | 21.62% | **0** | 1.12s |

**Batch close** — 738 records across 60 settlements, every one attempted,
hazards seeded at one settlement in sixteen:

| records | settlements | match rate | false clears | false alarms | throughput |
|---|---|---|---|---|---|
| 738 | 60 | **95.0%** | **0** | **0** | 252 rec/sec |

Every unresolved settlement returns a reason carrying an amount and a
direction — `scripts/close_batch.py`.

**Own benchmark** — 180 scenarios, 12 families × 3 pool densities, 30
unsolvable by construction:

| | |
|---|---|
| Auto-clear correct | 75.33% (member feed declared) / 62.0% (not) |
| **False clears** | **0** |
| Correct abstentions | **100%** |

**50K stress** — 50,000 records, target contested by 49,999 of them: exact
55/55 by ID, precision/recall 1.0000, **2.4-3.0s to reconcile** (~1s to load
and parse; 3.2-3.9s total wall clock), ties out to 0c. A range, because it is
wall-clock on one laptop and moves with machine load; the exact figure from
the last run is in [`benchmarks/latest.json`](benchmarks/latest.json), written
by `scripts/generate_benchmarks.py` with the commit it was measured at.

**Scenario counts differ by script, which is not a contradiction.**
`benchmark.py` defaults to 120 scenarios and `--scenarios` raises it;
`calibration.py` and the runs quoted here use 180; `realistic_benchmark.py`
defaults to 48 per profile. Any figure below names the count it came from.

**Calibration** — ECE **0.0863**, MCE 0.20, Brier 0.0513 over 180 scenarios
at seed 7. Above the 0.85 auto-clear gate the engine was right in **103 of 103**
observations. Below 0.70 it is overconfident, which the report names as the
dangerous direction and which is why the gate is where it is.

### Calibration holds out-of-sample

ECE 0.0863 is measured on 180 scenarios we wrote, using the buckets the 0.85
gate was picked from. Real, and in-sample, and those are not the same claim.
`scripts/calibration_out_of_sample.py` measures it on a corpus the gate was
never tuned against:

| corpus | predictions | ECE | at/above 0.85 | wrong above gate |
|---|---:|---:|---:|---:|
| own benchmark *(in-sample)* | 180 | 0.0863 | 103 | **0** |
| ReconRiver *(out-of-sample)* | 48 | 0.0906 | 27 | **0** |

ReconRiver reproduces the in-sample figure almost exactly, and every one of the
27 predictions at or above the gate was correct.

One caveat kept deliberately: two corpora is validation, not proof, and both
carry usable references.

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

---

## Known limitations

- **Calibration is fitted to our own benchmark.** A large improvement over
  numbers chosen by feel, but not validated out of sample.
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
python -m pytest tests/ -q                # 335 tests
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

```json
{
  "source": "...\data\sanctions\un_consolidated.txt",
  "is_illustrative": false,
  "entry_count": 1183,
  "list_generated": "2026-08-25T00:00:00",
  "retrieved": "2026-08-31T09:08:18+00:00",
  "match_mode": "exact after normalisation; no fuzzy, transliteration or DOB matching"
}
```

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
