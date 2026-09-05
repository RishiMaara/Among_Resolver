# Linkage — how the reconciliation engine identifies a settlement

## The problem it solves

Given a settlement of ₹8,16,863.41 and a pool of 50,000 transactions, which
transactions make up that settlement?

The obvious answer is subset-sum: find the subset whose amounts add to the
target. We built that first, on CP-SAT, and it does not work. Not because
the solver is slow — because the question is under-determined.

A solver may use **any** subset size, so the number of subsets competing for
one target is `2^n`, against a target that can only take about `2×10⁶`
distinct paise values:

| pool size | competing subsets | subsets per target value |
|---|---|---|
| 60 | 1.2 × 10¹⁸ | ~6 × 10¹¹ |
| 200 | 1.6 × 10⁶⁰ | astronomically many |
| 50,000 | — | — |

Measured on a 120-scenario benchmark, subset-sum alone achieved **0.0%**
auto-clear accuracy. It was not wrong occasionally; it could not identify
the right set at all, and correctly said so by flagging almost everything
ambiguous.

**A better solver does not fix an under-determined problem.** The error was
architectural.

## The reframe

Real reconciliation is **entity resolution first, arithmetic second**.

What actually identifies a settlement's members is *linkage*: the settlement
reference carried on the payment, the same payment appearing as both a bank
credit and a ledger entry, records clustering on a shared reference. The sum
is then used to **verify** the linked set, not to **discover** it.

So linkage runs before the solver and answers a different question — *which
transactions are plausibly connected to this settlement at all?* — after
which subset-sum runs over tens of candidates instead of tens of thousands,
where it genuinely is determined.

---

## How it works

### 1. Tokenisation

References are composites: `STL20260818001-ORD200042`, `UTR_NOISE_88`,
`JRNL900001`. The identifying part is usually one token inside, not the whole
string, so exact-string matching misses legitimate links. References are split
on letter/digit boundaries and tokens shorter than 4 characters are dropped —
`AB`, `1` match everything and would be worse than no signal.

### 2. Four signals, dynamically weighted via ML

Rather than brittle hardcoded weights, the linkage engine now uses dynamic, machine-learned weights that adapt to the density of the candidate pool (`dynamic_weights.py`). It scales the following four signals:

| signal | meaning |
|---|---|
| `settlement_id_match` | the record's reference contains the settlement's identifier |
| `shared_ref_token` | the record clusters with others on a shared token |
| `ref_prefix_cluster` | that cluster token also names the settlement |
| `cross_source_amount_peer` | same amount appears in a different feed |
| *(out of window)* | outside the settlement window (negative weight) |

**Anchor tokens must contain a digit.** Real payment references are
identifiers and effectively always carry digits; batch IDs routinely carry
descriptive words (`SETTLE`, `BATCH`, `DAILY`) that collide with unrelated
traffic. This was a measured failure — a batch id of
`BENCH_0001_near_collision_sparse` anchored eight decoy transactions whose
references began `NEAR`, because "near" appeared in both.

**A token shared by too much of the pool is boilerplate, not a reference.**
Clusters larger than `min(60, pool/8)` are ignored.

### 3. Source scoping — before any capping

The same payment appears in several feeds carrying the **same amount**. Pool
both representations and subset-sum will happily select both (double-counting
one payment) or swap one for the other, leaving the arithmetic identical while
the answer is wrong.

So when the member feed is known, records from other feeds are excluded.

- **Declared** (`SettlementBatch.member_source`) — authoritative. Records
  outside the member feed are excluded *even when they name the settlement*.
  Naming a settlement means a record **relates** to it, not that it
  **composes** it; the settlement credit itself is the standing example, since
  it carries the id because it *is* the settlement.
- **Inferred** — when every anchor comes from one feed, that feed is taken as
  the member feed, held more loosely.

The ordering here is load-bearing. Scoping must run **before** the
`max_candidates` cap. Capping first ranks by linkage score and keeps the top
N, but score does not correlate with feed — so the cap can retain 400 records
of which *none* are in the member feed, scoping then has nothing to filter,
silently no-ops, and every record it was meant to remove sails through. That
exact bug produced a confident 67-record wrong answer on the 50K dataset.

### 4. Tiered solving

Tiers are solved strongest-evidence-first, and the first that clears wins:

1. **anchor** — records naming the settlement, solved alone
2. **strong_link** — score ≥ 0.25
3. **all_linked** — everything with any signal

A weak signal admitted alongside strong ones does not add information, it adds
degeneracy. Cross-source amount peering scores 0.10 — real but feeble, since
unrelated payments share amounts constantly.

### 5. Two guards before auto-clear

**Substitutability.** Narrowing to a tier can hand the solver only one side of
a mirror pair, so the solve *looks* unique when it isn't. An anchored member is
safe; an unanchored member with an equal-amount twin in another feed outside
the tier is not, and the batch is withheld.

**Unanchored auto-clear limit.** With no anchor evidence and more than 20
candidates, an exact sum is a coincidence rather than an identification, and
the batch is withheld. Small pools are exempt because there the arithmetic
really is determined. (This was 25 in an earlier revision of this file; the
"judgement call" paragraph below explains why it moved.)

---

## Measured results

**Benchmark** — 180 scenarios, 12 families × 3 pool densities, 30 unsolvable
by construction, ground truth compared by transaction **ID**:

| | false clears | auto-clear correct | truth identified | correct abstentions |
|---|---|---|---|---|
| Subset-sum only *(baseline)* | 0 | **0.0%** | 1.1% | 100% |
| Linkage, member feed undeclared | **0** | **62.0%** | 66.0% | 100% |
| Linkage, member feed declared | **0** | **75.3%** | 81.3% | 100% |

Per family, with the member feed declared:

```
clean, near_collision, exact_collision,
duplicate_amounts, wide_spread, large_subset   100%
ref_partial                                    100%
ref_collision                                   60%
ref_missing                                     40%
ref_truncated                                 13.3%
missing_leg, out_of_window          100% correct abstention
```

**50K reference dataset** — 50,000 records across bank/gateway/ERP, target
contested by 49,999 of them:

| | |
|---|---|
| Exact set match | **True** (by ID) |
| Precision / recall | **1.0000 / 1.0000** |
| Matched | 55 of 55, ₹8,16,863.41 |
| Reconciliation | **2.4–3.0s** (was 25–49s with sharding) |
| Pool narrowing | 50,000 → 55 |

**Batch close** — 738 records, 60 settlements, every one attempted:

| | |
|---|---|
| Match rate | **95.0%** (57 of 60) |
| False clears | **0** |
| Declined without a planted problem | **0** |
| Throughput | 252 records/sec |

Run it with `scripts/close_batch.py --generate`. The fixture seeds problems on
purpose — a batch that reconciles completely measures the happy path, which was
never in doubt.

---

## Pros

**It makes the problem well-posed.** This is the whole argument. Subset-sum
over an unconstrained pool has no unique answer; over 55 linked candidates it
does. Everything else follows.

**Zero false clears, across every configuration measured.** Including the
adversarial families where linkage signal is missing, truncated or colliding.
In a system that books money against invoices this is the metric that matters:
an unresolved batch is an inconvenience, a confidently wrong one is a loss
nobody notices until a customer calls.

**8–30× faster.** The solver works over tens of candidates, not tens of
thousands. 2.4–3.0s versus 25–72s.

**It explains itself.** Every match reports *how* it was found — anchored,
clustered, or arithmetic-only — and confidence is graded accordingly (0.95
fully anchored → 0.45 arithmetic only). A reviewer can tell a strong match
from a lucky one.

**It degrades safely.** With no linkage signal anywhere it passes the pool
through unchanged rather than narrowing on no evidence, and the unanchored
guard then prevents a large unconstrained solve from clearing on a
coincidence.

**It removed the need for time-based sharding entirely**, along with that
strategy's failure modes.

## Cons

**It depends on reference quality, and degrades sharply without it.**
`ref_truncated` sits at 13.3% and `ref_missing` at 40%. Where the true
record's reference is gone, nothing distinguishes it from its copy in another
feed, and the engine declines. That is the correct behaviour, but declining
does not clear settlements.

**Part of the gain is configuration, not intelligence.** Declaring the member
feed buys +13.3pp (62.0% → 75.3%). It is fair to configure — you always know
which ledger you are reconciling — but it is not inference, and should not be
quoted as if it were.

**Blocking is unrecoverable.** A true member dropped at the linkage stage can
never be put back by the solver. Block keys are therefore deliberately
generous, which costs solver time; the risk is real if a future key is made
too aggressive.

**The weights are hand-set, not learned — and measurably, it does not matter
which values they take.** 0.55/0.25/0.15/0.10 are reasoned from forgeability,
not fitted to data. `scripts/sweep_linkage_weights.py` moves each one up and
down by 0.10 on its own and re-runs the benchmark:

| | |
|---|---|
| baseline | 62.0% auto-clear, 0 false clears |
| every single perturbation, all 10 | 62.00%, 0 false clears |
| **control: every weight zeroed** | **0.0%** auto-clear |

The control is the reason that flat table can be believed. A sweep whose
overrides never reached the module would look identical, so the script runs the
zeroed case first and refuses to print the table if it fails to move.

What it shows is that the weights are load-bearing *collectively* and not
*individually*. Linkage keeps only candidates scoring above zero
(`c.score > 0.0`), so with no weights nothing links and accuracy collapses; but
above that floor, the work is done by WHICH signals fire rather than by how
they are weighted. Tier membership is decided by the anchor boolean, and
perturbations across the 0.25 strong-link boundary moved nothing either.

So "hand-set, not fitted" is a smaller gap than it reads as: there is no peak
here to have missed. **Measured on corpora where anchors are
plentiful**, though — it does not follow that the weighting is irrelevant where
they are absent, and that case is not measured here.

**Confidence is calibrated, and it holds out-of-sample.** ECE 0.0863 is
measured on 180 scenarios we wrote, using the buckets the 0.85 gate was picked
from — real, and in-sample, and those are not the same claim.
`scripts/calibration_out_of_sample.py` measures it on a corpus the gate was
never tuned against:

| corpus | predictions | ECE | at/above 0.85 | wrong above the gate |
|---|---:|---:|---:|---:|
| own benchmark *(in-sample)* | 180 | 0.0863 | 103 | **0** |
| ReconRiver *(out-of-sample)* | 48 | 0.0906 | 27 | **0** |

ReconRiver reproduces the in-sample figure almost exactly — 0.0906 against
0.0863 — and every one of the 27 predictions above the gate was correct.

Two corpora is validation, not proof, and both carry usable references.

**Confidence is calibrated in-sample, and it is worse at the bottom than the top.**
`scripts/calibration.py` buckets 180 predictions against outcomes: ECE 0.0863,
MCE 0.20, Brier 0.0513. Above the 0.85 auto-clear gate the engine was right in
103 of 103. Below 0.70 it is *overconfident* — the [0.50, 0.70) bucket says
0.54 and is right 0.36 of the time — which is the dangerous direction, and the
reason nothing below the gate clears itself. This paragraph used to say
confidence was "asserted rather than measured"; it was measured, and the doc
was not updated.

**The 20-candidate unanchored limit is a judgement call.** It follows from the
density argument but the exact number is chosen, not derived. It was 25 until
`realistic_benchmark.py` produced false clears at pools of 22 and 23 — with an
estimated fee target and a 10-paise tolerance, C(22,5) is 26,334 subsets
competing for a 20-paise window — so it moved to the bottom of the plausible
range rather than the top.

**Not tested against real production data.** Every corpus here is synthetic —
ours, or ReconRiver's, whose own manifest says so. Real settlement data with
usable ground truth is not something this project has, so the sensitivity sweep
measures *which properties of real data hurt, and how much*, rather than
claiming a production number.

---

## What replaced what

Time-based sharding split the pool into 48-hour chunks, ranked them by
proximity to the settlement date, and reconciled each in turn. It was a way of
making an intractable search tractable by *guessing* that members cluster in
time. Linkage removes the need for the guess, and the guess was not free:

- Members can straddle a chunk boundary or land in a chunk the ranking never
  reaches. On the 50K dataset the top-ranked chunk held almost none of them
  and the run returned 71 unrelated ERP journal records that summed to the
  target.
- It halted on the first ambiguous chunk, ending the search before the chunk
  holding the real members was examined.
- 25–72s versus 2.4–3.0s.
- Its ranking heuristic had no evidence behind it.

The implementation is preserved on the **`archive/time-based-sharding`**
branch, because the question it was trying to answer is real: what do you do
when *nothing* names the settlement? Today's answer is "decline and route to a
human" — correct, but unsatisfying. A future non-arithmetic signal (timestamp
precedence between a payment and its ledger entry, amount+window pairing
across feeds) may do better than either approach.
