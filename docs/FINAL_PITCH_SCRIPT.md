# AmongResolver — The 5-Minute Live Pitch Script

**Objective:** A high-impact, 5-minute presentation that proves technical depth, demonstrates flawless architecture, and leaves the judges with zero doubt about the system's reliability.

---

## [0:00 – 1:00] The Hook & The Problem

Hi, I'm Rishikesh. I built AmongResolver because I wanted to tackle the hardest problem in enterprise finance: Automated Reconciliation. 

The core idea is simple: **Reconciliation is not just an addition problem. It is an entity resolution problem.**

Here's the scenario: A payment gateway processes 50,000 transactions this week, deducts its dynamic fees, and drops one lump-sum net deposit into a merchant's bank account. Someone in finance has to figure out exactly which 55 of those 50,000 payments belong to that deposit.

The industry handles this in two ways, and both are flawed:
1. **Legacy Systems** break because they require clean 1:1 gross matching, which fails when fees are deducted dynamically.
2. **"GenAI" Wrappers** try to dump raw data into an LLM and ask it to guess. But LLMs hallucinate math, and worse, streaming raw PII to an external API violates strict enterprise data sovereignty rules.

I built AmongResolver to solve this. It separates identification from verification. It runs local, deterministic math using Operations Research (CP-SAT), and it guarantees exactly zero false clears. 

I'm going to prove it. Watch it run.

---

## [1:00 – 2:00] Live Demo 1: The Verification Standard

**(Click "Sample: clears")**

Three different data feeds (Gateway, Bank, ERP), completely different column layouts. 
It just found the exact 14 payments out of a massive pool. 
Look at the residual: **Zero paise.** Not "close enough." Exactly zero.

**(Click "Sample: withholds")**

Now watch what happens when the data is ambiguous. 
Every gateway payment here has an identical ERP twin. The engine found 13 plausible payments, but it missed the target by exactly 3 paise. 

It declined to auto-clear. This is an amber warning, not a red error. The engine is saying: *"I can find a match, but I cannot mathematically prove it."* It refuses to guess. 

This is the difference between an AI that makes suggestions and an AI Finance Controller you can trust with money movement.

---

## [2:00 – 3:00] The "Hard" Architecture

How does it actually work? The engine runs a 12-agent pipeline. 

This is an AI system that had to decide where AI belongs. Two agents use a model — schema mapping, understanding that `val_dt` means `timestamp_utc`, and answering plain-language questions about a result that's already been computed. Twelve agents don't, because deciding which transactions compose a settlement is a money question with millions of arithmetically valid wrong answers, and I measured what happens when you let a model guess at that: zero percent. It never touches the math, and I can prove that from the audit trail on every run. I'll say the honest version of the data-handling story too, because judges check: the two AI-touching agents *do* send real data externally when they're switched on — sample values for schema mapping, the recorded result for Q&A — fenced as untrusted content the model can only read, never act on. That's a deliberate, defensible design. It is not "nothing ever leaves the server," and I'd rather tell you that myself than have you find the gap.

Then, our **Fee Decomposition** agent natively reconstructs the true gross targets from net Razorpay deposits.

Then, the **Math Engine** takes over. 
* What happens when two bank deposits both have a claim on the same disputed payment? The engine solves every settlement in the group in **one joint CP-SAT model**, so the shared transaction is resolved by what both deposits need at once, not by whichever one happened to be checked first. That's real, running code today — `POST /reconcile/joint` — and it falls back to the proven single-settlement path if a joint group can't be solved together, so it's never a worse answer than checking them one at a time.
* What happens if the data is so noisy that an exact mathematical match doesn't exist? Instead of crashing, the engine seamlessly degrades to a **Greedy Approximation** solver, finding the mathematically closest subset and routing it cleanly for human review.

It never stops running, and it never hallucinates.

---

## [3:00 – 4:00] The Hard Numbers

I didn't just build a demo. I built a benchmark. 

First, I ran a **50,000-record stress test** spread across three massive sources. The CP-SAT engine used linkage to shrink the search space from 50,000 candidates to 55, identified the exact true set by transaction ID, and cleared the batch in about **3 seconds** with 100% precision and recall.

Then, I generated **120 randomized, mathematically chaotic scenarios**: missing legs, exact arithmetic collisions, duplicate amounts, degraded and missing references, and out-of-window noise.

The final metric that matters: **exactly 0 false clears, across every scenario.** And when references are clean, it identifies the true set 100% of the time. I'll also tell you the number that isn't as good, because I'd rather you hear it from me: where references degrade — missing, truncated, partial — truth-identification drops hard, down toward zero on those specific families, even though it still never clears a wrong set. That gap is the honest measurement of how much of this system's accuracy is the engine and how much is clean data, and it's exactly why every clear requires more than one independent guard to agree, not just a confidence score.

When the engine loses evidence, it doesn't guess harder. It declines harder. That is what institutional-grade safety looks like.

---

## [4:00 – 5:00] End-to-End Autonomy & The Close

So what is the end result? 

AmongResolver isn't a dashboard you stare at. When the CP-SAT engine clears a match — evidence and arithmetic both agreeing, not just a confidence number — it dynamically decomposes the fees and builds a **perfectly balanced, double-entry journal posting**. Today that posts to a mock ERP endpoint I built for the demo, not a live customer ERP — I want to be upfront about that rather than let the demo imply more than it is. And ingestion today is CSV upload, or a polling pull against the real Razorpay settlements API that I've verified end to end against a live test-mode key — I have not built a live webhook listener yet, and I'd rather tell you the honest shape of what's built than the more impressive-sounding version.

Every automated decision is backed by a deterministic, human-readable SQLite audit trail so your compliance team can review the math at any time.

I know exactly where this system still has edges. Production traffic validation is still ongoing. But I am showing you the real numbers, because knowing your limitations precisely is what real engineering looks like. 

AmongResolver isn't trying to automate trust. It's trying to make trust mathematically measurable. 

Identify first. Verify second. And refuse when the evidence isn't there.

Thank you. I'd love to answer your questions.
