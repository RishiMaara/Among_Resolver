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

AI is strictly quarantined to schema mapping—understanding that `val_dt` means `timestamp_utc`. It never touches the math. No financial amounts ever leave the local server. **Zero Data Leakage.**

Then, our **Fee Decomposition** agent natively reconstructs the true gross targets from net Razorpay deposits.

Then, the **Math Engine** takes over. 
* What happens when two bank deposits cover three gateway payouts? The engine natively handles complex **Many-to-Many (N:M)** constraints without breaking a sweat.
* What happens if the data is so noisy that an exact mathematical match doesn't exist? Instead of crashing, the engine seamlessly degrades to a **Greedy Approximation** solver, finding the mathematically closest subset and routing it cleanly for human review.

It never stops running, and it never hallucinates.

---

## [3:00 – 4:00] The Hard Numbers

I didn't just build a demo. I built a benchmark. 

First, I ran a **50,000-record stress test** spread across three massive sources. The CP-SAT engine used linkage to shrink the search space, identified the exact 55 true transactions, and cleared the batch in exactly **2.9 seconds** with 100% precision.

Then, I generated **100 randomized, mathematically chaotic datasets**: missing legs, exact arithmetic collisions, duplicate amounts, and out-of-window noise. 
Across all 100 scenarios, the engine identified the truth when it was solvable, and correctly abstained when it was impossible.

The final metric that matters: **Exactly 0 False Clears.** 

When the engine loses evidence, it doesn't guess harder. It declines harder. That is what institutional-grade safety looks like.

---

## [4:00 – 5:00] End-to-End Autonomy & The Close

So what is the end result? 

AmongResolver isn't a dashboard you stare at. It is an autonomous agent. When a Razorpay webhook fires, and the CP-SAT engine verifies the match with 100% confidence, the engine dynamically decomposes the fees and pushes a **perfectly balanced, double-entry journal directly to the ERP**. 

Every automated decision is backed by a deterministic, human-readable SQLite audit trail so your compliance team can review the math at any time.

I know exactly where this system still has edges. Production traffic validation is still ongoing. But I am showing you the real numbers, because knowing your limitations precisely is what real engineering looks like. 

AmongResolver isn't trying to automate trust. It's trying to make trust mathematically measurable. 

Identify first. Verify second. And refuse when the evidence isn't there.

Thank you. I'd love to answer your questions.
