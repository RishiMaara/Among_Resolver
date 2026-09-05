# Sample data

Three feeds describing one settlement, with deliberately mismatched column
names so Agent 0's header mapping has real work to do.

**These do not go stale, provided you enter the settlement date below rather
than leaving it at today.** Every payment here is dated `2026-08-31`. The
lookback window is measured back from the settlement date, so anchoring both
to fixed values makes this fixture work on any day you run it.

The instructions used to say "leave as-is (today)", which worked only while
today stayed within five days of 2026-08-31. After that the first run anyone
did reported `0 within 5-day window` — the window filter working correctly on
old data, and indistinguishable from a broken product if you did not know to
look.

## Settings to enter in the form

| Field | Value |
|---|---|
| Batch ID | `SETTLE-001` |
| Net Amount (₹) | `66466.36` |
| Settlement Date (UTC) | `2026-09-02` — **set this, do not leave it at today** |
| Lookback (Days) | `5` |
| Members Live In | **Gateway** |
| Declared Deductions (₹) | `2055.66` |
| Number of Sources | 3 |

The Batch ID matters: the 14 member payments carry references reading
`SETTLE-001-ORDER000`…`ORDER013`, and that is what lets linkage anchor them.
Enter a different batch id and nothing anchors — which is itself worth seeing.

## Files

| File | What it is |
|---|---|
| `gateway_report.csv` | 45 card captures. The first 14 carry the settlement reference. |
| `bank_statement.csv` | The settlement credit itself. Note it is *not* a member — it names the settlement because it **is** one. |
| `erp_ledger.json` | Every gateway payment mirrored, under different column names (`journal_id`, `reference`, `credit`, `booked_at`, `narration`). |

Ground truth: **14 gateway payments, gross ₹68,522.02.**

## Two runs worth doing

**1. As configured above.** Verified result:

```
cleared     True      confidence 0.95
matched     14 of 91  — every one a true member, checked by id
residual    0 cents   ties_out True
exceptions  0         fee basis: declared
```

**2. Set "Members Live In" to _Unknown_.** Verified result:

```
cleared     False     ambiguous True     confidence below the 0.85 gate
matched     13–14 of 91  — and WHICH ones varies between machines
exceptions  78           fee basis: declared
```

The member count is deliberately given as a range. This run is the *ambiguous*
case: several different subsets satisfy the arithmetic equally well, so CP-SAT
returns whichever its parallel search reaches first, and that differs between
machines. Windows returns 13 here; Linux CI returns 14. Neither is more correct
than the other — that they disagree **is the finding**, and it is why the batch
is withheld rather than cleared.

Run 1 has no such range because it is anchored: exactly one subset carries the
settlement reference, so there is nothing for the solver to choose between.

Every gateway payment has an ERP twin at the same amount, so with no declared
feed nothing distinguishes them and the batch is **withheld rather than
guessed**. That abstention is the engine working, not failing — it is what
produces zero false clears. Watch the Agent 6 node turn amber rather than red.

Note what the second run does *not* do: it does not fail to find anything. It
finds 13 plausible members and a residual three paise off, and still declines,
because a near-miss with no evidence behind it is exactly the answer that must
not be booked. The interesting number is the 0.54 confidence — high enough to
be worth a reviewer's attention, far below the 0.85 gate that would clear it.

These figures are asserted by `engine/tests/test_sample_walkthrough.py`, which
runs both configurations against these files and fails if either drifts from
what is printed above. An earlier revision of this page claimed run 2 returned
16 matches at 0.19 confidence; it returned 13 at 0.54, and nothing caught it
until a reviewer typed the numbers in. The behaviour was right and the page
was wrong, which is the more embarrassing way round.
