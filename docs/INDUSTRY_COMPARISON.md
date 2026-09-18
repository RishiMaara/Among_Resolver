# AmongResolver vs. The Industry

## The Enterprise Standard for an AI Finance Controller
Any true "AI Finance Controller" must deliver on three uncompromising standards:
1. **Mathematical Trust:** Financial controllers do not tolerate hallucinations. A model that guesses a financial match with "95% confidence" is useless if it causes a misstated ledger.
2. **Data Sovereignty:** Sending sensitive raw PII (Personally Identifiable Information) or transaction amounts to external APIs (OpenAI/Gemini) violates strict banking and enterprise compliance (SOC2/PCI-DSS).
3. **End-to-End Autonomy:** An AI controller shouldn't just "report" things for a human to type; it should read from the source (webhook) and write the journal entry directly to the ERP.

## What the Industry Has (The Status Quo)

### 1. Legacy Enterprise Systems (Trintech, BlackLine, HighRadius)
* **What they have:** Rigid, month-end batch processing tools requiring massive ETL (Extract, Transform, Load) setups. They demand clean, standardized inputs and rely heavily on pre-aggregated data.
* **The Flaw:** They break when settlements hit the bank *net of gateway fees*. A ₹10,000 order hits the bank as ₹9,700, and legacy software immediately raises an exception because the amounts don't match.

### 2. "GenAI" Wrappers & Copilots
* **What they have:** Tools that dump raw CSV data into a massive LLM context window and ask it to "find the matching transactions." 
* **The Flaw:** Large Language Models are notoriously terrible at exact arithmetic and combinatorial optimization. They hallucinate math, and worse, they transmit highly sensitive enterprise financial data to external, third-party servers.

### 3. Pure Machine Learning (Probabilistic Matching)
* **What they have:** Models trained on historical reconciliations that output a "Match Confidence Score" (e.g., 94% likely this is the match).
* **The Flaw:** These systems *identify* but cannot *verify*. In a dense pool of 50,000 transactions, probabilistic scoring will confidently present the wrong subset. In finance, this translates directly to a false clear and a corrupted ledger.

---

## What AmongResolver Has (The Differentiator)

AmongResolver is built on a radically different premise: **Reconciliation is entity resolution first, arithmetic second.** It does not guess. It proves.

### 1. Zero False Clears (The CP-SAT Core)
Instead of asking an LLM to do math, AmongResolver uses Operations Research (Google's CP-SAT solver) to demand absolute mathematical proof. 
* **The Industry:** Probabilistic guesses leading to costly false positives.
* **AmongResolver:** Evaluated across 120 randomized scenarios, a third-party corpus (ReconRiver), a 738-record batch-close run, and a 50K-record stress test, the engine recorded exactly **0 false clears** in every one.

### 2. The Money Path Is Local and Deterministic — and We Say Exactly What Isn't
* **The Industry:** Streams sensitive ledger data to third-party AI APIs to *decide* matches.
* **AmongResolver:** The core matching engine (linkage clustering, fee decomposition, subset-sum math) is 100% deterministic and local — no model ever decides which transactions compose a settlement. Two agents *do* call an external LLM when enabled: header mapping sends a few sample cell values per column to resolve an unfamiliar schema, and settlement Q&A sends the recorded result (matched IDs, amounts, cash position) as grounding for a plain-language answer. Both are off by default, both fence that data as untrusted content the model cannot act on, and neither can write a decision back into the pipeline — but real transaction data does leave the server when either is switched on, and a "zero leakage" claim would not survive reading the code that sends it.

### 3. Natively Built for Gateways (Razorpay/Stripe)
* **The Industry:** Requires exact 1:1 gross-to-gross matching.
* **AmongResolver:** Dynamically decomposes gateway fees (Agent 2), converting a net bank deposit back to its true gross target on the fly. Furthermore, it easily navigates complex **N:M (Many-to-Many)** constraints, where multiple gateway payouts cover a clustered set of bank deposits.

### 4. A Real Journal Posting, Honestly Scoped
* **The Industry:** Flags matches on a dashboard for an accountant to manually post at the end of the month.
* **AmongResolver:** On a genuine clear — arithmetic tying out *and* evidence, not a raw confidence score alone — the engine builds a balanced double-entry journal posting and syncs it (`erp_sync.py`). Today `push_to_erp` posts to a local mock endpoint built for this demo, not a live customer ERP, and ingestion is CSV upload or a polling pull against the real Razorpay settlements API (`razorpay_source.py`, `scripts/pull_razorpay.py`) — there is no live webhook listener in this repo. The journal-building and posting logic is real and runs end to end; the "live webhook, straight to production ERP" framing would not be.

### 5. Graceful Degradation (Anchorless Math Fallbacks)
* **The Industry:** Solvers crash or time out when the dataset is too massive and noisy.
* **AmongResolver:** If the exact CP-SAT subset-sum fails, the engine seamlessly degrades to a custom greedy approximation solver, finding the mathematically closest possible subset and routing it cleanly for human review. 

---

**The Verdict:** While the industry forces finance controllers to choose between rigid, slow legacy software or risky, hallucinating AI wrappers, AmongResolver provides the ultimate hybrid: the speed and adaptability of AI for data ingestion, governed strictly by the uncompromising mathematical safety of Operations Research.
