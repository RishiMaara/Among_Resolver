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

### 2. Four signals, hand-weighted — and a learned model beside them

The four signals below carry reasoned weights (0.55 / 0.25 / 0.15 / 0.10),
not fitted ones. This section once said they were "dynamic, machine-learned
weights"; that described an opt-in branch of `dynamic_weights.py` that counted
words in the audit log, and it was not true (FAILURE_LOG entry 21). The
learned model is described under **Learned linkage** below.

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
2. **names_settlement_number** — records carrying the settlement's own number,
   in the reference or the description, with the anchors
3. **learned** — the cohort the learned payout cycle points at, with the anchors
4. **strong_link** — score ≥ 0.25
5. **all_linked** — everything with any signal

A weak signal admitted alongside strong ones does not add information, it adds
degeneracy. Cross-source amount peering scores 0.10 — real but feeble, since
unrelated payments share amounts constantly.

### 5. Guards before auto-clear

**Substitutability.** Narrowing to a tier can hand the solver only one side of
a mirror pair, so the solve *looks* unique when it isn't. An anchored member is
safe, and so is a member carrying the settlement's number whose twin does not
carry it: the evidence then says which copy is the member. Any other member
with an equal-amount twin in another feed outside the tier withholds the batch.

**The whole named set.** When the matched set is exactly the records carrying
the settlement's number, all of them and nothing else, the set was fixed
before the solver ran, and the arithmetic checked one hypothesis instead of
searching 2ⁿ subsets; an unrelated set lands within tolerance of a
seven-digit target about once in 100,000. That set may clear at any pool size
(band `named_complete`, 0.88). One record carrying the number left out, or one
without it let in, and the band does not apply.

**Unanchored auto-clear limit.** With no anchor evidence and more than 20
candidates, an exact sum is a coincidence rather than an identification, and
the batch is withheld. (This was 25 in an earlier revision of this file; the
"judgement call" paragraph below explains why it moved.) Small pools are not,
however, exempt from every guard: the 0.85 confidence gate now applies
regardless of pool size, closing a residual case where a small pool with weak
(not zero) linkage signal could previously clear at a structural confidence
around 0.22 with nothing withholding it.

---

## Measured results

**Benchmark** — 120 scenarios (the default), 12 families × 3 pool densities,
20 unsolvable by construction, ground truth compared by transaction **ID**,
seed 7:

| | false clears | cleared and right | truth identified | correct abstentions |
|---|---|---|---|---|
| Subset-sum only *(baseline)* | 0 | **0.0%** | 0.0% | 100% |
| Linkage, member feed undeclared | **0** | **89%** | 95% | 100% |
| Linkage, member feed declared | **0** | **89%** | 95% | 100% |

On seeds 202 and 303, never looked at while tuning: 89% and 90% cleared, 93%
and 95% identified, 0 false clears. With every description erased: 62% and
65%, the figures before identifiers were read from descriptions.

Per family, true set identified:

```
clean, near_collision, exact_collision,
duplicate_amounts, wide_spread, large_subset   100%
ref_missing, ref_truncated, ref_partial        100%
ref_collision                                   50%
missing_leg, out_of_window          100% correct abstention
```

`ref_collision` is the ceiling, and it is the data's: unrelated records carry
the settlement's own identifier, and in half the scenarios two different sets
of them sum to the payout. Nothing but the generator's label words separates
them, and reading those would be reading the answer key (FAILURE_LOG 26).

**50K reference dataset** — 50,000 records across bank/gateway/ERP, target
contested by 49,999 of them:

| | |
|---|---|
| Exact set match | **True** (by ID) |
| Precision / recall | **1.0000 / 1.0000** |
| Matched | 55 of 55, ₹8,16,863.41 |
| Reconciliation | **3.1–3.2s** (was 25–49s with sharding) |
| Pool narrowing | 50,000 → 55 |

**Batch close** — 738 records, 60 settlements, every one attempted:

| | |
|---|---|
| Match rate | **95.0%** (57 of 60) |
| False clears | **0** |
| Declined without a planted problem | **0** |
| Throughput | 190–250 records/sec (3–4 s for all 60) |

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

**~8–23× faster.** The solver works over tens of candidates, not tens of
thousands. 3.1–3.2s versus 25–72s.

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

**It depends on evidence surviving somewhere.** With the reference column
gone, the engine now reads the settlement's number from descriptions, and
missing and truncated references went from 13-40% to 100%. Where neither the
reference nor the description carries it, nothing distinguishes a payment from
its copy in another feed, and the engine declines. The benchmark's generator
always writes the number into a member's description; how often a real bank
narration or ERP memo carries it is not measured here.

**Declaring the member feed is configuration, not intelligence.** It once
bought 6.7 points; since descriptions are read it buys nothing on the
benchmark (89% either way), but it remains the stronger claim where
descriptions are bare.

**Blocking is unrecoverable.** A true member dropped at the linkage stage can
never be put back by the solver. Block keys are therefore deliberately
generous, which costs solver time; the risk is real if a future key is made
too aggressive.

### Learned linkage: Fellegi-Sunter, and the settlement cycle

`linkage_em.py` is a Fellegi-Sunter model, the one Splink and the US Census
use: six comparisons per candidate (anchor, reference cluster, cluster naming
the batch, cross-feed amount peer, currency, and the lag in days from capture
to payout), an m- and u-probability per level, and a match weight in bits.
u comes from the pool. m comes from what identifies members — EM over the
anchored records when some name the settlement, or, where nothing does, the
**settlement cycle learned from earlier verified clears** (`settlement_cycle.py`).
Whole comparison patterns are taken highest weight first until they cover the
members the target implies, and that cohort is solved as its own tier. The
arithmetic still decides.

It declines to fit when nothing identifies members. A mixture over a single
informative comparison is not identifiable, and unsupervised EM on
ReconRiver's stripped pools collapsed to "no members" on every one.

Measured on ReconRiver, references stripped, the cycle learned from the other
three scenarios' anchored clears — nothing from the scenario being measured
(`scripts/learned_linkage_benchmark.py`, `docs/benchmarks/learned_linkage.json`):

| stripped condition, 37 settlements | exact set identified | auto-cleared | false clears |
|---|---:|---:|---:|
| before (hand weights only) | 9 (24.3%) | 8 | 0 |
| learned linkage, no history | 9 (24.3%) | 8 | 0 |
| learned linkage, cycle learned from other scenarios | **21 (56.8%)** | 8 | 0 |

### On real payments

`scripts/public_ledger_benchmark.py` takes two public government checkbooks —
Baton Rouge (public domain) and Fulton County, GA (CC BY 4.0) — where each
cheque or transfer pays several invoices and the government's own system
recorded which. One payment is the settlement, the day's whole payment run
(median 482 and 675 invoices) is the pool, and invoice ids are opaque.

| | payee known | amounts only |
|---|---:|---:|
| Baton Rouge, 50 payments | **50 exact**, 49 auto-cleared | 2 exact, 0 cleared |
| Fulton County, 50 payments | **50 exact**, 50 auto-cleared | 0 exact, 0 cleared |
| wrong clears | **0** | **0** |

Amounts only is subset-sum's case: about a fifth of amounts repeat within a
day, nothing ties an invoice to the payment, and the engine withholds rather
than pick one of the sets that add up. With the payee on the invoices it is
exact every time — after one fix the real data forced (FAILURE_LOG 36): the
first run found 25 and 36, because a payee code ending in a digit, glued to a
numeric invoice number, lost its boundary when the reference was
canonicalised. Linkage now also reads the reference as the source wrote it,
at its separators.

The first two rows read 8 (21.6%) until the tiebreak was confined to the
declared member feed (FAILURE_LOG 31), which made one more settlement's
proposal the exact set. Auto-clears and false clears did not move.

The anchored condition is unchanged (94.6% identified, 34 auto-cleared, 0
false clears), and so are this project's own benchmark and the 1,050-case edge
suite (0 false clears in both). Auto-clears do not rise, deliberately: the new
matches come from a band whose own proposals were right 7 of 7 times, and seven
cases do not justify releasing money (Wilson 95% lower bound 0.65). They
arrive as exact proposals for one-click confirmation. What the lag feature is
worth was found by looking at ReconRiver, so the other corpora are the held-out
check, and on them it changed nothing — they carry no settlement cycle.

**The weights are hand-set, not learned — and measurably, it does not matter
which values they take.** 0.55/0.25/0.15/0.10 are reasoned from forgeability,
not fitted to data. `scripts/sweep_linkage_weights.py` moves each one up and
down by 0.10 on its own and re-runs the benchmark:

| | |
|---|---|
| baseline (before the tuning of 30 September) | 62.0% auto-clear, 0 false clears |
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

**Confidence is trustworthy where it is used to clear.**

| corpus | predictions | ECE | Brier | at/above 0.85 | wrong above the gate |
|---|---:|---:|---:|---:|---:|
| own benchmark *(in-sample)* | 180 | 0.0783 | 0.0223 | 133 | **0** |
| ReconRiver *(out-of-sample)* | 74 | 0.0901 | 0.029 | 42 | **0** |

Every prediction at or above the gate was the exact true set, on our corpus and
on one the gate was never tuned against. Above it the number errs low, which
costs review time: the band for a complete named set says 0.88 and was right
80 of 80 times, kept low because all 80 came from one generator. Below the gate
it is a ranking: out-of-sample the lowest band says 0.14 and is right 6.3% (2 of
32), and never clears. It got here by fixing two numbers that told a reviewer
to trust something wrong, a fuzzy bundle reporting 0.90 and an ambiguous-match
constant reporting 0.54; both are in FAILURE_LOG. Even so, clearing needs this
number **and** the other guards to agree.

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
- 25–72s versus 3.1–3.2s.
- Its ranking heuristic had no evidence behind it.

The implementation is preserved on the **`archive/time-based-sharding`**
branch, because the question it was trying to answer is real: what do you do
when *nothing* names the settlement? Today's answer is "decline and route to a
human" — correct, but unsatisfying. A future non-arithmetic signal (timestamp
precedence between a payment and its ledger entry, amount+window pairing
across feeds) may do better than either approach.
