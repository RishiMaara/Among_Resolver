# AI Finance Controller — Multi-Source Reconciliation Engine

Built for Razorpay Buildathon Track 04. Direction: multi-source
reconciliation (bank settlement <-> gateway transaction subset-sum).

## What this proves

Given a lump-sum bank settlement and a pool of individual gateway
transactions, reconstruct exactly which transactions the settlement
represents — the "which 412 out of 10,000" problem from the brief —
with an honest match rate, an exception list for anything unresolved,
and a full audit trail. Nothing writes back to a ledger automatically;
every auto-cleared match is proposed, logged, and still flagged for
human approval per the brief's governance requirement.

## Architecture — 6 agents

1. **Ingestion (`ingestion.py`)** — normalizes gateway/bank/ERP records
   into one schema. Deterministic. Amounts stored as integer cents,
   never float — this is the one rule that can't bend.
2. **Fee decomposition (`fee_decomposition.py`)** — reconstructs the gross target
   from the net settlement amount (reverses gateway fee + tax
   withholding deductions).
3. **Subset-sum matching (`subset_sum.py`)** — the core differentiator.
   **Uses OR-tools CP-SAT, not naive DP.** See "Key finding" below —
   this wasn't the original design and the reasoning matters.
4. **Fuzzy/semantic fallback (`fuzzy_match.py`)** — catches split
   payments, mistyped ref IDs, semantically-similar memos, for
   whatever exact subset-sum can't clear. Confidence-gated; never
   force-clears below threshold.
5. **Exception diagnosis (`exception_diagnosis.py`)** — classifies
   unresolved records (timing lag / duplicate / partial payment /
   missing entry) instead of dumping them in an undifferentiated queue.
6. **Orchestrator (`orchestrator.py`) + audit (`audit.py`)** — runs the
   pipeline in order, logs every decision, computes the honest match
   rate and exception report.

## Key finding: naive DP is the wrong algorithm at real scale

The first version of the matching engine used array/dict-based dynamic
programming — textbook subset-sum. It worked instantly on toy examples
but **took 108 seconds on a single Rs 60,000 / 62-transaction batch**,
because naive DP is `O(n × target_value)` — pseudo-polynomial in the
rupee amount, not the item count. Cents-level precision at real
settlement amounts (millions of cents) makes that term huge regardless
of how few candidates there are.

Switching the core solve to **OR-tools CP-SAT** (constraint
programming, branch-and-bound + pruning instead of brute enumeration)
fixed it:

| Scenario | Naive DP | CP-SAT |
|---|---|---|
| 62 candidates, Rs 60,806 target | 108s | 0.1s |
| 10,000 candidates, 412-txn true subset (the brief's own scale) | not tested (would be far worse) | 2.2s standalone solve, ~22s through the full pipeline with ambiguity probing |

If you're building this yourself: don't default to DP for subset-sum
at real financial scale. It looks correct in small tests and silently
becomes unusable the moment real rupee amounts show up.

## Key finding: exact matches can still be wrong — ambiguity is real, not theoretical

At 10,000 candidates, the engine found a valid 415-transaction subset
summing to the target — but it was **not** the true 412-transaction
subset that actually made up the settlement. Multiple genuinely
different combinations can sum to the same value. This is not a bug;
it's mathematically expected once the candidate pool is large enough,
and it's exactly the "zero tolerance for hallucination" trap the
brief warns about — an LLM-style "close enough" match here would be
a silent accounting error.

The fix is **not** exhaustive uniqueness proof — exact subset-sum
counting is #P-complete and intractable at real scale regardless of
solver. Instead: a bounded probe (`ambiguity_probe_limit` in
`SubsetSumConfig`) asks the solver to find one alternate valid subset
using a forbidding constraint. If it finds one, the match is flagged
`ambiguous=True`, confidence drops, and — critically — **it does not
auto-clear**, forcing human review. This is a best-effort heuristic
with a time budget, documented as such in the result, never silently
upgraded to "provably unique."

Practical levers to reduce real ambiguity (not just detect it):
- Tighten `filter_candidates_by_settlement_window` — smaller pool,
  fewer coincidental matches
- Use Agent 4's ref_id/memo signal to break ties between equally-valid
  arithmetic matches (not yet wired into the orchestrator — natural
  next step)
- Tighten `tolerance_cents` where the fee-reconstruction accuracy
  allows it

## Run it

```bash
pip install -r requirements.txt --break-system-packages

# generate synthetic demo data
python data/synthetic/generate.py --count 60 --seed 42

# run unit tests
pytest tests/ -v

# start the API
uvicorn main:app --reload --app-dir src
# POST /reconcile with gateway_txns + settlement_batch
```

## What's stubbed / next steps for the buildathon

- Agent 4 (fuzzy fallback) is implemented but not yet wired into the
  orchestrator's control flow for partial-batch recovery — currently
  only runs conceptually per-unmatched-record; orchestrator calls
  Agent 5 directly on anything Agent 3 doesn't clear
- Fee rate card (`fee_decomposition.py`) is a flat config; production would version
  it by source + effective date
- Redis audit log falls back to in-memory if Redis isn't running —
  fine for demo, note the fallback in a live run
- No tie-breaking logic yet for ambiguous matches (ref_id/memo scoring
  to prefer the more plausible of two valid subsets)
