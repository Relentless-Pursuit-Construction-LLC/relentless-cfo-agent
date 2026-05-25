# Keystone — Relentless CFO Agent

Read-only Chief Financial Officer agent for **Relentless Pursuit Construction LLC**.

Watches QBO + Ramp + Chase. Surfaces cash position, AR aging, margin by job/crew/salesperson, anomalies, and 13-week cash flow forecast. Coaches Josh (owner), Matt (CFO), and Joanne (accounting manager) in their own language. **Never moves money.**

## Who Keystone is

See `docs/KEYSTONE_IDENTITY.md`. Short version: the fused intelligence of Ellen Rohr, Shawn Van Dyke, Michael Stone, Dominic Rubino, Tom Reber, and Tommy Mello — applied to Relentless specifically.

## The four jobs

| Job | Cadence | Output |
|---|---|---|
| **The Pulse** | Daily 6:30 AM MT | Cash + yesterday's revenue + one anomaly — under 50 words |
| **The Watch** | Real-time | NSF, fraud, missing deposits, large unexpected outflows |
| **The Audit** | Weekly Mon 7:00 AM MT | Margin by everything, AR aging, AP pacing, backlog health |
| **The Counsel** | Monthly 5th of new month | Full P&L walkthrough + 13-week cash flow forecast |

## Architecture

- **Runtime:** Python 3.13 on Railway (Railpack builder)
- **QBO access:** Shares the `/data` volume with `relentless-ghl-agent` (Matt's webhook service). Reads `qbo_tokens.json` as a passive consumer; webhook service is the token refresher.
- **Ramp access:** Direct API (read-only key)
- **Slack:** New "Keystone" bot (separate identity from the GHL agent)
- **LLM:** Anthropic Claude for prose generation (heartbeat copy, coaching language)

## Repo structure

```
relentless-cfo-agent/
├── main.py                    # FastAPI app + cron entrypoints
├── requirements.txt
├── keystone/
│   ├── qbo.py                 # QBO token mgmt + API helpers (passive reader)
│   ├── ramp.py                # Ramp API client
│   ├── slack_client.py        # Slack send helpers
│   ├── rep_mapping.py         # Fetches shared rep registry
│   ├── voice.py               # Keystone persona loaded for Claude calls
│   └── jobs/
│       ├── pulse.py           # Daily cash heartbeat
│       ├── ar_aging.py        # Per-rep + Matt AR digest
│       ├── watch.py           # Real-time anomalies
│       ├── audit.py           # Weekly Monday report
│       └── counsel.py         # Monthly P&L walkthrough
├── docs/
│   ├── KEYSTONE_IDENTITY.md
│   ├── KEYSTONE_SYSTEM_PROMPT.md
│   └── KEYSTONE_KNOWLEDGE_INDEX.md
└── README.md
```

## Environment variables

Required (set in Railway):

```
QBO_CLIENT_ID            # same as relentless-ghl-agent
QBO_CLIENT_SECRET        # same as relentless-ghl-agent
QBO_ENVIRONMENT=production
QBO_REALM_ID=9341455482460418
STATE_DIR=/data
RAMP_API_TOKEN
SLACK_BOT_TOKEN          # new Keystone bot, NOT the GHL agent bot
SLACK_SIGNING_SECRET
MATT_SLACK_ID
JOSH_SLACK_ID
FINANCE_SLACK_ID         # Joanne
ANTHROPIC_API_KEY
ADMIN_SECRET             # rotate from GHL agent's value
```

## Doctrine (non-negotiable)

- **Read-only on every source.** Never initiates transactions, never categorizes, never gives tax advice.
- **Halts on stale data.** If QBO sync is >48h old, flags rather than guesses.
- **Plain English to Josh.** Accountant-precise to Matt. Bookkeeper-precise to Joanne.
- **No motivational language about money.** Facts only.
- **No exclamation points. No emojis.**
- **Every flag ends with "what to do."** Reporting without coaching is half the job.

## Ownership

- **Code owner:** Josh Holland
- **Infrastructure shared with:** Matt (relentless-ghl-agent project on Railway)
- **Reviewers for architecture changes:** Matt

## Related repos

- `RelentlessMatt/relentless-ghl-agent` — sales-ops agent (writes invoices to QBO from GHL contracts)
- `Relentless-Pursuit-Construction-LLC/relentless-shared-config` — shared rep registry
