"""The Audit — weekly Monday report.

Runs Monday 7:00 AM MT. Pulls a comprehensive weekly snapshot from QBO and
returns two voice-faithful payloads (Josh digest + Matt full) plus structured
stats and any data-quality flags surfaced during the pull.

Read-only on QBO. Writes a snapshot to /data/audit_history.json so the
following week can compute week-over-week deltas. Never sends Slack from
here — the caller (cron endpoint) decides delivery.

Sections:
  1. Cash position (cash + WoW + 7-day avg)
  2. Revenue last week (Mon-Sat vs $75K target)
  3. AR aging snapshot (open AR, % current, top 3 oldest)
  4. AP pacing (bills due next 14 days, % of operating cash)
  5. Margin signal (Income - COGS / Income, P&L last week)
  6. Backlog hint (open invoice count + $) — Phase 2 caveat: GHL contract data
  7. Cash conversion velocity (avg days invoice -> paid, last 30 days)
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from keystone import qbo

# --- Config ---------------------------------------------------------------

STATE_DIR = os.environ.get("STATE_DIR", "/data")
AUDIT_HISTORY_PATH = f"{STATE_DIR}/audit_history.json"

WEEKLY_REVENUE_TARGET = 75_000.00

# Thresholds (from KEYSTONE_KNOWLEDGE_INDEX.md)
AR_CURRENT_PCT_HEALTHY = 0.90        # >=90% under 30 days = healthy
AR_CURRENT_PCT_YELLOW = 0.75         # 75-90% yellow, <75% red
AP_PCT_OPERATING_CASH_YELLOW = 0.30  # bills 14d / cash > 30% = yellow
AP_PCT_OPERATING_CASH_RED = 0.50     # > 50% = red
CASH_RED_THRESHOLD = 50_000.00       # escalation line
CASH_YELLOW_THRESHOLD = 100_000.00
WINDOW_GM_HEALTHY_LOW = 0.35
WINDOW_GM_HEALTHY_HIGH = 0.45
ROOFING_GM_HEALTHY_LOW = 0.25
ROOFING_GM_HEALTHY_HIGH = 0.35
CASH_CONV_HEALTHY_DAYS = 14          # balance collection target
CASH_CONV_YELLOW_DAYS = 30

AZ_TZ_LABEL = "MT"  # Arizona is MST year-round (no DST)


# --- Helpers --------------------------------------------------------------


def _now_az() -> datetime:
    """Server runs UTC; AZ is UTC-7 year-round. Use a fixed offset."""
    return datetime.utcnow() - timedelta(hours=7)


def _fmt_dollars(n: float | None) -> str:
    if n is None:
        return "n/a"
    return f"${n:,.0f}"


def _fmt_pct(n: float | None, digits: int = 1) -> str:
    if n is None:
        return "n/a"
    return f"{n * 100:.{digits}f}%"


def _signed_delta(curr: float | None, prev: float | None) -> str:
    if curr is None or prev is None:
        return "n/a (no prior week)"
    delta = curr - prev
    sign = "+" if delta >= 0 else "−"
    return f"{sign}{_fmt_dollars(abs(delta))}"


def _signed_delta_pct(curr: float | None, prev: float | None) -> str:
    if curr is None or prev is None or prev == 0:
        return "n/a"
    delta = (curr - prev) / prev
    sign = "+" if delta >= 0 else "−"
    return f"{sign}{abs(delta) * 100:.1f}%"


def _color(value: float, healthy_low: float, yellow_low: float, *, higher_is_better: bool = True) -> str:
    """Return 'healthy' / 'yellow' / 'red' for a metric."""
    if higher_is_better:
        if value >= healthy_low:
            return "healthy"
        if value >= yellow_low:
            return "yellow"
        return "red"
    else:
        if value <= healthy_low:
            return "healthy"
        if value <= yellow_low:
            return "yellow"
        return "red"


# --- QBO report parsing ---------------------------------------------------
# QBO returns reports as a nested Header/Columns/Rows tree. These walkers
# extract the totals we need without coupling to a specific row order.


def _walk_rows(rows_block: Any) -> list[dict[str, Any]]:
    """Flatten QBO Report Row tree into a list of leaf+summary rows."""
    out: list[dict[str, Any]] = []
    if not rows_block:
        return out
    if isinstance(rows_block, dict):
        rows = rows_block.get("Row", [])
    else:
        rows = rows_block
    for r in rows:
        out.append(r)
        if "Rows" in r:
            out.extend(_walk_rows(r["Rows"]))
        if "Summary" in r:
            # Summary is a row-shape but lives off the parent Row
            out.append({"type": "Summary", "Summary": r["Summary"], "group": r.get("group")})
    return out


def _row_label_amount(row: dict[str, Any]) -> tuple[str | None, float | None]:
    """Pull (label, amount) from a typical QBO ColData row. Amount is last col."""
    cols = row.get("ColData") or row.get("Summary", {}).get("ColData")
    if not cols:
        return None, None
    label = cols[0].get("value") if cols else None
    amt_str = cols[-1].get("value") if cols else None
    try:
        amt = float(amt_str) if amt_str not in (None, "") else None
    except ValueError:
        amt = None
    return label, amt


def _find_amount_by_label(report: dict[str, Any], label_substr: str) -> float | None:
    """Find first row whose label contains the substring (case-insensitive)."""
    rows = _walk_rows(report.get("Rows"))
    target = label_substr.lower()
    for r in rows:
        label, amt = _row_label_amount(r)
        if label and target in label.lower() and amt is not None:
            return amt
    return None


def _find_group_total(report: dict[str, Any], group_name: str) -> float | None:
    """Walk Rows looking for a group whose 'group' attr matches; return its Summary total."""
    rows = report.get("Rows", {}).get("Row", []) if isinstance(report.get("Rows"), dict) else []
    target = group_name.lower()

    def _scan(rs: list[dict[str, Any]]) -> float | None:
        for r in rs:
            grp = (r.get("group") or "").lower()
            if grp == target:
                summary = r.get("Summary", {}).get("ColData", [])
                if summary:
                    last = summary[-1].get("value")
                    try:
                        return float(last) if last not in (None, "") else None
                    except ValueError:
                        return None
            sub = r.get("Rows", {}).get("Row", []) if isinstance(r.get("Rows"), dict) else []
            if sub:
                found = _scan(sub)
                if found is not None:
                    return found
        return None

    return _scan(rows)


# --- Section builders -----------------------------------------------------


def _cash_position(as_of: date, flags: list[str]) -> dict[str, Any]:
    """Pull cash from balance sheet — sum of Bank-type accounts."""
    try:
        bs = qbo.get_balance_sheet(as_of.isoformat())
    except Exception as e:
        flags.append(f"balance_sheet_unavailable: {e}")
        return {"cash": None, "source": "unavailable"}

    # Bank accounts live under Assets > Current Assets > Bank Accounts in QBO.
    # We try labeled lookups in order of specificity.
    cash = _find_amount_by_label(bs, "Total Bank Accounts")
    if cash is None:
        cash = _find_amount_by_label(bs, "Bank Accounts")
    if cash is None:
        # Last resort: Total Current Assets (overstates — flag it)
        cash = _find_amount_by_label(bs, "Total Current Assets")
        if cash is not None:
            flags.append(
                "cash_proxy: used Total Current Assets — Bank Accounts row not found"
            )

    return {"cash": cash, "source": "QBO BalanceSheet"}


def _revenue_last_week(week_start: date, week_end: date, flags: list[str]) -> dict[str, Any]:
    """Revenue Mon-Sat from P&L Total Income."""
    try:
        pl = qbo.get_profit_loss(week_start.isoformat(), week_end.isoformat())
    except Exception as e:
        flags.append(f"profit_loss_unavailable: {e}")
        return {"revenue": None, "target": WEEKLY_REVENUE_TARGET, "pct_to_goal": None}

    income = _find_amount_by_label(pl, "Total Income")
    pct = (income / WEEKLY_REVENUE_TARGET) if income is not None else None
    return {
        "revenue": income,
        "target": WEEKLY_REVENUE_TARGET,
        "pct_to_goal": pct,
        "window": f"{week_start.isoformat()}..{week_end.isoformat()}",
    }


def _ar_snapshot(as_of: date, flags: list[str]) -> dict[str, Any]:
    """Total open AR, % current, top 3 oldest invoices by age."""
    try:
        summary = qbo.get_ar_aging_summary(as_of.isoformat())
        detail = qbo.get_ar_aging_detail(as_of.isoformat())
    except Exception as e:
        flags.append(f"ar_aging_unavailable: {e}")
        return {"total_ar": None, "pct_current": None, "top_oldest": []}

    # Summary report has bucket totals as columns of a Total row
    total_ar = _find_amount_by_label(summary, "Total") or _find_amount_by_label(summary, "TOTAL")

    # Walk the summary columns to find the "Current" / "1-30" bucket totals
    rows = _walk_rows(summary.get("Rows"))
    pct_current = None
    if rows and total_ar:
        # Find the "TOTAL" row's ColData breakdown
        for r in rows:
            cols = r.get("Summary", {}).get("ColData") or r.get("ColData", [])
            if cols and (cols[0].get("value", "").lower().startswith("total")):
                # Columns are typically: Customer, Current, 1-30, 31-60, 61-90, 91+, Total
                amounts: list[float] = []
                for c in cols[1:]:
                    try:
                        amounts.append(float(c.get("value", "") or 0))
                    except ValueError:
                        amounts.append(0.0)
                if len(amounts) >= 2 and total_ar > 0:
                    # Current bucket is amounts[0]; "1-30" is amounts[1].
                    # "Under 30 days" = Current + 1-30.
                    under_30 = amounts[0] + (amounts[1] if len(amounts) > 1 else 0)
                    pct_current = under_30 / total_ar
                break

    # Top 3 oldest from detail
    top_oldest: list[dict[str, Any]] = []
    try:
        detail_rows = _walk_rows(detail.get("Rows"))
        invoices: list[dict[str, Any]] = []
        for r in detail_rows:
            cols = r.get("ColData")
            if not cols or len(cols) < 5:
                continue
            # Typical AR detail cols: Date, Txn Type, Num, Customer, Due Date, Aging, Open Balance, Amount
            values = [c.get("value", "") for c in cols]
            # Find aging (integer) and balance (float) heuristically
            age = None
            balance = None
            for v in values:
                if v and v.replace("-", "").isdigit() and age is None and len(v) <= 4:
                    try:
                        age = int(v)
                    except ValueError:
                        pass
            try:
                balance = float(values[-1]) if values[-1] else None
            except ValueError:
                balance = None
            customer = next((v for v in values if v and not v.replace(".", "").replace("-", "").isdigit()), "")
            if balance and balance > 0 and age is not None:
                invoices.append({"customer": customer, "age_days": age, "balance": balance})
        invoices.sort(key=lambda x: x["age_days"], reverse=True)
        top_oldest = invoices[:3]
    except Exception as e:
        flags.append(f"ar_aging_detail_parse_failed: {e}")

    return {
        "total_ar": total_ar,
        "pct_current": pct_current,
        "top_oldest": top_oldest,
    }


def _ap_pacing(as_of: date, cash: float | None, flags: list[str]) -> dict[str, Any]:
    """Bills due in next 14 days via QBO Bill query."""
    end = as_of + timedelta(days=14)
    try:
        q = (
            f"SELECT Id, DueDate, Balance, VendorRef FROM Bill "
            f"WHERE Balance > '0' AND DueDate <= '{end.isoformat()}' "
            f"MAXRESULTS 1000"
        )
        result = qbo.qbo_query(q)
    except Exception as e:
        flags.append(f"ap_query_unavailable: {e}")
        return {"bills_due_14d": None, "count": 0, "pct_of_cash": None}

    bills = result.get("QueryResponse", {}).get("Bill", []) or []
    total = sum(float(b.get("Balance", 0) or 0) for b in bills)
    pct = (total / cash) if cash and cash > 0 else None
    return {
        "bills_due_14d": total,
        "count": len(bills),
        "pct_of_cash": pct,
        "window_end": end.isoformat(),
    }


def _margin_signal(week_start: date, week_end: date, flags: list[str]) -> dict[str, Any]:
    """Gross margin = (Income - COGS) / Income from last week's P&L."""
    try:
        pl = qbo.get_profit_loss(week_start.isoformat(), week_end.isoformat())
    except Exception as e:
        flags.append(f"pl_margin_unavailable: {e}")
        return {"income": None, "cogs": None, "gross_margin": None}

    income = _find_amount_by_label(pl, "Total Income")
    cogs = _find_amount_by_label(pl, "Total Cost of Goods Sold")
    if cogs is None:
        cogs = _find_amount_by_label(pl, "Total COGS")

    if cogs is None or cogs == 0:
        flags.append(
            "cogs_missing_or_zero: P&L shows no Cost of Goods Sold for the week — "
            "Joanne to confirm material/labor expense categorization"
        )

    gm = None
    if income and income > 0 and cogs is not None:
        gm = (income - cogs) / income

    return {"income": income, "cogs": cogs, "gross_margin": gm}


def _backlog_hint(flags: list[str]) -> dict[str, Any]:
    """Open invoice count + total $ as a weak backlog proxy."""
    try:
        result = qbo.get_open_invoices()
    except Exception as e:
        flags.append(f"open_invoices_unavailable: {e}")
        return {"open_count": None, "open_total": None}

    invoices = result.get("QueryResponse", {}).get("Invoice", []) or []
    total = sum(float(inv.get("Balance", 0) or 0) for inv in invoices)
    return {
        "open_count": len(invoices),
        "open_total": total,
        "caveat": "Phase 2: improve once GHL contract data is wired in",
    }


def _cash_conversion(as_of: date, flags: list[str]) -> dict[str, Any]:
    """Avg days invoice issued -> paid, for invoices fully paid in last 30 days."""
    start = as_of - timedelta(days=30)
    try:
        q = (
            f"SELECT TxnDate, Balance, MetaData, LinkedTxn, Id "
            f"FROM Invoice WHERE Balance = '0' AND TxnDate >= '{start.isoformat()}' "
            f"MAXRESULTS 1000"
        )
        result = qbo.qbo_query(q)
    except Exception as e:
        flags.append(f"cash_conversion_unavailable: {e}")
        return {"avg_days": None, "sample_size": 0}

    invoices = result.get("QueryResponse", {}).get("Invoice", []) or []
    days: list[int] = []
    for inv in invoices:
        try:
            issued = datetime.fromisoformat(inv["TxnDate"]).date()
            # LastUpdatedTime on a zero-balance invoice is a reasonable paid-date proxy
            meta = inv.get("MetaData", {})
            updated_raw = meta.get("LastUpdatedTime", "")
            # Strip timezone for simple parse
            updated_raw = re.sub(r"[+-]\d{2}:\d{2}$", "", updated_raw).rstrip("Z")
            updated = datetime.fromisoformat(updated_raw).date() if updated_raw else None
            if updated:
                d = (updated - issued).days
                if d >= 0:
                    days.append(d)
        except (KeyError, ValueError):
            continue

    if not days:
        return {"avg_days": None, "sample_size": 0}
    return {"avg_days": sum(days) / len(days), "sample_size": len(days)}


# --- History persistence --------------------------------------------------


def _load_history() -> dict[str, Any]:
    path = Path(AUDIT_HISTORY_PATH)
    if not path.exists():
        return {"snapshots": []}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"snapshots": []}


def _save_snapshot(snapshot: dict[str, Any]) -> None:
    history = _load_history()
    history["snapshots"].append(snapshot)
    # Keep last 26 weeks (~6 months)
    history["snapshots"] = history["snapshots"][-26:]
    path = Path(AUDIT_HISTORY_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(history, f, indent=2)
        tmp.replace(path)
    except OSError:
        # Don't blow up the report if /data is read-only in dev
        pass


def _prior_snapshot(history: dict[str, Any]) -> dict[str, Any] | None:
    snaps = history.get("snapshots", [])
    return snaps[-1] if snaps else None


# --- Message composers (deterministic, no LLM) ----------------------------


def _assess_cash(cash: float | None) -> str:
    if cash is None:
        return "unknown"
    if cash < CASH_RED_THRESHOLD:
        return "red"
    if cash < CASH_YELLOW_THRESHOLD:
        return "yellow"
    return "healthy"


def _assess_revenue(pct: float | None) -> str:
    if pct is None:
        return "unknown"
    if pct >= 1.0:
        return "healthy"
    if pct >= 0.70:
        return "yellow"
    return "red"


def _assess_ar(pct_current: float | None) -> str:
    if pct_current is None:
        return "unknown"
    if pct_current >= AR_CURRENT_PCT_HEALTHY:
        return "healthy"
    if pct_current >= AR_CURRENT_PCT_YELLOW:
        return "yellow"
    return "red"


def _assess_ap(pct: float | None) -> str:
    if pct is None:
        return "unknown"
    if pct < AP_PCT_OPERATING_CASH_YELLOW:
        return "healthy"
    if pct < AP_PCT_OPERATING_CASH_RED:
        return "yellow"
    return "red"


def _assess_margin(gm: float | None) -> str:
    """Use window install range as primary benchmark — Relentless is windows-primary."""
    if gm is None:
        return "unknown"
    if gm >= WINDOW_GM_HEALTHY_LOW:
        return "healthy"
    if gm >= ROOFING_GM_HEALTHY_LOW:
        return "yellow"
    return "red"


def _assess_cash_conv(avg_days: float | None) -> str:
    if avg_days is None:
        return "unknown"
    if avg_days <= CASH_CONV_HEALTHY_DAYS:
        return "healthy"
    if avg_days <= CASH_CONV_YELLOW_DAYS:
        return "yellow"
    return "red"


def _compose_josh_digest(stats: dict[str, Any], flags: list[str], stamp: str) -> str:
    """Plain English, under 500 words. Where we are / what changed / what to do."""
    cash = stats["cash"]["cash"]
    cash_prev = stats["cash"].get("prev_cash")
    cash_avg7 = stats["cash"].get("avg_7d")
    cash_color = _assess_cash(cash)

    rev = stats["revenue"]["revenue"]
    rev_pct = stats["revenue"]["pct_to_goal"]
    rev_color = _assess_revenue(rev_pct)

    ar_total = stats["ar"]["total_ar"]
    ar_pct = stats["ar"]["pct_current"]
    ar_color = _assess_ar(ar_pct)

    ap_total = stats["ap"]["bills_due_14d"]
    ap_pct = stats["ap"]["pct_of_cash"]
    ap_color = _assess_ap(ap_pct)

    gm = stats["margin"]["gross_margin"]
    gm_color = _assess_margin(gm)

    open_count = stats["backlog"]["open_count"]
    open_total = stats["backlog"]["open_total"]

    avg_days = stats["cash_conv"]["avg_days"]
    conv_color = _assess_cash_conv(avg_days)

    lines: list[str] = []
    lines.append("Weekly audit — where we stand, what changed, what to do.")
    lines.append("")
    lines.append("Cash on hand: " + _fmt_dollars(cash) + f" ({cash_color}).")
    if cash_prev is not None and cash is not None:
        lines[-1] += f" Week-over-week: {_signed_delta(cash, cash_prev)}."
    if cash_avg7 is not None:
        lines.append(f"7-day average: {_fmt_dollars(cash_avg7)}.")
    if cash_color == "red":
        lines.append("Move: cash under $50K. Hold any non-essential spend and confirm next deposit dates with Joanne.")
    elif cash_color == "yellow":
        lines.append("Move: cash under $100K. Pull AR list and chase the top 3 oldest this week.")

    lines.append("")
    lines.append(
        f"Revenue last week: {_fmt_dollars(rev)} against the $75K target "
        f"({_fmt_pct(rev_pct)}). {rev_color}."
    )
    if rev_color != "healthy":
        lines.append("Move: review sits-to-close last week with Cam and Tegan. Funnel math says 15 sits = 1 close target — count the sits.")

    lines.append("")
    lines.append(
        f"Open receivables: {_fmt_dollars(ar_total)} total, "
        f"{_fmt_pct(ar_pct) if ar_pct is not None else 'n/a'} under 30 days ({ar_color})."
    )
    top = stats["ar"].get("top_oldest") or []
    if top:
        lines.append("Top 3 oldest:")
        for inv in top:
            lines.append(f"  - {inv['customer']}: {_fmt_dollars(inv['balance'])} at {inv['age_days']} days")
    if ar_color != "healthy":
        lines.append("Move: Joanne calls the top 3 oldest this week. Anything over 60 days needs a written commitment date.")

    lines.append("")
    lines.append(
        f"Bills due next 14 days: {_fmt_dollars(ap_total)} "
        f"({_fmt_pct(ap_pct) if ap_pct is not None else 'n/a'} of cash) — {ap_color}."
    )
    if ap_color == "red":
        lines.append("Move: bills due in 2 weeks exceed half of cash on hand. Sit with Matt on payment sequencing before any new spend.")
    elif ap_color == "yellow":
        lines.append("Move: AP pacing is tight. Hold non-essential card spend until after this Friday.")

    lines.append("")
    if gm is not None:
        lines.append(f"Gross margin last week: {_fmt_pct(gm)} ({gm_color}).")
        if gm_color != "healthy":
            lines.append("Move: margin below the 35% window target. Pull last week's sold jobs and compare price to materials + labor.")
    else:
        lines.append("Gross margin: cannot calculate — COGS is empty in QBO. Joanne to confirm material and labor categorization.")

    lines.append("")
    lines.append(
        f"Open invoices (backlog proxy): {open_count if open_count is not None else 'n/a'} "
        f"invoices, {_fmt_dollars(open_total)} outstanding. "
        f"Note: weak proxy — we will improve this once GHL contract data is wired in."
    )

    lines.append("")
    if avg_days is not None:
        lines.append(
            f"Cash conversion: {avg_days:.1f} days average from invoice to paid "
            f"(last 30 days, n={stats['cash_conv']['sample_size']}) — {conv_color}."
        )
        if conv_color != "healthy":
            lines.append("Move: review collection cadence with Joanne. Cash deals should clear in under 7 days, balances under 14.")
    else:
        lines.append("Cash conversion: not enough paid invoices in last 30 days to compute.")

    if flags:
        lines.append("")
        lines.append("Data flags (review with Joanne):")
        for f in flags:
            lines.append(f"  - {f}")

    lines.append("")
    lines.append("— Keystone")
    lines.append(f"Data pulled: {stamp}")
    return "\n".join(lines)


def _compose_matt_full(stats: dict[str, Any], flags: list[str], stamp: str) -> str:
    """Accountant-precise. Ratios + dollars. GAAP terms welcome."""
    cash = stats["cash"]["cash"]
    cash_prev = stats["cash"].get("prev_cash")
    cash_avg7 = stats["cash"].get("avg_7d")

    rev = stats["revenue"]["revenue"]
    rev_prev = stats["revenue"].get("prev_revenue")

    ar = stats["ar"]
    ap = stats["ap"]
    margin = stats["margin"]
    backlog = stats["backlog"]
    conv = stats["cash_conv"]

    lines: list[str] = []
    lines.append("WEEKLY AUDIT — Relentless Pursuit Construction LLC")
    lines.append(f"Reporting period: {stats['revenue']['window']}")
    lines.append("")
    lines.append("1. CASH POSITION")
    lines.append(f"   Cash on hand (sum of Bank-type GL accounts): {_fmt_dollars(cash)}")
    lines.append(f"   Prior week cash: {_fmt_dollars(cash_prev)}")
    lines.append(f"   Δ WoW: {_signed_delta(cash, cash_prev)} ({_signed_delta_pct(cash, cash_prev)})")
    lines.append(f"   7-day rolling avg cash: {_fmt_dollars(cash_avg7)}")
    lines.append(f"   Assessment: {_assess_cash(cash)}")
    lines.append("")
    lines.append("2. REVENUE (Accrual basis, last week Mon-Sat)")
    lines.append(f"   Total Income: {_fmt_dollars(rev)}")
    lines.append(f"   Prior week: {_fmt_dollars(rev_prev)}  Δ: {_signed_delta(rev, rev_prev)} ({_signed_delta_pct(rev, rev_prev)})")
    lines.append(f"   Target: {_fmt_dollars(WEEKLY_REVENUE_TARGET)}  Attainment: {_fmt_pct(stats['revenue']['pct_to_goal'])}")
    lines.append(f"   Assessment: {_assess_revenue(stats['revenue']['pct_to_goal'])}")
    lines.append("")
    lines.append("3. AR AGING SNAPSHOT")
    lines.append(f"   Total open AR: {_fmt_dollars(ar['total_ar'])}")
    lines.append(f"   % under 30 days (Current + 1-30 buckets): {_fmt_pct(ar['pct_current'])}")
    lines.append(f"   Healthy threshold: ≥{int(AR_CURRENT_PCT_HEALTHY*100)}%  Assessment: {_assess_ar(ar['pct_current'])}")
    if ar.get("top_oldest"):
        lines.append("   Top 3 oldest:")
        for inv in ar["top_oldest"]:
            lines.append(f"     • {inv['customer']} — {_fmt_dollars(inv['balance'])} at {inv['age_days']} days")
    lines.append("")
    lines.append("4. AP PACING (bills due in next 14 days)")
    lines.append(f"   Bills due ≤ {ap.get('window_end', 'n/a')}: {_fmt_dollars(ap['bills_due_14d'])} across {ap['count']} bills")
    lines.append(f"   % of operating cash: {_fmt_pct(ap['pct_of_cash'])}")
    lines.append(f"   Thresholds: <30% healthy / 30-50% yellow / >50% red.  Assessment: {_assess_ap(ap['pct_of_cash'])}")
    lines.append("")
    lines.append("5. MARGIN SIGNAL (P&L, last week)")
    lines.append(f"   Total Income: {_fmt_dollars(margin['income'])}")
    lines.append(f"   Total COGS: {_fmt_dollars(margin['cogs'])}")
    lines.append(f"   Gross margin: {_fmt_pct(margin['gross_margin'])}")
    lines.append(f"   Benchmarks: windows 35-45%, roofing 25-35%.  Assessment: {_assess_margin(margin['gross_margin'])}")
    if margin["cogs"] in (None, 0):
        lines.append("   NOTE: COGS is empty or zero. Direct material/labor likely posted to OpEx — Joanne to reclassify.")
    lines.append("")
    lines.append("6. BACKLOG HINT (open invoice $ as weak proxy)")
    lines.append(f"   Open invoice count: {backlog['open_count']}")
    lines.append(f"   Open invoice $: {_fmt_dollars(backlog['open_total'])}")
    lines.append("   Caveat: this is not true backlog. WIP / signed-not-started will be sourced from GHL in Phase 2.")
    lines.append("")
    lines.append("7. CASH CONVERSION VELOCITY")
    lines.append(f"   Avg days invoice → paid, last 30 days: {conv['avg_days']:.1f}" if conv['avg_days'] is not None else "   Avg days invoice → paid: n/a")
    lines.append(f"   Sample size: {conv['sample_size']} fully-paid invoices")
    lines.append(f"   Targets: ≤7 days (cash deals) / ≤14 days (balances).  Assessment: {_assess_cash_conv(conv['avg_days'])}")

    if flags:
        lines.append("")
        lines.append("DATA-QUALITY FLAGS")
        for f in flags:
            lines.append(f"  - {f}")

    lines.append("")
    lines.append("— Keystone")
    lines.append(f"Data pulled: {stamp}")
    return "\n".join(lines)


# --- Main entrypoint ------------------------------------------------------


def run_audit(as_of: date | None = None) -> dict[str, Any]:
    """Run the weekly audit. Returns dict with josh_message, matt_message, stats, flags.

    `as_of` defaults to today. Last week's window is the prior Mon-Sat.
    """
    flags: list[str] = []
    now = _now_az()
    as_of = as_of or now.date()

    # Last week's window: prior Monday through prior Saturday.
    # Monday = weekday 0. If today is Monday, "last week" = the 6 days ending yesterday.
    days_since_mon = as_of.weekday()  # Mon=0
    this_monday = as_of - timedelta(days=days_since_mon)
    last_monday = this_monday - timedelta(days=7)
    last_saturday = last_monday + timedelta(days=5)

    # --- Pulls ---
    cash_block = _cash_position(as_of, flags)
    cash = cash_block["cash"]

    revenue_block = _revenue_last_week(last_monday, last_saturday, flags)
    ar_block = _ar_snapshot(as_of, flags)
    ap_block = _ap_pacing(as_of, cash, flags)
    margin_block = _margin_signal(last_monday, last_saturday, flags)
    backlog_block = _backlog_hint(flags)
    conv_block = _cash_conversion(as_of, flags)

    # --- WoW deltas from history ---
    history = _load_history()
    prior = _prior_snapshot(history)
    if prior:
        prior_cash = prior.get("stats", {}).get("cash", {}).get("cash")
        prior_rev = prior.get("stats", {}).get("revenue", {}).get("revenue")
        cash_block["prev_cash"] = prior_cash
        revenue_block["prev_revenue"] = prior_rev

        # crude 7-day avg = (current + prior) / 2 until we have daily cash snapshots
        if cash is not None and prior_cash is not None:
            cash_block["avg_7d"] = (cash + prior_cash) / 2

    stats: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "week_window": f"{last_monday.isoformat()}..{last_saturday.isoformat()}",
        "cash": cash_block,
        "revenue": revenue_block,
        "ar": ar_block,
        "ap": ap_block,
        "margin": margin_block,
        "backlog": backlog_block,
        "cash_conv": conv_block,
    }

    stamp = now.strftime(f"%Y-%m-%d %H:%M {AZ_TZ_LABEL}")
    josh_msg = _compose_josh_digest(stats, flags, stamp)
    matt_msg = _compose_matt_full(stats, flags, stamp)

    # Persist this week's snapshot for next week's deltas
    _save_snapshot({"run_at": now.isoformat(), "stats": stats, "flags": flags})

    return {
        "josh_message": josh_msg,
        "matt_message": matt_msg,
        "stats": stats,
        "flags": flags,
    }
