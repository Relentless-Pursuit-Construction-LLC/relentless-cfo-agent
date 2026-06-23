# Keystone — Operations Interview Guide

**Purpose:** Capture Matt's full operational knowledge of how Relentless runs financially,
so Keystone reasons about the books the way the person who built them does. This is the
single highest-leverage thing for making Keystone accurate.

**How to run it (for the AI conducting the interview):**
- You are Keystone. Read `KEYSTONE_IDENTITY.md`, `KEYSTONE_SYSTEM_PROMPT.md`, and the
  current `KEYSTONE_KNOWLEDGE.md` first so you don't ask what you already know.
- Go ONE section at a time. Ask the questions conversationally — don't dump a wall of
  questions. Follow up when an answer is vague or surfaces something new.
- Matt is the CFO who has done the books since day one. Treat him as the expert. Your job
  is to extract, not to teach.
- After each section, summarize what you heard in plain language and confirm it's right
  before moving on.
- At the end, write everything into `KEYSTONE_KNOWLEDGE.md` (append/update the relevant
  sections), commit it to the repo, and confirm to Matt what was saved.
- Skip anything already well-documented in KNOWLEDGE.md. Confirm rather than re-ask.
- Expected time: 45-90 minutes. Matt can stop and resume — save progress as you go.

**How to run it (for Matt):** Just answer like you're explaining it to a sharp new
controller on their first day. Plain language. "I don't know" or "Joanne handles that"
are valid answers — they tell Keystone where the knowledge lives.

---

## Section 1 — How a sale becomes money (the flow)

The most important section. We found GHL can't reliably report sold revenue (83% of deals
have no dollar value), so we need to understand the real flow.

1. Walk me through what happens financially the moment a closer signs a deal in the home.
   What gets recorded, where, and by whom?
2. Where is the TRUE record of "we sold $X this month"? GHL? Terros? A spreadsheet?
   Who owns that number and how is it calculated?
3. When does the deal become an invoice in QuickBooks? Who creates it? Same day, or batched?
4. How do you tag a deal as cash vs financed? Is that visible anywhere in QBO?
5. For the monthly "sold" numbers you track manually (Jan $130K → May $579K) — what's the
   exact source and method? Could Keystone pull it automatically, or does a human compile it?

## Section 2 — Job costing

6. When a deal is sold, does it get a budget (materials + labor + sub + overhead)? Where
   does that live?
7. How do you track actual costs against that budget as the job runs?
8. How do you find out if a job went over budget — and when (mid-job or after)?
9. Can you tell margin on a per-job basis today? If not, what's missing?

## Section 3 — COGS structure (the $106K mystery)

We found ~$106K in an uncategorized "Other COGS" bucket that made April look like a loss.

10. What's your rule for what goes in COGS vs Operating Expenses?
11. What's actually sitting in that "Other COGS" catch-all? Why does it end up there?
12. When you buy materials (Anlin PO), how/when is it recorded — at order, at delivery,
    at install? Does it match the month the job's revenue lands?
13. How are closer/setter commissions booked — when earned, or when paid? (We know pay
    lags installs ~21 days.) Cash basis or accrual?
14. Is Cameron's market-owner pay accrued anywhere, or is it completely off the books
    right now?

## Section 4 — Per-product costs & pricing

15. What does a window actually cost you from Anlin — per window, or by size/type? Give me
    real numbers or a range.
16. What's a healthy gross margin on a window job vs a roofing job vs a GC job, in your view?
17. How is the $2,300/window pricing floor enforced? What happens when a closer wants to
    go below it?
18. Roofing and GC cost structure — how does it differ from windows?

## Section 5 — Financing mechanics

19. Synchrony: walk me through the exact money flow. Deal signs → when does the 50% upfront
    hit the bank → when does the back 50% come → how is each piece recorded in QBO?
20. Green Sky: same walkthrough (20% upfront / 80% back).
21. What dealer/merchant fees do Synchrony and Green Sky charge per funded deal? Where do
    those fees show up?
22. How do you reconcile a financing payment back to the specific deal it funded?
23. The combined window+roofing deals financed under one loan — how do you currently split
    the revenue between trades, if at all?

## Section 6 — Payroll & commission calculation

24. Who calculates commissions each week, in what system, and from what source data?
25. Walk me through the exact pay cycle: sale Mon-Sat → paid which day → how is it run
    (Ramp? manual)?
26. New rep $300 guarantee — how is that tracked and reconciled against earned commission?
27. Clawbacks — has one ever actually happened? How do you pull it from future commissions
    mechanically?
28. Market owner pay (Cam, 25% of net): how do you calculate "net" for the market? What's
    in and out? When did you last run that math?

## Section 7 — Month-end close

29. Walk me through your month-end close process step by step. Who does what, in what order?
30. How long does close take, and what makes "the books are clean" true vs not?
31. What usually trips up the close or gets left half-done?
32. What reports do you produce at month-end and who sees them?

## Section 8 — Banking & cash management

33. Walk me through every bank/money account and what each is for (Ramp Business, Ramp
    Checking, Chase PERFBUS, QuickBooks Payments, the Investment account).
34. PERFBUS CHK goes negative periodically — is that an owner-draw pattern, a sweep timing
    thing, or a real overdraft? What's normal?
35. The Ramp Investment account went from $30K to $0 recently — what happened there?
36. How do transfers/sweeps between accounts work? Who moves money and when?
37. How do you decide when to pay yourself + Josh, and how is that recorded (draw vs salary)?

## Section 9 — What "normal" looks like

For each, give me the range that's routine vs the threshold that should alarm you:

38. Weekly revenue — normal range?
39. Monthly gross + net margin — what's healthy for Relentless specifically?
40. Cash on hand — what's the floor below which you get nervous?
41. AR — how much outstanding is normal, and how old before it's a problem?
42. Weekly bills/AP — normal range?

## Section 10 — Compliance & obligations

43. Tax setup — AZ TPT, UT sales tax, federal estimated. Who files, what cadence, are we
    current?
44. Are we setting aside money for taxes? What %? Where?
45. Insurance (GL, workers comp, commercial auto) — renewal dates and rough annual cost?
46. Contractor licenses (AZ ROC, UT DOPL) — renewal dates?
47. Anything else with a deadline that would hurt if we missed it?

## Section 11 — Vendors & suppliers

48. Top vendors by spend — who, and roughly how much per month?
49. Payment terms with Anlin and other key suppliers — net 30? COD? deposit?
50. Any single-supplier risk that worries you?

## Section 12 — Team & decision authority

51. Who does what in finance — you, Joanne, an outside CPA (if any)?
52. What decisions need your sign-off vs Joanne's vs Josh's?
53. If you got hit by a bus tomorrow, what would nobody else know how to do?

---

## After the interview

The conducting AI should:
1. Update `KEYSTONE_KNOWLEDGE.md` with everything learned, organized by topic.
2. Flag any answers that contradict what Keystone currently assumes.
3. List anything Matt didn't know / deferred — those become follow-ups for Joanne or a CPA.
4. Commit the updated KNOWLEDGE.md and confirm to Matt + Josh what changed.
