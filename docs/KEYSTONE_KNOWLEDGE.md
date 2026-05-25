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
