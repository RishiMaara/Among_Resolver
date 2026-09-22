# What each part does, and what measured it

The README's first screen says what the engine does. This is the same ground
part by part — the module, the check, and the measurement behind it. Moved
here from the README unchanged, except where a figure has since changed.

## Core capabilities

1. **N:M (Many-to-Many) Reconciliation:** `POST /reconcile/joint` solves several settlement targets in one CP-SAT model, so a transaction contested between two batches is resolved by what the *other* batch needs, not by whichever batch happened to run first. It reuses the same evidence-based safety the 1:N path has — linkage narrows each batch's own candidates, an anchored refund is still forced, an unevidenced arithmetic match is still withheld — and falls back to running the proven 1:N path per batch if the joint model can't satisfy every target at once, so N:M is never worse than calling the 1:N path on each batch separately. See `engine/src/subset_sum_nm.py` and `orchestrator.reconcile_many`.
2. **Anchorless Math Fallbacks:** If the pool is too massive for exact subset-sum, the engine gracefully degrades to a custom greedy approximation solver (`approximate_subset_sum_greedy`) rather than timing out.
3. **ERP Journal Sync (no real ERP has received one):** On a genuine clear, the engine builds a balanced double-entry journal and POSTs it to `ERP_JOURNAL_URL`. Unset by default, and unset means **nothing is posted and the journal says `no_target_configured`** — it does not quietly succeed. This module previously caught an unreachable endpoint and returned success with the journal marked `posted_mock`, so a connection refused left the books showing a journal as posted that no ERP ever received; a crash gets investigated, a false success gets reconciled against next month. Every outcome is now distinct (`posted`, `posted_to_mock`, `rejected_by_erp`, `unreachable`, `no_target_configured`, `skipped_unbalanced`) and only the first two return true. The payload is the shape NetSuite/Tally/QuickBooks accept, with exact integer paise carried alongside each decimal — but "would be accepted" is a design claim, not a measurement. Ingestion is **settlements pushed, transactions pulled**: `POST /webhooks/razorpay` receives signed `settlement.processed` events (HMAC-SHA256 over the raw body, constant-time compare, replay-suppressed across every instance through the shared Redis store, 503 rather than accepting an unsigned delivery), while the payments a settlement decomposes into still arrive by CSV upload or the `razorpay_source.py` pull. So a verified delivery queues a settlement for reconciliation rather than reconciling it — the transaction feed is what reconciliation needs and the webhook does not carry it.
4. **Learned Linkage Weights:** A Fellegi-Sunter model (`linkage_em.py`) with m-probabilities learned by EM from anchored records, or from the processor's payout cycle as read off verified clears — beside the reasoned 0.55/0.25/0.15/0.10 weights, which remain unfitted. An earlier version of this line claimed "machine-learned" weights that were in fact word counts (FAILURE_LOG 21).
5. **Deterministic LLM Fallbacks:** When external LLM APIs fail (latency or 503s), the engine seamlessly falls back to local deterministic string similarity (`difflib`) to keep the pipeline moving.
6. **AML Screening:** Compliance rulebook enforcement against the UN Consolidated Sanctions List — exact match after normalisation (no fuzzy, transliteration or DOB matching; `docs/ARCHITECTURE.md` states this alongside the caveat it implies). Without a fetched list it screens four illustrative names and says so loudly (see "Running it").

## Part by part

- **Five uses of a model (Gemini), each opt-in and metered** — column
  headers when rules cannot find a required one, bank narrations, scanned
  statements the browser's OCR could not prove, a next step for a withheld
  settlement, and questions about a result. Off unless `GEMINI_API_KEY` is
  set; every call from a web request is metered per visitor and per day
  (`model_budget.py`). Fuzzy matching *can* use a small embedding model
  (`all-MiniLM-L6-v2`) when `sentence-transformers` is installed; the shipped
  build does not install it, so in practice that path is lexical. All of them
  propose; none decides, and no model sits on the money path.
- **The AI's answers are checked, not just instructed.** Every figure,
  transaction id and date in a Q&A answer is traced back to the settlement's
  recorded results before it is shown (`grounding_check.py`). One that does
  not trace means the answer is withheld, logged, and replaced with the
  engine's own summary. It matches by value, not meaning — a real number on
  the wrong noun passes — and the tests pin that edge.
- **Exceptions are ranked by the money waiting on them.** Each carries its
  rupees at stake, the list is sorted largest first, and the results screen
  states the total value waiting on review and its share of the settlement —
  a payment named by two exceptions is counted once in that total.
- **Fees and tax are audited per payment method** (`fee_audit.py`), and the
  findings come back in every response under `fee_audit`: UPI, cards,
  netbanking and wallets against a contract rate card, GST at 18% on the fee
  rather than the principal, e-commerce TDS and GST TCS. Each payment is
  checked under the law **on its own date** (`india_tax.py`): TDS cites
  Section 194-O until 31 March 2026 and Section 393(1) Table Sl. 8(v) of the
  Income-tax Act 2025 from 1 April, at 0.1% (1% before October 2024); TCS
  under Section 52 CGST is 0.5% from 10 July 2024, on supplies net of
  returns, and is checked only when the data reports it. A batch straddling
  1 April cites both Acts. Razorpay's `fee` column includes its GST and is
  read that way. `POST /tax/itc-check` compares a month's deducted GST with
  the gateway's invoice: what can be claimed as input tax credit, and what
  needs a debit or credit note first. Whether any of this applies depends on
  the merchant's arrangement, so every check is configurable; it is
  arithmetic, not tax advice.
- **Open items, carried across runs** (`GET /open-items`). Every
  reconciliation adds the payments it saw that are not yet paid out, the
  refunds not yet deducted and the payouts it withheld, and closes whatever a
  cleared settlement took. What is left is aged in **working days** against a
  T+2 due date — Sundays, second and fourth Saturdays and the state's bank
  holidays skipped (`india_calendar.py`, Maharashtra by default, correctable
  by environment variable without a release). A card payment captured on
  Friday 11 September 2026 is due Wednesday the 16th, not Sunday the 13th;
  `GET /calendar/due` shows every skipped day and why.
- **Separation of duties on posting approvals.** Whoever accepted a match —
  by confirming the batch or accepting the oldest-first convention — cannot
  approve the posting that rests on it; the attempt is refused and recorded.
  The rule reads the audit trail, so it holds across instances. Identity is
  the name a reviewer types, so it stops the accident and the habit, not a
  determined person; with real user identity it keys on a user id instead.
- **Chargebacks add a row; they never edit a closed settlement.** Filing one
  (`POST /chargebacks`) emits a negative reversal that waits until a later
  payout absorbs it — opt-in per reconciliation, and only reversals filed
  inside that settlement's window are taken, so one the payout could not
  absorb is never dropped from the queue.
- **What linkage buys, measured** (`scripts/baseline_comparison.py`). The same
  engine with linkage disabled identifies the true set in 0% of 120 benchmark
  scenarios; with it, 65%, with zero wrong approvals in both arms. This is
  deterministic entity resolution, not AI — the script says so, because it
  was first written claiming otherwise.
- **Learned linkage for payouts with no reference** (`linkage_em.py`,
  `settlement_cycle.py`). A Fellegi-Sunter model — the method behind Splink —
  with m-probabilities learned by EM from anchored records, or from the
  processor's settlement cycle as read off earlier verified clears (T+1, T+2:
  the engine learns which). On ReconRiver with every settlement id stripped,
  exact sets identified rise from **24.3% to 56.8%**, zero false clears, with
  the cycle learned only from other scenarios. They arrive as proposals, not
  auto-clears: seven measured cases do not justify releasing money. Every
  response carries what the model learned (`summary.learned_linkage`).
- **Razorpay as the primary feed** (`razorpay_recon.py`,
  `POST /razorpay/reconcile` live with keys, `/razorpay/reconcile/upload`
  with saved API responses). The Settlement Recon API already says which
  payments, refunds and adjustments each payout contains, so re-deriving that
  by subset-sum would be solving a solved problem worse. The engine checks
  what the list does not prove: the lines sum to the payout **to the paisa**;
  a **blind re-solve** with settlement ids stripped and capture times only —
  the payout cycle learned from the *other* settlements, never the one being
  checked — reaches the same set; the bank credit carries the settlement's
  UTR and exact amount; every order is in the books; fees, GST, TDS and TCS
  are right for their dates. On the sample (`public/sample-data/razorpay/`,
  invented values in Razorpay's published shapes) three payouts verify, one
  has a 2.5% card fee and an unbooked order, one arrived ₹10 short at the
  bank, and the blind solve reaches Razorpay's exact set on all five. Not
  run against a live account: test mode creates no settlements. A merchant can
  instead upload the **Settlement Recon report they download from the
  Dashboard** (`settlement_report.csv` in the sample is the same payouts in
  that form) — no keys, and on a self-hosted engine nothing leaves their
  machine. Without a settlements list each payout's amount is its lines' sum,
  so the tie-out is marked as by construction and the bank credit (UTR and
  amount) is the independent check; a report read in the wrong unit is off by
  100x from its bank credit and is not verified.
- **Bank statements as banks send them** (`statement_parsers.py`): SWIFT
  MT940, ISO 20022 CAMT.053, OFX and text PDFs, straight into the bank slot of
  any reconciliation, or `POST /statements/parse` to check the read on its own.
  Every parse proves itself with the rule the bank guarantees — opening +
  credits − debits = closing, and the running balance line by line (the
  Golden Rule of the open-source `bankstatementparser`, whose approach this
  follows without its lxml/pandas<3 dependencies). On a PDF the running balance
  also decides which column an amount was in. A statement that does not
  balance is refused with the arithmetic shown, never partly used. A scanned
  PDF or a photo is read by Tesseract.js in the browser (`src/lib/scan-ocr.ts`,
  pdf.js to render the page) and the text goes through the same parser; if
  it does not balance, Gemini reads the file itself (`statement_ocr.py`) and
  is held to the same check. On 24 deliberately noisy scans the browser read
  11 exactly right and Gemini 12 of the other 13; no wrong reading was ever
  accepted (`scripts/ocr_eval.py`, `docs/benchmarks/ocr_eval.json`). Samples of
  one account in all four formats, and as a scan, are in
  `public/sample-data/statements/`.
- **Exceptions filed the way a finance team routes them**
  (`exception_taxonomy.py`): in transit, missing in books, unidentified
  receipt, booked but not at the gateway, duplicate, split or partial, amount
  mismatch, compliance hold — each exception carries its category, whose desk
  it goes to and the first thing to do, and the summary counts them with the
  money at stake. Fixed rules, so a misfiled break can be read and corrected.
- **The approved posting, as a Tally import file**
  (`POST /settlement/{batch_id}/export/tally`). Tally XML (ENVELOPE → VOUCHER),
  debits deemed positive with negative amounts as Tally expects, ledger names
  from the company's own chart of accounts. Produced only for a balanced
  journal a person approved — and separation of duties means the approver is
  not whoever accepted the match — and recorded in the audit trail with who
  exported it. The engine still posts nothing; Tally does, on import.
- **AI where it measured better, and a check in code wherever it is used**
  — [docs/AI_EVALUATION.md](docs/AI_EVALUATION.md) has every figure and the
  script behind it. Bank narrations (UTR, settlement ref, payer, rail): a
  grounded model reads 97.5% of fields right on bank formats the rules never
  saw, against 82.7% for the rules, and no value it returns is kept unless it
  appears in the narration. Withheld settlements: an investigator proposes one
  typed action — match, wait, request a document, write off rounding,
  escalate — and a verifier checks the arithmetic, the ledger, the calendar,
  the evidence and every figure in the reason before a reviewer sees it; on
  58 withheld benchmark cases it puts 22 right, verified proposals in front
  of a reviewer against 16 from fixed rules — 20 of them cases missing a
  member, 2 of the 38 solvable ones — with 3 wrong ones getting through
  where the evidence itself misleads. Questions about a result: 44 of 44 on a
  small set, facts from the record and refusals where the record cannot
  answer. Confidence is shown calibrated as well as raw (out-of-sample ECE
  0.090 → 0.026), and reviewer decisions flag drift. The first investigator
  measurement read the benchmark's labels and was thrown away; the write-up
  says how.
- **A tamper-evident audit trail.** Every entry carries the SHA-256 of the
  entry before it; edit a word, delete a line or reorder two and
  `GET /audit/{batch_id}/verify` names the first entry that no longer checks
  out. A chain cannot see its own tail cut off, so every reconciliation also
  returns its head hash (`audit_head`) as a receipt, and verifying against the
  receipt catches truncation. Tamper-evident, not tamper-proof: someone with
  write access can rebuild a chain, but not one that matches a receipt
  somebody else already holds.
- **Scale.** 200,000 records in one settlement: the exact 55-transaction
  true set, precision and recall 1.0, no exceptions
  (`scripts/run_scale_proof.py`). Throughput measured 13,700–26,900
  records/second across two runs on the same laptop — load on the machine
  moves it by 2x, so the range is the honest figure.
