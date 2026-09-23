# Where it goes next

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
What is deliberately **not** generalised to the joint case: the 1:N path's
tiering (progressively widening the candidate pool), which has no single
meaning once several targets share a pool. The substitutability guard is
shared: each target's matched set is checked against its own pool. If the single joint model can't satisfy every target in a group at
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
