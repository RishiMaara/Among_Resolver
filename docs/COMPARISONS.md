# AmongResolver: Comparisons

This document brings together every comparison made between AmongResolver and
other ways of reconciling payouts: arithmetic alone, traditional matching
rules, an enterprise-style matcher run on the same data, the figures that
commercial vendors publish, and the other submissions to this track.

Our own figures are measured and can be re-run from the repository; the
commands and result files are named in `docs/FINAL_TEST_REPORT.md`. Vendor
figures are their published claims about their own customers' data. The two
kinds of number are not the same thing, and the comparison says where they
cannot be set side by side.

---

## Summary

| | Where AmongResolver stands |
|---|---|
| Wrong clears | Zero on every test we run, including a blind adversarial test written after the engine. No vendor we reviewed publishes a wrong-match rate. |
| Payouts that carry references | Level with rule-based tools: every clearable payout with evidence was cleared. |
| Payouts that carry no reference | Ahead of arithmetic alone and of FIFO matching on safety; behind FIFO matching on volume cleared, because we refuse what we cannot prove. |
| Checking the processor itself | Ahead: we re-derive each Razorpay payout without Razorpay's own ids. We found no vendor that describes doing this. |
| Production scale, integrations, customer results | Behind: the vendors have years of production use, many connectors and published customer outcomes. We have none of these yet. |

---

## 1. Against arithmetic alone

A payout is a sum of payments, so the obvious method is to search for the set
of payments that adds up to it. On its own that method cannot identify a
payout: in a pool of 60 payments with 5 true members there are millions of
subsets, and many of them reach the same rupee value.

`engine/scripts/baseline_comparison.py` runs the same 120 scenarios twice,
with the engine's evidence stage (linkage) switched off and on.

| | Arithmetic alone | AmongResolver |
|---|---|---|
| True sets identified | 0% | 65% |
| Auto-cleared | 0% | 62% |
| Wrong clears | 0 | 0 |

Result file: `engine/docs/benchmarks/baseline_comparison.json`. Linkage decides
which payments plausibly belong to the payout; the arithmetic then proves the
set to the paisa.

---

## 2. Against traditional matching rules

The blind test (`engine/scripts/blind_test.py`, 237 settlements) scores two
traditional methods on exactly the same data as the engine.

- **One-to-one**: a single payment whose amount equals the credit.
- **Rule grouping**: payments whose reference or memo contains the settlement
  id, summed with a 5-paise tolerance; no record of payments already paid out,
  no exchange rates, no duplicate handling.

| Method | Correct clears | Wrong clears | Refused |
|---|---|---|---|
| One-to-one | 0 | 0 | 237 |
| Rule grouping | 99 | 12 | 126 |
| **AmongResolver** | **133** | **0** | **104** |

Where the methods part company (fresh seed; the original seed gives the same
counts):

| Case | Rule grouping | AmongResolver |
|---|---|---|
| Settlement id written with other separators or case | 0 of 12 cleared | 12 of 12 |
| A row exported twice | 0 of 12 (counted twice, does not tie) | 12 of 12 |
| Second settlement re-using a paid-out payment | **12 wrong clears** | 12 of 12 refused |
| Foreign currency with a declared rate | 0 of 4 | 4 of 4 |
| Deductions estimated from a rate card | 0 of 6 | 6 of 6 |

One-to-one matching clears nothing, because a payout is many payments. Rule
grouping works where the settlement id is written exactly, and fails, or clears
wrongly, everywhere else.

---

## 3. Against an enterprise-style matcher

`engine/scripts/enterprise_baseline.py` models how rule-based reconciliation
tools are typically configured, and gives that model every advantage they are
normally set up with: reference normalisation, a persistent record of matched
items, duplicate-id removal, exchange-rate tables, the same rate card and the
same tolerance. It runs in two modes.

- **Rules**: match on the normalised settlement reference only.
- **Rules with FIFO**: where no payment carries the reference, clear the oldest
  open payments that add up to the payout, which is the usual cash-application
  shortcut.

This is a model of the approach, not any vendor's code.
Results: `engine/docs/benchmarks/enterprise_comparison.json`.

| Method | Correct clears | Wrong clears | Sent to a person |
|---|---|---|---|
| Rules | 133 | 0 | 104 |
| Rules with FIFO | 145 | **12** | 80 |
| **AmongResolver** | **133** | **0** | **104** |

The two families where the methods differ:

| Case | Rules | Rules with FIFO | AmongResolver |
|---|---|---|---|
| No references, and only one set of payments adds up | refused | 12 cleared correctly | refused |
| No references, fixed-price catalogue (many sets add up) | refused | **12 wrong clears** | refused |

On data with references, a well-configured rules engine and AmongResolver reach
the same answers. The difference appears when references are missing. FIFO
clears more, and on a fixed-price catalogue clears the wrong payments every
time. AmongResolver refuses both families, and for the second it names the one
payment whose answer would settle the tie. We only found 1 of the 12
catalogue ambiguities to be a pure swap of identical amounts; the other 11 are
different combinations that reach the same total, so no assignment rule can
resolve them safely.

---

## 4. Against published vendor figures

Vendor figures come from their own product pages and case studies, on their
customers' data, which mostly carries references. They cannot be re-run. Ours
come from generated, third-party and public data, much of it adversarial by
design, and can be re-run from the repository.

| What is measured | Vendors (published) | AmongResolver (measured) |
|---|---|---|
| Volume and speed | BlackLine: millions of transactions per minute; 7 million a month for SiriusXM. Osfin: a retailer with more than 1 million transactions a month. | 2,000,000 records in one file in 165 s on one machine. 200,000 payments in one payout proved in 16.75 s. The hosted demo accepts about 60,000 rows per upload. |
| Auto-match rate | BlackLine up to 99.9%; Trintech 99% and above; Osfin 98% and above; HighRadius 90 to 95%. | 133 of 133 clearable payouts with evidence cleared on the blind test. 104 others refused by design. |
| Wrong-match rate | Not published by any vendor reviewed. | 0 across 474 blind settlements, 1,050 edge batches and the limits test. |
| Payouts with no reference | Not published. | Not cleared on arithmetic alone. 57% identified exactly on stripped third-party data with a learned payout cycle, 0 wrong. |
| Tolerance | Configured per customer. | 5 paise, fixed. 5 short clears with the shortfall shown; 6 short is refused. |
| Checking the processor's report | Matched against as the source of truth. | Each Razorpay payout re-derived without Razorpay's ids, checked against the bank by UTR, and every fee and tax audited. Live, a Rs 10 shortfall and a Rs 43.09 overcharge were found. |
| Indian tax and calendar | Osfin and Bluecopa are India-focused and support Razorpay. | Section 194-O to Section 393(1) by date, GST on fees, TCS under Section 52 and the Indian bank calendar, built in. Not unique among Indian vendors. |
| Customer outcomes | Trintech: 50 to 70% less time on reconciliation. Bluecopa: effort cut by more than 60% in the first close. Osfin: eight people run more than 1 million transactions a month. | None. No merchant uses it in production. |
| Integrations | Osfin: more than 170 connectors. BlackLine and Trintech: the major ERPs. | File upload and the Razorpay Settlement Recon report. No live connectors. |

---

## 5. What is new, and what is not

Reconciliation is not a new problem; every tool above matches payouts to
books. Three capabilities are, as far as the vendors' public material shows,
new.

1. **Proving that a match is the only possible one.** A rules engine reports a
   match when a rule passes; it does not check whether a different set of
   payments also fits. AmongResolver searches for any rival set before it
   clears, and refuses if one exists. On the blind test, FIFO matching cleared
   the wrong payments in 12 of 12 catalogue cases; AmongResolver cleared none
   wrongly.
2. **Checking the payment processor.** Tools treat the processor's settlement
   report as the truth to match against. AmongResolver re-derives each payout
   with the processor's own settlement ids removed, confirms the bank credit by
   UTR and amount, and audits every fee and tax under the law on its date.
3. **Turning an unresolved payout into one question.** Where two sets tie,
   the investigator names the payment whose presence decides it, so a reviewer
   answers one yes-or-no question instead of investigating from the start. In
   the investigator benchmark every rules escalation carries such a question,
   and a tie takes about 2.5 answers to settle.

The following are done well, but are not unique: tolerances and many-to-one
grouping on a shared key, audit trails, exception workflows, records of matched
items, fee accounting and sanctions screening. The following are improvements
rather than new problems: Indian tax by date, the working-day calendar, reading
scanned statements in the browser, model proposals checked by code, calibrated
confidence, and needing no matching rules to be configured.

---

## 6. Against other submissions to this track

On 22 September 2026 we reviewed four other public submissions to Track 04.
None showed a Razorpay integration tested against a live account or real data,
and the largest test suite any of them listed had 83 tests. AmongResolver has
806 backend and 147 frontend tests, a committed blind test and a committed
limits test. This review has not been repeated since that date.

---

## 7. Where others lead

- **Production use.** The vendors run at scale for real customers; AmongResolver
  has no merchant in production.
- **Breadth.** ERP connectors, multi-entity close, account reconciliations and
  close checklists are outside this project's scope.
- **Assurance.** Vendors hold SOC certifications and offer support.
- **Volume cleared without references.** FIFO matching clears more payouts that
  carry no reference; AmongResolver sends them to a person unless a learned
  payout cycle or other evidence identifies them.

---

## 8. Positioning

AmongResolver is not a replacement for a financial close platform. It is a
settlement layer for Indian payment gateways: it establishes which payments
make up each payout, proves the answer to the paisa, verifies the processor's
own report, and, where it cannot prove an answer, declines and states the one
question that would settle it. The claim it can defend with evidence is
narrow and specific: on every test it has been given, including tests written
to break it, it has cleared no wrong set.

---

## Sources

- BlackLine, Transaction Matching: https://www.blackline.com/products/financial-close/transaction-matching/
- BlackLine, Transaction Matching solutions: https://www.blackline.com/solutions/financial-close-management/transaction-matching/
- BlackLine, pass rules and evaluation criteria (Clearsulting): https://www.clearsulting.com/insights/blog/blackline-matching-pass-rules/
- Trintech, AI transaction matching: https://www.trintech.com/platform/ai-transaction-matching/
- Trintech, Cadency Match: https://www.trintech.com/cadency/match/
- Trintech, Adra Matcher: https://www.trintech.com/adra/matcher/
- HighRadius, cash application: https://www.highradius.com/product/cash-application-automation/
- Osfin, e-commerce case study: https://www.osfin.ai/case-study/e-commerce-payment-reconciliation-leading-retailer-achieves-99-automation-with-osfin
- Osfin: https://www.osfin.ai/
- Bluecopa, ERP and payment gateway reconciliation: https://www.bluecopa.com/blog/erp-payment-gateway-reconciliation
- Bluecopa, payment reconciliation software: https://www.bluecopa.com/blog/payment-reconciliation-softwares

Vendor figures were read on 24 September 2026 and may have changed since.
