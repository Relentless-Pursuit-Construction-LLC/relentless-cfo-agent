# Keystone — Knowledge Base (Relentless-specific rules)

This file is **Matt's playground.** Anything written here gets loaded into every
Keystone Claude call alongside the persona prompt. Treat it as the institutional
memory Matt has built from doing the books since day one.

**Edit this file directly on GitHub. Commit → Railway redeploys → next report uses your update. No code changes required.**

---

## How to write entries

Each entry follows this loose structure:

```
- **<Short topic name>** — what to know
  - Pattern: what the data looks like
  - Cause: why it happens
  - Rule for Keystone: what to do (flag, ignore, escalate, contextualize)
```

Keep entries short and concrete. Real example below.

---

## Accounting patterns Keystone should know

> _Matt — fill this in. Examples below are placeholders to show the format. Replace them with real Relentless patterns._

- **PERFBUS CHK (5683) periodic overdrafts** — [PLACEHOLDER: confirm pattern]
  - Pattern: Account goes negative $5-15K on certain days of month
  - Cause: [confirm: owner draw schedule? routing transit? real overdraft?]
  - Rule for Keystone: [placeholder] Do not flag as critical unless negative > 5 business days OR amount > $20K.

- **Window Commissions COGS timing** — [PLACEHOLDER: confirm]
  - Pattern: "5101 Window Commissions - Closer Pay" lags actual installs by ~21 days
  - Cause: Closers paid on funded jobs, funding lags install date
  - Rule for Keystone: When reading monthly P&L margin, note that this month's commission cost reflects install volume from ~3 weeks prior.

- **QuickBooks Payments holding account** — confirmed
  - Pattern: "1004 QuickBooks Checking Account" balance is QB Payments holding, not real Chase
  - Cause: Card payments collected via QB Payments take 2-5 days to deposit to Chase
  - Rule for Keystone: Include in total cash but mark as "in transit." Never flag this balance as anomalous on its own.

## Vendor patterns

> _Matt — list out the vendors that need special handling. Examples:_
> - Andersen Windows Supply — primary window supplier, expect large monthly invoices
> - Milgard — secondary window supplier, smaller volume
> - [add others]

## Customer patterns

> _Matt — customers with quirks Keystone should know about:_
> - Stratton Custom Homes — large concentration risk, always 30-60 days, that's their normal pattern
> - [add others]

## Closer / rep patterns

> _Matt — anything Keystone should know about the closers' books:_
> - Cam (AZ) leads with windows, occasional roofing
> - Tegan (UT) windows primary
> - [add others]

## Sales compensation structure (confirmed by Josh 2026-05-25)

**Three positions per market:**
1. **Setter** — books appointments, paid on conversion of their bookings
2. **Closer** — sells in-home, paid on close (different rate depending on lead source)
3. **Market Owner** — runs the market (manager role), paid % of net market profit

### Setter compensation — weekly tiered structure

| Tier | Closes per week (from setter's bookings) | Commission rate | Sit pay |
|---|---|---|---|
| **Tier 1** | 0-2 | 5% per deal | $150 per sit |
| **Tier 2** | 3 | 7% per deal (applied retroactively to all 3 deals that week) | None |
| **Tier 3** | 4+ | 9% per deal (applied retroactively to all deals that week) | None |

### Closer compensation

- **9-10% per deal** when taking a lead from a setter (setter gets their tiered cut on the same deal)
- **18% per deal** when the closer knocked and set the appointment themselves (no setter share)
- Historical context: Relentless was 100% door-knocking until ~2 weeks ago, so MOST deals have been self-knocked = paying closers at 18%. The new Google + digital marketing campaigns should shift comp toward setter-fed leads (closer drops to 9-10%, setter gets 5-9%). Net: similar combined cost, but lighter per-closer payouts.

### Market Owner compensation

- **25% of net profit for the market**
- Net profit = market gross revenue minus office cost, recruiting, knocking gear for reps, etc.
- Josh estimates this works out to roughly **5-7% of gross revenue** that the market produces
- **Paid monthly on the 15th, for prior month's installs (not closes)** — paid on what actually installed last month, not what was sold last month
- AZ: Cameron Kendall is the market owner
- UT: NO market owner currently. Tegan is Josh + Matt's partner, so UT market profit stays in the company

**CRITICAL OPEN ITEM:** Cameron hasn't been paid market owner pay yet this year because the books are messy. This is liability sitting on the balance sheet that's not being tracked. **Keystone should flag this prominently — it represents accrued comp owed to Cam that needs to be calculated and either paid or formally deferred.**

### Total sales-comp stack on a typical deal

| Lead source | Setter % | Closer % | Market owner % (of net) | Combined % of gross |
|---|---|---|---|---|
| Setter-fed lead | 5-9% | 9-10% | ~5-7% | **19-26%** |
| Self-knocked (closer = setter) | 0% | 18% | ~5-7% | **23-25%** |

Industry benchmark for residential window install "SG&A" (sales, general, admin) is 18-25% of revenue. Relentless sits at the high end of that band — historically because of 100% self-knocking at 18%. Should improve as setter-fed mix increases.

### Pay cycles

- **Setter + Closer commission:** weekly. Sell Mon-Sat, deposit collected, paid following Friday.
- **Market Owner:** monthly. Paid the 15th for prior month's installs.
- **New rep guarantee:** $300/week guaranteed for the first 2-3 weeks while training. Rep gets whichever is GREATER between guarantee and commission earned.

### Clawbacks

- 3-day federal right of rescission applies (cooling-off period)
- After day 3, if a job cancels, **closer's clawback comes out of future commissions** (not direct repayment)
- **Keystone should track this** — cancellations after rescission represent margin events that need to flow through the closer's commission ledger

**How Keystone should reason about this:**

- Tier escalation is retroactive — a closer's 3rd close of the week bumps their first 2 closes up to 7%, not just the 3rd
- Net commission cost shifts materially by tier:
  - Tier 1: 5% of revenue (best margin for company)
  - Tier 2: 7% of revenue (4 points worse, but volume offsets)
  - Tier 3: 9% of revenue (4 more points worse, but higher unit count + lower CAC per deal)
- Sit pay is a fixed cost — $150 × number of sits regardless of close. Closers stuck at Tier 1 with many sits = high cost-per-deal.
- COGS "5101 Window Commissions - Closer Pay" reflects this comp structure
- Closer commission lags installs by ~21 days because closers paid on funded jobs (per memory: Brady-style funded-job comp)

**What Keystone should flag:**

- Closers consistently in Tier 1 for 3+ weeks running — coaching opportunity (either training problem or wrong role)
- Closers in Tier 3 with low AOV — they're discounting to land deals, eating the gross margin gain back
- Weeks where commission COGS spikes — likely a Tier 3 hit (good) OR a sit-heavy/low-close week at Tier 1 (bad)
- A market trending toward more Tier 3 weeks vs Tier 1 = positive operational signal

**What Keystone should NOT do:**

- Recommend changing the comp structure — that's a Josh + Matt strategic decision
- Compare commissions across markets without controlling for AOV and close rate (different deal mix changes the math)

## Pricing floor

- **Minimum target: $2,300 per window** as a base
- Historical floor: dipped to **$1,700 per window** at times — this is the discount territory that erodes margin
- Keystone should flag deals/jobs where avg per-window price is under $2,000 — that's margin pressure

## Suppliers

- **Anlin Windows — primary current supplier** (switched ~recently from Alside)
- Have supplier access to: Andersen, Milgard, Pella — but **haven't sold any of these yet**
- Matt is the authoritative source on per-window costs and supplier-specific pricing — Keystone should consult Matt for cost benchmarks rather than guess

## Installers (subcontractors)

- All installers are **1099 subcontractors** (no W-2 install crews)
- **Arizona:** 2 main installers, each running 4 crews = ~8 crews total
- **Utah:** 1 main installer with crews (count TBD)

## Lead sources

- **Historical: 100% door-knocking** through ~mid-May 2026
- **New as of ~mid-May:** Google ads + digital marketing campaign launched
- Keystone should watch the lead-source mix shift as the marketing pipeline matures — this will materially affect closer comp (18% self-knocked → 9-10% setter-fed) and CAC

## Permits + inspections

- **No permits or inspections needed for window installs** — simplifies cash conversion compared to roofing or remodel work

## Warranty

- **Manufacturer covers almost all warranty issues** (Anlin handles most claims)
- Warranty reserve set-aside is therefore less critical for Relentless than for trades where warranty falls on the installer

## Customer financing

- **Two financing partners:** Synchrony and Green Sky
- **About 50% of overall sales volume is financed** through these two
- **Synchrony:** 50% paid upfront to Relentless on funded deal, 50% on backend (after install). NEW as of ~mid-May 2026.
- **Green Sky:** 20% upfront, 80% backend.
- **Pre-mid-May 2026:** financed deals paid NOTHING upfront — Relentless carried 100% of materials + labor cost before any cash came in. This was a major working capital strain. The Synchrony shift is a meaningful cash flow improvement.

**Keystone should track financed vs cash deals separately** — they have radically different cash flow profiles. A "great month" of financed deals can still create a cash crunch if upfront funding doesn't cover crew + materials.

## Utah multi-trade

- AZ is **windows + roofing** only
- **UT does general contracting** in addition to windows: kitchen remodels, decks, siding, bathroom remodels
- Some UT customers have bought windows AND other projects from Relentless
- **Window + roofing combined deals exist** — some financed under ONE loan, which makes attribution between the two trades hard. Keystone should help propose a cleaner way to track these.

## GoHighLevel = source of truth for sales

- **GHL is the system of record for closers, customers, and opportunities**
- **QBO is just for invoicing** — invoices flow from GHL → Matt's webhook service → QBO
- This is why Customer.SalesRep is empty in QBO — the rep info lives in GHL, not QBO
- **Keystone should cross-reference QBO and GHL** during audits to get full attribution

## Open items / things Keystone should always ask Matt or Josh about

- Per-window manufacturer costs (Matt knows)
- Specific clawback amounts ever taken from closer commissions
- The accrued (unpaid) Cameron market-owner liability — needs to be calculated and addressed
- Tracking proposal for combined window-roofing financed deals

## Sales tax / compliance gotchas

> _Matt — what Keystone should remember about tax compliance:_
> - AZ TPT rates differ by city — Phoenix vs Mesa vs Scottsdale all different
> - UT sales tax: monthly filing
> - Workers comp audit: year-end true-up of payroll classifications

## What "normal" looks like for Relentless

> _Matt — set the calibration for "normal" weekly/monthly/quarterly patterns:_
> - Normal weekly revenue: $X-$Y
> - Normal weekly bills paid: $X-$Y
> - Normal gross margin range: X-Y%
> - Normal payroll cycle: weekly Friday, gross ~$X
> - [fill in]

## What "abnormal" should look like

> _Matt — what should make Keystone immediately concerned:_
> - Cash below $X (set the real floor, not the placeholder $50K)
> - Any single vendor invoice > $X
> - Any customer balance > X% of total AR
> - Any week with revenue < $X
> - [fill in]

## Override Keystone's default reactions

> _Matt — places where Keystone's defaults are wrong for Relentless:_
> - Default: "Cash drop > $10K day-over-day → flag." Reality: it's normal on bill-pay Fridays. Quiet down on Fridays.
> - [add more]

---

## How Keystone uses this file

Every Claude call that generates a report (Pulse, AR digest, Audit, Counsel) loads:
1. `docs/KEYSTONE_IDENTITY.md` — who Keystone is
2. `docs/KEYSTONE_SYSTEM_PROMPT.md` — operational doctrine
3. `docs/KEYSTONE_KNOWLEDGE_INDEX.md` — frameworks
4. **`docs/KEYSTONE_KNOWLEDGE.md`** — this file, Matt's institutional memory

When evaluating data, Keystone checks this file first to see if there's a Relentless-specific rule that overrides its generic interpretation. Example: it sees PERFBUS CHK -$8K and consults this file before flagging.

## Edit cadence

Matt: edit whenever you spot something Keystone should know. No formality required. The next report uses your update.

Josh: review periodically. If Matt's rules conflict with how you want Keystone to behave, push back via PR or comment.

## Sign-off — Keystone's voice rules still apply

When generating outputs, Keystone never quotes from this file directly. It uses the knowledge to make better decisions, but the voice stays plain trades English to Josh, accountant-precise to Matt, bookkeeper-precise to Joanne.

— Keystone
