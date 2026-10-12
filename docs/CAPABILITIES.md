# ⚡ AmongResolver — Feature & Capability Catalog

> **Comprehensive overview of engine capabilities, verified measurements, and technical specifications.**

---

## 1. Core Settlement Reconciliation Engine

### 1.1 Multi-Target & N:M (Many-to-Many) Solving
* **Joint Constraint Optimization (`POST /reconcile/joint`)**: Solves multiple settlement targets simultaneously using Google OR-Tools CP-SAT. When transactions are contested across multiple payout batches, the engine determines optimal membership by global constraints rather than whichever batch executed first.
* **Fallback Safety**: If the joint model cannot satisfy all targets simultaneously, it gracefully falls back to sequential 1:N evaluation (`subset_sum_nm.py`), ensuring N:M execution is never less accurate than individual batch runs.

### 1.2 Entity Linkage & Payout Cycle Learning
* **Multi-Signal Record Linkage (`linkage.py`)**: Ranks transactions across 4 independent evidence signals:
  1. Direct Settlement ID match (weight: 0.55)
  2. Reference token cluster match (weight: 0.25)
  3. Cluster token naming settlement (weight: 0.15)
  4. Cross-feed amount correspondence (weight: 0.10)
* **Description Token Mining**: Extracts numeric transaction identifiers ($\ge 4$ digits) from unstructured memo descriptions, recovering matches even when formal reference columns are truncated or blank.
* **Statistical Fellegi-Sunter Model (`linkage_em.py`)**: Implements Expectation-Maximization (EM) to estimate $m$- and $u$-probabilities, learning the processor's payout cycle ($T+1$ vs $T+2$) from previous settlements.
  * **Empirical Impact**: On the third-party ReconRiver benchmark with all reference IDs stripped, exact identification jumps from **<!--fig:rr_stripped_exact:pct1-->24.3%<!--/fig--> to <!--fig:rr_learned_exact:pct1-->56.8%<!--/fig-->** with **zero false clears**.

### 1.3 Adaptive Solvers & Refusal Gates
* **Exact CP-SAT Subset-Sum**: Proves exact mathematical balance to the paisa. Checks for rival subsets to guarantee uniqueness.
* **Greedy Approximation Fallback (`fuzzy_match.py`)**: If transaction pools exceed exact solving thresholds, the engine provides closest-match candidate bundles for human controller review rather than failing or timing out.
* **Zero False Clears (4 Safety Gates)**: Enforces strict refusal if candidates exceed 20 without evidence, if twin amounts create ambiguity, if gross-to-net deductions do not tie out to zero, or if structural confidence is below 0.85.

---

## 2. Ingestion, Data Feeds & Real-Time Processing

### 2.1 Live Razorpay Integration & Webhook Security
* **Direct Dashboard CSV Ingestion**: Merchants can upload standard Razorpay Settlement Recon CSV reports directly. No live API keys or credentials leave the customer environment.
* **Live Settlements API Ingestion (`razorpay_source.py`)**: Pulls `/v1/settlements` and `/v1/settlements/recon/combined` feeds for real-time monitoring.
* **Secure Webhook Receiver (`POST /webhooks/razorpay`)**:
  * Ingests signed `settlement.processed` events.
  * Verifies HMAC-SHA256 signatures over raw request payloads using constant-time comparison (`hmac.compare_digest`). Unsigned payloads return HTTP 503.
  * Suppresses duplicate webhook retries across distributed instances via Redis atomic `SET NX` with 48-hour TTL.

### 2.2 Multi-Format Bank Statement Ingestion
* **Standard Financial Formats (`statement_parsers.py`)**: Ingests SWIFT MT940, ISO 20022 CAMT.053, OFX, and digital bank PDFs.
* **Arithmetic Balance Verification**: Every statement line is verified against the golden rule:  
  $$\text{Opening Balance} + \text{Credits} - \text{Debits} = \text{Closing Balance}$$  
  Statements with broken running arithmetic are refused with the exact failing line identified.
* **Scanned Statements & Browser OCR**:
  * Scans and images are rendered via pdf.js and extracted locally in-browser with Tesseract.js (zero data leakage).
  * If browser OCR is insufficient, Gemini statement OCR parses noisy scans, subject to the same strict running-balance arithmetic proof (23 of 24 noisy scans verified).

### 2.3 Grounded Bank Narration Extraction
* **Hybrid Parser (`narration_reader.py`)**: Extracts UTR numbers, settlement IDs, and counterparty entities from truncated (40–70 character) bank memos.
* **Accuracy Benchmark**: Achieves **97.5% extraction accuracy** across unseen bank formats (compared to 82.7% for hardcoded regexes).
* **Strict Value Grounding**: Every extracted entity is verified to exist verbatim in the source narration text before being accepted.

---

## 3. Statutory Compliance, Tax & Fee Auditing

### 3.1 Indian Statutory Tax Compliance (`india_tax.py`, `tax_audit.py`)
* **Automated TDS Transition**: Automatically switches statutory tax withholding from **Section 194-O (0.1%)** to **Section 393(1) Sl. 8(v)** of the Income-tax Act effective April 1, 2026.
* **GST TCS Calculation**: Audits 0.5% Tax Collected at Source under Section 52 CGST on supplies net of sales returns.
* **Input Tax Credit (ITC) Check (`POST /tax/itc-check`)**: Reconciles monthly deducted GST against processor tax invoices, flagging discrepancies requiring credit/debit notes.

### 3.2 Gateway MDR & Fee Auditing (`fee_audit.py`)
* **Per-Method Fee Decomposition**: Audits deduction schedules across payment instruments (UPI, Credit/Debit Cards, Netbanking, Wallets).
* **GST on Fees**: Audits that GST (18%) is levied exclusively on the gateway fee rather than gross order principal.

### 3.3 Statutory Sanctions & AML Screening (`compliance_agent.py`)
* **UN Consolidated Sanctions List**: Screens against 3,422 official UN Security Council designated entities implemented in India via the UAPA Order.
* **Match Logic**: Exact alphanumeric normalized matches block transactions immediately; close spellings ($\ge 85\%$ similarity) raise compliance review alerts.

---

## 4. Cash Management, Exceptions & Accounting

### 4.1 Working-Day Calendar & Open Items Aging (`india_calendar.py`)
* **RBI Banking Calendar**: Calculates $T+2$ settlement due dates by automatically skipping Sundays, 2nd and 4th Saturdays, and state bank holidays.
* **Open Item Tracking (`GET /open-items`)**: Tracks uncleared payments, unsettled adjustments, and withheld payouts across multiple runs, aging them in statutory working days.

### 4.2 Automated Discrepancy & Recovery Claims (`recovery_claims.py`)
* **Automated Dispute Drafting**: Automatically generates actionable recovery claims when processor fees exceed contractual rate cards or when bank credits fall short of processor payouts.
* **Exportable Claim Bundles**: Provides claim amounts, copy-paste explanation drafts, and itemized CSV evidence packages.

### 4.3 Structured Exception Taxonomy (`exception_taxonomy.py`)
* **Clear Categorization**: Groups reconciliation exceptions into standardized categories (In-Transit, Missing in Books, Unidentified Credit, Gateway Omission, Duplicate, Amount Mismatch, Compliance Hold).
* **Ranked by Exposure**: Sorts open items by total rupees at stake, identifying root cause and immediate recommended action for controllers.

---

## 5. Governance, Auditability & Enterprise Scale

### 5.1 Dual-Control Governance & Separation of Duties
* **Approval Safeguards**: The team member who accepts or matches a settlement batch is cryptographically restricted from approving the resulting general ledger journal entry.
* **Immutable Proposed State**: Reconciliations generate balanced journal entries marked `status="proposed"`. The engine never posts directly to production ledgers without human controller authorization.

### 5.2 ERP Export & Tally XML Integration (`erp_sync.py`)
* **Standard Tally XML**: Generates native Tally ERP import vouchers (`ENVELOPE → VOUCHER`) with exact debit/credit balancing to the paisa.
* **Audit Logging**: Every exported voucher is immutably logged with the authorizer's identity and timestamp.

### 5.3 Cryptographic Tamper-Evident Audit Trail (`audit.py`)
* **SHA-256 Hash Chaining**: Every lifecycle event records the cryptographic hash of the preceding entry. Modifying, deleting, or reordering any entry invalidates the chain.
* **Receipt Verification (`GET /audit/{batch_id}/verify`)**: Returns verifiable audit receipts (`audit_head`) allowing auditors to detect any truncation or tampering.

### 5.4 Scale & Throughput Benchmarks
* **50,000-Record Stress Test**: Identifies and clears the true settlement set in **3.1 seconds** with 1.0 precision and recall (`run_50k_stress_test.py`).
* **200,000-Record Scale Proof**: Processes 200,000 records in a single batch with exact 55/55 transaction retrieval (`run_scale_proof.py`).
* **In-Process Load Performance**: Sustains 10 reconciliations per second with sub-524ms p95 latency and zero errors under concurrent load.
