# AmongResolver — the 5-minute pitch

One page, one flow: everything happens on **https://among-resolver.vercel.app/judge**.
Three runs, in the order the page lists them. Words to say are in plain text;
what to do is in **bold**.

---

## Before you start (5 minutes ahead)

1. Open the judge page once and click **Run** on the first card. That wakes the
   servers, so the live run is fast. Close nothing.
2. Check the line under the title says the model is live.
3. Have the screen recording of a clean run open in another tab, in case the
   network fails.

---

## [0:00 – 0:30] The problem

Hi, I'm Rishikesh. Razorpay tells a merchant which payments are in each payout —
the Settlement Recon report puts a settlement id on every line.

What it cannot tell them is whether their bank actually received that payout,
whether every payment is in their books, whether the fees and tax were right,
and what to do when the numbers don't tie. That is still a person with two
spreadsheets. AmongResolver is that person's work, done and proved.

**(Point at the grey box: "It cannot check on its own".)**

---

## [0:30 – 1:30] Run 1 — clear one, refuse one, and the agent

**(Click Run on "Reconcile a payout; investigate one it will not clear".)**

Three feeds — gateway, bank, ledger — in different formats. It found the 14
payments that make this payout, and they add up to ₹68,522.02 exactly. Zero
paise left over. And the audit trail is a hash chain: this receipt proves no
entry was changed or removed after the fact.

Now the same payout, but I don't tell it which feed to trust. Every gateway
payment has a ledger twin with the same amount, so two sets add up. It refuses
to clear. At this confidence a guess is right about half the time — measured.

This is the agent: it reads the withheld case with read-only tools, proposes
one of five actions — match, wait, request a document, write off rounding,
escalate — and code verifies the proposal before any person sees it.

**(Read the green lines.)** The model proposed a match, it passed every check,
and it is the same 14 payments — found without being told which feed to trust.

*If it escalates instead:* it refused to guess and said why. That is the
behaviour you want near money.

---

## [1:30 – 2:30] Run 2 — Razorpay's own data, checked five ways

**(Click Run on "Razorpay's own data, checked five ways".)**

Five payouts in Razorpay's own report format. Razorpay names the members; the
engine checks everything that naming does not prove: the paisa tie-out, a blind
re-solve with the settlement ids removed, the bank credit by UTR, the books,
and the fees and tax.

**(Point at the findings.)** A card payment charged ₹43.09 more than the agreed
rate. A payment Razorpay settled that is not in the books. And a bank credit
₹10 short of what Razorpay says it paid. Each finding names the payment.

---

## [2:30 – 3:15] Run 3 — AI where it is safe

**(Click Run on "A scanned statement, read in your browser".)**

An image-only PDF — no text in it. It is read right here in the browser, and
the reading is used only because every line's running balance follows from the
one before. A misread digit is refused, with the line named.

That is the rule for all five places a model is used here: the model proposes,
code checks, and no model ever decides which payments make up a settlement.

---

## [3:15 – 4:15] Why trust it

- It has never cleared a wrong set, on any dataset it has been measured on.
- On real government payments from two public checkbooks it found 100 of 100
  exactly.
- A model reads bank narrations right 97.5% of the time on formats the rules
  never saw, against 82.7% for rules — and anything it returns that is not in
  the narration is thrown away.
- Every failure is written down: 44 of them in the failure log, each with the
  fix and the re-measurement. 769 backend tests.

---

## [4:15 – 5:00] Why Razorpay, and the close

It is built for Indian money: T+2 in working days across second Saturdays and
festivals, TDS under Section 194-O until 31 March 2026 and Section 393(1) from
1 April, GST TCS at 0.5%. It reads the report a merchant already downloads —
no integration, no keys.

It sits on top of what Razorpay already gives, and closes the loop to the bank
and the books. Thank you.

---

## If a judge asks

- **"Isn't this just subset-sum?"** — Sums alone scored 0%: thousands of sets
  hit the same total. It links first — references, the same payment in two
  feeds, the payout cycle — and uses the sum to prove the set.
- **"What does the AI actually do?"** — Five things: column headers, bank
  narrations, scanned statements, the withheld-payout agent, and questions
  about a result. Each is checked in code before it is used.
- **"Real data?"** — Two public government checkbooks, 100 of 100. No live
  merchant yet: Razorpay test mode creates no settlements, so the Razorpay path
  runs on the report format a merchant downloads.
- **"What if the model is down?"** — It falls back to a second model, then to
  fixed rules, and the screen says which one answered.
