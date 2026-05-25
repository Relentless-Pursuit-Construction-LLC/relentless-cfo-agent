"""The Counsel — monthly walkthrough.

Runs the 5th of each new month (covers prior month). Heaviest report — 1500-2500
word deep walkthrough delivered to Matt + Josh, eight sections:

  1. Headline
  2. P&L walkthrough (current month vs prior month, vs same month prior year)
  3. Cash flow (opening vs closing, biggest inflows / outflows)
  4. 13-week forward cash flow forecast (with confidence bands)
  5. AR portfolio health (concentration, aging, top 5 customers)
  6. Industry benchmark comparison (NAHB / Tommy Mello / ServiceTitan)
  7. What changed and why (top 3 month-over-month deltas + causes)
  8. Coaching: 3 priorities for next month (specific + accountable)

Sources (read-only):
  - qbo.get_profit_loss(start, end)
  - qbo.get_balance_sheet(as_of)
  - qbo.get_ar_aging_summary(as_of)
  - qbo.get_ar_aging_detail(as_of)
  - qbo.get_open_invoices()

History (write OK):
  - /data/counsel_history.json — snapshots of each monthly run for MoM and YoY
    comparison going forward.

This module does not send Slack / email. Caller owns delivery. Returns the full
report text in the dict.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from keystone import qbo

# --- Configuration ---------------------------------------------------------

STATE_DIR = os.environ.get("STATE_DIR", "/data")
COUNSEL_HISTORY_PATH = f"{STATE_DIR}/counsel_history.json"

STALE_DATA_HOURS = 48
FORECAST_WEEKS = 13
SLOW_PAY_LAG_DAYS = 14         # offset for "slow" customers in forecast
RECURRING_OUTFLOW_WINDOW_DAYS = 56   # last 8 weeks for outflow patterning

# Industry benchmarks (mirror of KEYSTONE_KNOWLEDGE_INDEX.md)
BENCH_WINDOW_GROSS_MARGIN = (0.35, 0.45)
BENCH_ROOFING_GROSS_MARGIN = (0.25, 0.35)
BENCH_LABOR_PCT = (0.18, 0.25)         # window install
BENCH_NET_MARGIN_HEALTHY = (0.08, 0.15)
NET_MARGIN_FLOOR = 0.05                # below 5% = something is broken
FRANK_BLAU_NET_FLOOR = 0.20            # 20% minimum aspirational line

AZ_TZ_OFFSET = timezone(timedelta(hours=-7))


# --- History persistence ---------------------------------------------------


def _load_history() -> dict[str, Any]:
    """Load counsel history. Returns empty shell on first run."""
    path = Path(COUNSEL_HISTORY_PATH)
    if not path.exists():
        return {"months": []}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if "months" not in data or not isinstance(data["months"], list):
            return {"months": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"months": []}


def _save_history(history: dict[str, Any]) -> None:
    """Atomic write. Keeps last 36 months."""
    history["months"] = history["months"][-36:]
    path = Path(COUNSEL_HISTORY_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(history, f, indent=2)
    tmp_path.replace(path)


def _prior_month_snapshot(history: dict[str, Any], for_month: str) -> dict[str, Any] | None:
    months = [m for m in history["months"] if m.get("month") and m["month"] < for_month]
    if not months:
        return None
    return max(months, key=lambda m: m["month"])


def _same_month_prior_year(history: dict[str, Any], for_month: str) -> dict[str, Any] | None:
    """for_month is 'YYYY-MM'. Returns snapshot for (YYYY-1)-MM."""
    try:
        y, m = for_month.split("-")
        target = f"{int(y) - 1}-{m}"
    except (ValueError, AttributeError):
        return None
    for snap in history["months"]:
        if snap.get("month") == target:
            return snap
    return None


# --- Date helpers ----------------------------------------------------------


def _prev_month_str(today: date) -> str:
    """Return prior month as 'YYYY-MM'."""
    first_of_this = today.replace(day=1)
    last_of_prev = first_of_this - timedelta(days=1)
    return last_of_prev.strftime("%Y-%m")


def _month_bounds(month_str: str) -> tuple[str, str]:
    """Return (start_date, end_date) inclusive for a 'YYYY-MM' month."""
    y, m = month_str.split("-")
    start = date(int(y), int(m), 1)
    if int(m) == 12:
        end = date(int(y), 12, 31)
    else:
        end = date(int(y), int(m) + 1, 1) - timedelta(days=1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


# --- QBO report parsing ----------------------------------------------------


def _parse_money(s: Any) -> float:
    """Parse a QBO money value. Handles '', strings, numbers, '(123.45)'."""
    if s is None or s == "":
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().replace(",", "").replace("$", "")
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return -v if neg else v


def _walk_pl_rows(rows_obj: Any, out: list[dict[str, Any]], section: str = "") -> None:
    """Walk a P&L report, collecting (section, name, amount) tuples for leaf rows.

    P&L sections we care about: Income, Cost of Goods Sold, Gross Profit (summary),
    Expenses, Net Operating Income (summary), Net Income (summary).
    """
    if not isinstance(rows_obj, dict):
        return
    for row in rows_obj.get("Row", []) or []:
        if not isinstance(row, dict):
            continue
        rtype = row.get("type")
        if rtype == "Section":
            header = row.get("Header", {})
            cd = header.get("ColData", []) if isinstance(header, dict) else []
            sect_label = cd[0].get("value", "") if cd and isinstance(cd[0], dict) else section
            nested = row.get("Rows")
            if nested:
                _walk_pl_rows(nested, out, sect_label or section)
            # Also capture section summary line (totals)
            summary = row.get("Summary")
            if isinstance(summary, dict):
                scd = summary.get("ColData", [])
                if isinstance(scd, list) and len(scd) >= 2:
                    out.append({
                        "section": section,
                        "kind": "section_total",
                        "name": scd[0].get("value", "") if isinstance(scd[0], dict) else "",
                        "amount": _parse_money(scd[-1].get("value", "") if isinstance(scd[-1], dict) else ""),
                    })
        elif rtype == "Data":
            cd = row.get("ColData", [])
            if isinstance(cd, list) and len(cd) >= 2:
                out.append({
                    "section": section,
                    "kind": "line",
                    "name": cd[0].get("value", "") if isinstance(cd[0], dict) else "",
                    "amount": _parse_money(cd[-1].get("value", "") if isinstance(cd[-1], dict) else ""),
                })


def _summarize_pl(pl_report: dict[str, Any]) -> dict[str, Any]:
    """Reduce a P&L report to the numbers Keystone needs.

    Returns:
      {
        "revenue": float,
        "cogs": float,
        "gross_profit": float,
        "gross_margin_pct": float,
        "operating_expenses": float,
        "net_income": float,
        "net_margin_pct": float,
        "expense_lines": [{"name": str, "amount": float}, ...] sorted desc,
        "cogs_lines": [...],
        "revenue_lines": [...],
      }
    """
    rows = pl_report.get("Rows", {})
    parsed: list[dict[str, Any]] = []
    _walk_pl_rows(rows, parsed)

    revenue = 0.0
    cogs = 0.0
    operating_expenses = 0.0
    net_income = 0.0
    revenue_lines: list[dict[str, Any]] = []
    cogs_lines: list[dict[str, Any]] = []
    expense_lines: list[dict[str, Any]] = []

    for item in parsed:
        sect = (item["section"] or "").lower()
        name_lower = item["name"].lower()
        if item["kind"] == "section_total":
            if "total income" in name_lower or name_lower == "total revenue":
                revenue = item["amount"]
            elif "total cost of goods sold" in name_lower or "total cogs" in name_lower:
                cogs = item["amount"]
            elif "total expenses" in name_lower:
                operating_expenses = item["amount"]
            elif "net income" == name_lower or name_lower.endswith("net income"):
                net_income = item["amount"]
        else:  # line
            if "income" in sect and "cost" not in sect:
                revenue_lines.append({"name": item["name"], "amount": item["amount"]})
            elif "cost of goods sold" in sect or "cogs" in sect:
                cogs_lines.append({"name": item["name"], "amount": item["amount"]})
            elif "expense" in sect:
                expense_lines.append({"name": item["name"], "amount": item["amount"]})

    # Fallbacks if QBO didn't emit the section totals we expected
    if revenue == 0.0 and revenue_lines:
        revenue = sum(x["amount"] for x in revenue_lines)
    if cogs == 0.0 and cogs_lines:
        cogs = sum(x["amount"] for x in cogs_lines)
    if operating_expenses == 0.0 and expense_lines:
        operating_expenses = sum(x["amount"] for x in expense_lines)
    if net_income == 0.0:
        net_income = revenue - cogs - operating_expenses

    gross_profit = revenue - cogs
    gross_margin_pct = (gross_profit / revenue * 100.0) if revenue else 0.0
    net_margin_pct = (net_income / revenue * 100.0) if revenue else 0.0

    revenue_lines.sort(key=lambda x: x["amount"], reverse=True)
    cogs_lines.sort(key=lambda x: x["amount"], reverse=True)
    expense_lines.sort(key=lambda x: x["amount"], reverse=True)

    return {
        "revenue": revenue,
        "cogs": cogs,
        "gross_profit": gross_profit,
        "gross_margin_pct": gross_margin_pct,
        "operating_expenses": operating_expenses,
        "net_income": net_income,
        "net_margin_pct": net_margin_pct,
        "expense_lines": expense_lines,
        "cogs_lines": cogs_lines,
        "revenue_lines": revenue_lines,
    }


# --- Balance sheet / cash --------------------------------------------------


def _walk_bs_rows(rows_obj: Any, out: list[dict[str, Any]]) -> None:
    """Walk BalanceSheet rows collecting all leaves (mirrors pulse pattern)."""
    if not isinstance(rows_obj, dict):
        return
    for row in rows_obj.get("Row", []) or []:
        if not isinstance(row, dict):
            continue
        rtype = row.get("type")
        if rtype == "Data":
            cd = row.get("ColData", [])
            if isinstance(cd, list) and len(cd) >= 2:
                out.append({
                    "name": cd[0].get("value", "") if isinstance(cd[0], dict) else "",
                    "value": _parse_money(cd[-1].get("value", "") if isinstance(cd[-1], dict) else ""),
                    "group": row.get("group", ""),
                })
        nested = row.get("Rows")
        if nested:
            _walk_bs_rows(nested, out)


def _find_section(rows_obj: Any, label_match: tuple[str, ...]) -> dict[str, Any] | None:
    if not isinstance(rows_obj, dict):
        return None
    for row in rows_obj.get("Row", []) or []:
        if not isinstance(row, dict):
            continue
        if row.get("type") == "Section":
            header = row.get("Header", {})
            cd = header.get("ColData", []) if isinstance(header, dict) else []
            label = (cd[0].get("value", "") if cd and isinstance(cd[0], dict) else "").strip().lower()
            if any(label == m or label.startswith(m) for m in label_match):
                return row
            nested = row.get("Rows")
            found = _find_section(nested, label_match) if nested else None
            if found:
                return found
    return None


def _extract_cash_from_bs(bs: dict[str, Any]) -> float:
    """Sum bank account balances from a BalanceSheet report."""
    rows = bs.get("Rows", {})
    section = _find_section(rows, ("bank accounts", "checking", "checking/savings"))
    if section is None:
        return 0.0
    leaves: list[dict[str, Any]] = []
    _walk_bs_rows(section.get("Rows", {}), leaves)
    return sum(l["value"] for l in leaves)


# --- AR parsing ------------------------------------------------------------


def _summarize_ar_summary(ar_summary: dict[str, Any]) -> dict[str, Any]:
    """Pull aging buckets out of an AgedReceivables summary report.

    Columns are typically: Customer | Current | 1-30 | 31-60 | 61-90 | 91+ | Total
    The summary report's TOTAL row gives bucket totals across all customers.
    """
    rows_obj = ar_summary.get("Rows", {})
    total_row: list[float] = []

    # Find the grand-total summary row at the report root.
    for row in rows_obj.get("Row", []) or []:
        if not isinstance(row, dict):
            continue
        if row.get("type") == "Section":
            summary = row.get("Summary")
            if isinstance(summary, dict):
                cd = summary.get("ColData", [])
                if isinstance(cd, list) and len(cd) >= 2:
                    total_row = [_parse_money(c.get("value", "")) for c in cd[1:]]
        elif row.get("group") == "GrandTotal" or row.get("type") == "Summary":
            cd = row.get("Summary", {}).get("ColData") if "Summary" in row else row.get("ColData", [])
            if isinstance(cd, list) and len(cd) >= 2:
                total_row = [_parse_money(c.get("value", "")) for c in cd[1:]]

    # If we still don't have it, try walking all rows and finding one labeled TOTAL.
    if not total_row:
        leaves: list[dict[str, Any]] = []
        _walk_bs_rows(rows_obj, leaves)
        for leaf in leaves:
            if "total" in leaf["name"].lower():
                # Single value — not useful for buckets. Skip.
                pass

    # Map columns to buckets. Columns vary; default labels:
    #   [Current, 1-30, 31-60, 61-90, 91+, Total]
    buckets = {"current": 0.0, "1_30": 0.0, "31_60": 0.0, "61_90": 0.0, "91_plus": 0.0, "total": 0.0}
    if len(total_row) >= 6:
        buckets["current"] = total_row[0]
        buckets["1_30"] = total_row[1]
        buckets["31_60"] = total_row[2]
        buckets["61_90"] = total_row[3]
        buckets["91_plus"] = total_row[4]
        buckets["total"] = total_row[5]
    elif len(total_row) >= 1:
        buckets["total"] = total_row[-1]
    return buckets


def _summarize_ar_detail(ar_detail: dict[str, Any]) -> dict[str, Any]:
    """Walk AR detail to compute top 5 customers and concentration.

    AgedReceivableDetail row ColData layout (typical):
      [Date, TxnType, Num, Customer, DueDate, Past Due Days, Amount, Open Balance]
    """
    rows_obj = ar_detail.get("Rows", {})
    by_customer: dict[str, float] = defaultdict(float)
    open_invoices: list[dict[str, Any]] = []

    def _walk(ro: Any) -> None:
        if not isinstance(ro, dict):
            return
        for r in ro.get("Row", []) or []:
            if not isinstance(r, dict):
                continue
            if r.get("type") == "Data":
                cd = r.get("ColData", [])
                if not isinstance(cd, list) or len(cd) < 4:
                    continue
                # Try to extract customer + open balance defensively
                vals = [c.get("value", "") if isinstance(c, dict) else "" for c in cd]
                # Open balance is typically last numeric column
                open_bal = _parse_money(vals[-1])
                # Customer name — search for a non-empty string that's not a date / num
                customer = ""
                for v in vals[3:6]:
                    if v and not v[:4].isdigit():
                        customer = v
                        break
                if not customer and len(vals) >= 4:
                    customer = vals[3]
                if customer:
                    by_customer[customer] += open_bal
                    open_invoices.append({
                        "customer": customer,
                        "open_balance": open_bal,
                        "due_date": vals[4] if len(vals) > 4 else "",
                    })
            nested = r.get("Rows")
            if nested:
                _walk(nested)

    _walk(rows_obj)

    total = sum(by_customer.values())
    top = sorted(by_customer.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top_5 = [{"customer": k, "balance": v, "pct_of_ar": (v / total * 100.0) if total else 0.0} for k, v in top]
    top_5_concentration = sum(x["pct_of_ar"] for x in top_5)

    return {
        "total_ar": total,
        "customer_count": len(by_customer),
        "top_5": top_5,
        "top_5_concentration_pct": top_5_concentration,
        "open_invoices": open_invoices,
    }


# --- 13-week forecast ------------------------------------------------------


def _parse_qbo_date(s: str) -> date | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:10], fmt[:10] if "T" not in fmt else fmt[:10]).date()
        except ValueError:
            continue
    return None


def _classify_customer_speed(ar_detail_summary: dict[str, Any]) -> dict[str, str]:
    """Heuristic: a customer is 'slow' if they have any open balance aged 31+ days.

    Returns {customer_name: 'good' | 'slow'}.
    """
    speed: dict[str, str] = {}
    # Re-walk open invoices: any line with due_date older than today by 30+ days
    today = date.today()
    by_cust_aged: dict[str, bool] = defaultdict(bool)
    for inv in ar_detail_summary.get("open_invoices", []):
        due = _parse_qbo_date(inv.get("due_date", ""))
        if due is not None and (today - due).days >= 30 and inv.get("open_balance", 0) > 0:
            by_cust_aged[inv["customer"]] = True
    for cust in {i["customer"] for i in ar_detail_summary.get("open_invoices", [])}:
        speed[cust] = "slow" if by_cust_aged.get(cust) else "good"
    return speed


def _project_ar_inflows(
    open_invoices_query: dict[str, Any],
    ar_detail_summary: dict[str, Any],
    today: date,
    weeks: int = FORECAST_WEEKS,
) -> list[float]:
    """Project weekly AR cash inflows for the next `weeks` weeks.

    Method:
      - Pull every open invoice with a DueDate.
      - 'good' customer: assume payment on DueDate.
      - 'slow' customer: assume payment DueDate + SLOW_PAY_LAG_DAYS.
      - Invoice already past due: collect within the first 2 weeks (split 50/50).
    """
    speed_map = _classify_customer_speed(ar_detail_summary)
    weekly = [0.0] * weeks

    qr = open_invoices_query.get("QueryResponse", {})
    invoices = qr.get("Invoice", []) or []

    week_start = today  # week 0 starts today

    for inv in invoices:
        balance = float(inv.get("Balance", 0) or 0)
        if balance <= 0:
            continue
        due_str = inv.get("DueDate") or inv.get("TxnDate")
        due = _parse_qbo_date(due_str or "")
        if due is None:
            continue

        # Customer name lookup
        cust_ref = inv.get("CustomerRef", {})
        cust = cust_ref.get("name", "") if isinstance(cust_ref, dict) else ""
        is_slow = speed_map.get(cust) == "slow"

        if due < today:
            # Past due — split across first two weeks (50/50). Slow customers skew later.
            if is_slow:
                if weeks >= 2:
                    weekly[0] += balance * 0.25
                    weekly[1] += balance * 0.75
                else:
                    weekly[0] += balance
            else:
                if weeks >= 2:
                    weekly[0] += balance * 0.5
                    weekly[1] += balance * 0.5
                else:
                    weekly[0] += balance
            continue

        pay_date = due + timedelta(days=SLOW_PAY_LAG_DAYS) if is_slow else due
        week_idx = (pay_date - week_start).days // 7
        if 0 <= week_idx < weeks:
            weekly[week_idx] += balance

    return weekly


def _project_outflows(pl_summary: dict[str, Any], weeks: int = FORECAST_WEEKS) -> tuple[list[float], dict[str, Any]]:
    """Project weekly cash outflows from the prior month's expense + COGS pattern.

    v1 approach (be honest about confidence):
      - Take prior month's total cash outflows (COGS + operating expenses).
      - Divide by ~4.33 (weeks in a month) to get a weekly run-rate.
      - Apply that flat across the forecast window.

    Confidence is medium at best — we don't have transaction-level recurring
    pattern detection in v1. Flag this clearly in the report.
    """
    monthly_outflow = pl_summary["cogs"] + pl_summary["operating_expenses"]
    weekly_run_rate = monthly_outflow / 4.33 if monthly_outflow else 0.0
    weekly = [weekly_run_rate] * weeks
    detail = {
        "monthly_outflow_basis": monthly_outflow,
        "weekly_run_rate": weekly_run_rate,
        "method": "flat run-rate from prior month total COGS + operating expenses",
    }
    return weekly, detail


def _build_forecast(
    starting_cash: float,
    weekly_inflows: list[float],
    weekly_outflows: list[float],
    today: date,
) -> dict[str, Any]:
    """Roll forward weekly cash projections."""
    weeks_out = []
    cash = starting_cash
    for i, (inflow, outflow) in enumerate(zip(weekly_inflows, weekly_outflows)):
        net = inflow - outflow
        cash += net
        week_start = today + timedelta(days=i * 7)
        weeks_out.append({
            "week": i + 1,
            "week_start": week_start.strftime("%Y-%m-%d"),
            "inflow": round(inflow, 2),
            "outflow": round(outflow, 2),
            "net": round(net, 2),
            "ending_cash": round(cash, 2),
        })

    lowest = min(weeks_out, key=lambda w: w["ending_cash"]) if weeks_out else None
    ending = weeks_out[-1] if weeks_out else None
    return {
        "starting_cash": starting_cash,
        "weeks": weeks_out,
        "lowest_week": lowest,
        "ending_position": ending,
    }


def _forecast_confidence(history: dict[str, Any]) -> str:
    """Confidence band — low if first run, medium if 1-2 months history, high if 3+."""
    n = len(history.get("months", []))
    if n >= 3:
        return "medium-high"
    if n >= 1:
        return "medium"
    return "low"


# --- Freshness gate --------------------------------------------------------


def _check_freshness(balance_sheet: dict[str, Any], now_utc: datetime) -> dict[str, Any]:
    header = balance_sheet.get("Header", {})
    report_time = header.get("Time") or header.get("EndPeriod")
    if not report_time:
        return {"stale": True, "as_of": None, "hours_old": None,
                "reason": "QBO BalanceSheet returned no timestamp"}
    parsed: datetime | None = None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(report_time[:25], fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return {"stale": True, "as_of": report_time, "hours_old": None,
                "reason": f"Could not parse QBO timestamp '{report_time}'"}
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age_hours = (now_utc - parsed).total_seconds() / 3600.0
    return {
        "stale": age_hours > STALE_DATA_HOURS,
        "as_of": report_time,
        "hours_old": round(age_hours, 1),
        "reason": "",
    }


# --- Voice / formatters ----------------------------------------------------


def _fmt_dollars(v: float) -> str:
    return f"${v:,.0f}"


def _fmt_dollars_signed(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):,.0f}"


def _fmt_pct(v: float, decimals: int = 1) -> str:
    return f"{v:.{decimals}f}%"


def _delta(curr: float, prior: float | None) -> str:
    if prior is None:
        return "no prior period to compare"
    diff = curr - prior
    pct = (diff / prior * 100.0) if prior else 0.0
    return f"{_fmt_dollars_signed(diff)} ({pct:+.1f}%)"


def _benchmark_verdict(actual_pct: float, lo: float, hi: float) -> str:
    """Return 'below', 'in-range', or 'above' relative to a benchmark band (decimals)."""
    actual_dec = actual_pct / 100.0
    if actual_dec < lo:
        return "below"
    if actual_dec > hi:
        return "above"
    return "in-range"


# --- Report builder --------------------------------------------------------


def _build_report(
    month: str,
    pl: dict[str, Any],
    prior_pl: dict[str, Any] | None,
    yoy_pl: dict[str, Any] | None,
    cash_open: float,
    cash_close: float,
    ar: dict[str, Any],
    ar_buckets: dict[str, float],
    forecast: dict[str, Any],
    forecast_outflow_detail: dict[str, Any],
    confidence: str,
    pull_ts_az: str,
    priorities: list[dict[str, str]],
) -> str:
    """Hand-built monthly walkthrough. ~1500-2500 words.

    Two-voice section: plain English to Josh in the body, accountant-grade
    footnote to Matt at the end of each major section where the framework
    matters.
    """
    revenue = pl["revenue"]
    cogs = pl["cogs"]
    gp = pl["gross_profit"]
    gm_pct = pl["gross_margin_pct"]
    opex = pl["operating_expenses"]
    ni = pl["net_income"]
    nm_pct = pl["net_margin_pct"]

    cash_delta = cash_close - cash_open
    cash_dir = "up" if cash_delta > 0 else ("down" if cash_delta < 0 else "flat")

    # Section 1 — Headline
    headline = (
        f"Revenue {_fmt_dollars(revenue)}, gross margin {_fmt_pct(gm_pct)}, "
        f"net {_fmt_dollars(ni)} ({_fmt_pct(nm_pct)}), cash {cash_dir} "
        f"{_fmt_dollars(abs(cash_delta))} to {_fmt_dollars(cash_close)}."
    )

    # Section 2 — P&L walkthrough
    pl_lines = [
        "## 1. Headline",
        "",
        headline,
        "",
        "## 2. P&L walkthrough",
        "",
        f"**Top line.** Invoiced revenue for the month was {_fmt_dollars(revenue)}. "
        f"vs prior month: {_delta(revenue, prior_pl['revenue'] if prior_pl else None)}. "
        f"vs same month prior year: {_delta(revenue, yoy_pl['revenue'] if yoy_pl else None)}.",
        "",
    ]

    if pl["revenue_lines"]:
        top_rev = pl["revenue_lines"][:5]
        pl_lines.append("Revenue by line:")
        for r in top_rev:
            share = (r["amount"] / revenue * 100.0) if revenue else 0
            pl_lines.append(f"- {r['name']}: {_fmt_dollars(r['amount'])} ({_fmt_pct(share)})")
        pl_lines.append("")

    pl_lines.extend([
        f"**Cost of goods sold.** {_fmt_dollars(cogs)} "
        f"({_fmt_pct((cogs / revenue * 100.0) if revenue else 0)} of revenue). "
        f"vs prior month: {_delta(cogs, prior_pl['cogs'] if prior_pl else None)}.",
        "",
    ])
    if pl["cogs_lines"]:
        pl_lines.append("Biggest COGS lines:")
        for c in pl["cogs_lines"][:5]:
            pl_lines.append(f"- {c['name']}: {_fmt_dollars(c['amount'])}")
        pl_lines.append("")

    pl_lines.extend([
        f"**Gross profit.** {_fmt_dollars(gp)} on {_fmt_dollars(revenue)} = "
        f"{_fmt_pct(gm_pct)} gross margin. This is the dollar that's left after "
        f"the install is paid for, before any office overhead.",
        "",
        f"**Operating expenses.** {_fmt_dollars(opex)} "
        f"({_fmt_pct((opex / revenue * 100.0) if revenue else 0)} of revenue). "
        f"vs prior month: {_delta(opex, prior_pl['operating_expenses'] if prior_pl else None)}.",
        "",
    ])
    if pl["expense_lines"]:
        pl_lines.append("Top operating expenses:")
        for e in pl["expense_lines"][:6]:
            pl_lines.append(f"- {e['name']}: {_fmt_dollars(e['amount'])}")
        pl_lines.append("")

    pl_lines.extend([
        f"**Net income.** {_fmt_dollars(ni)} = {_fmt_pct(nm_pct)} net margin. "
        f"vs prior month: {_delta(ni, prior_pl['net_income'] if prior_pl else None)}. "
        f"vs same month prior year: {_delta(ni, yoy_pl['net_income'] if yoy_pl else None)}.",
        "",
        "*Note to Matt: P&L shown on accrual basis per QBO defaults. Net margin "
        "calc is operating-level (Net Operating Income / Total Income); below-the-line "
        "items would shift the figure if material.*",
        "",
    ])

    # Section 3 — Cash flow
    cash_section = [
        "## 3. Cash flow",
        "",
        f"**Opening cash (first of month):** {_fmt_dollars(cash_open)}.",
        f"**Closing cash (last of month):** {_fmt_dollars(cash_close)}.",
        f"**Net change:** {_fmt_dollars_signed(cash_delta)}.",
        "",
        f"Revenue invoiced was {_fmt_dollars(revenue)}, total cash costs (COGS + opex) "
        f"were {_fmt_dollars(cogs + opex)}. The gap between net income "
        f"({_fmt_dollars(ni)}) and actual cash movement ({_fmt_dollars_signed(cash_delta)}) "
        f"is timing — invoices that haven't collected yet, deposits taken on jobs "
        f"not yet booked as revenue, and any draws or financing moves.",
        "",
        "*Note to Matt: full SoCF treatment requires direct method reconciliation "
        "(operating / investing / financing). Recommend a true cash flow statement "
        "from QBO if you want to tie the $ change in cash to net income through "
        "working capital deltas. Numbers above are the period-end snapshot from "
        "the BalanceSheet bank section.*",
        "",
    ]

    # Section 4 — 13-week forecast
    forecast_lines = [
        "## 4. 13-week forward cash flow forecast",
        "",
        f"Starting cash: {_fmt_dollars(forecast['starting_cash'])}. "
        f"Confidence band: **{confidence}**.",
        "",
        "Method:",
        "- Inflows: every open invoice projected to collect on its due date for "
        "customers paying on time, or due date + 14 days for customers with any "
        "balance aged 31+ days.",
        "- Past-due invoices: split across the first two weeks (50/50 for good "
        "payers, 25/75 for slow payers).",
        f"- Outflows: flat weekly run-rate of {_fmt_dollars(forecast_outflow_detail['weekly_run_rate'])} "
        "derived from prior month's total COGS + operating expenses divided by 4.33. "
        "This does not yet model payroll cadence, scheduled bill payments, or "
        "tax accruals discretely.",
        "",
        "**Assumptions are explicit and conservative.** This is a v1 projection. "
        "Treat as directional, not as a commitment.",
        "",
        "| Wk | Starts | Inflow | Outflow | Net | Ending cash |",
        "|---|---|---|---|---|---|",
    ]
    for w in forecast["weeks"]:
        forecast_lines.append(
            f"| {w['week']} | {w['week_start']} | {_fmt_dollars(w['inflow'])} | "
            f"{_fmt_dollars(w['outflow'])} | {_fmt_dollars_signed(w['net'])} | "
            f"{_fmt_dollars(w['ending_cash'])} |"
        )

    lowest = forecast["lowest_week"]
    ending = forecast["ending_position"]
    if lowest and ending:
        forecast_lines.extend([
            "",
            f"**Lowest projected cash:** {_fmt_dollars(lowest['ending_cash'])} "
            f"in week {lowest['week']} (starts {lowest['week_start']}).",
            f"**Projected ending cash (week 13):** {_fmt_dollars(ending['ending_cash'])}.",
            "",
            "*Note to Matt: this is essentially a direct-method 13-week with AR "
            "collections as the only modeled inflow and prior-period operating "
            "burn as the outflow proxy. Confidence will tighten in Phase 2 when "
            "we add payroll calendar, AP scheduled-payment dates, and tax "
            "accruals as discrete line items.*",
            "",
        ])

    # Section 5 — AR portfolio
    total_ar = ar.get("total_ar", 0.0) or ar_buckets.get("total", 0.0)
    ar_section = [
        "## 5. AR portfolio health",
        "",
        f"**Total open AR:** {_fmt_dollars(total_ar)} across {ar.get('customer_count', 0)} customers.",
        "",
        "Aging buckets:",
        f"- Current: {_fmt_dollars(ar_buckets['current'])} ({_fmt_pct((ar_buckets['current']/total_ar*100) if total_ar else 0)})",
        f"- 1-30 days: {_fmt_dollars(ar_buckets['1_30'])} ({_fmt_pct((ar_buckets['1_30']/total_ar*100) if total_ar else 0)})",
        f"- 31-60 days: {_fmt_dollars(ar_buckets['31_60'])} ({_fmt_pct((ar_buckets['31_60']/total_ar*100) if total_ar else 0)})",
        f"- 61-90 days: {_fmt_dollars(ar_buckets['61_90'])} ({_fmt_pct((ar_buckets['61_90']/total_ar*100) if total_ar else 0)})",
        f"- 91+ days: {_fmt_dollars(ar_buckets['91_plus'])} ({_fmt_pct((ar_buckets['91_plus']/total_ar*100) if total_ar else 0)})",
        "",
        f"**Top 5 customers ({_fmt_pct(ar.get('top_5_concentration_pct', 0))} of total AR):**",
    ]
    for i, t in enumerate(ar.get("top_5", []), start=1):
        ar_section.append(
            f"{i}. {t['customer']}: {_fmt_dollars(t['balance'])} "
            f"({_fmt_pct(t['pct_of_ar'])})"
        )
    ar_section.extend([
        "",
        "*Note to Matt: 'DSO' = days customers take to pay us. With 50%-deposit-"
        "at-signing cash model, healthy DSO sits under 7 days for cash deals and "
        "under 30 days for progress-billed work. Concentration above 40% in top 5 "
        "is a risk-management flag, not necessarily a collection problem.*",
        "",
    ])

    # Section 6 — Benchmarks
    bench_section = [
        "## 6. Industry benchmark comparison",
        "",
        "Benchmarks are window-install primary (NAHB / Brady-JKR data) with "
        "Tommy Mello / ServiceTitan home-services overlays.",
        "",
    ]
    gm_verdict = _benchmark_verdict(gm_pct, *BENCH_WINDOW_GROSS_MARGIN)
    nm_verdict = _benchmark_verdict(nm_pct, *BENCH_NET_MARGIN_HEALTHY)
    bench_section.extend([
        f"**Gross margin:** {_fmt_pct(gm_pct)} vs window benchmark 35-45%. "
        f"Verdict: **{gm_verdict}**.",
    ])
    if gm_verdict == "below":
        bench_section.append(
            "Below-band gross margin on a window install business means one of "
            "three things is eating the dollar: materials cost (paid too much, or "
            "wrong supplier), labor cost (crew slow or overstaffed), or discount "
            "discipline (closers cutting price to land deals). The COGS line "
            "detail above tells us which."
        )
    bench_section.extend([
        "",
        f"**Net margin:** {_fmt_pct(nm_pct)} vs healthy 8-15%. Verdict: **{nm_verdict}**. "
        f"Hard floor is 5% (below = something is broken). Aspirational "
        f"line is 20% net (Frank Blau's contractor floor).",
        "",
        "*Note to Matt: industry comparison shown on operating-margin basis. "
        "Below-band signals are flagged for cause; full variance attribution "
        "between materials %, labor %, sub %, and discount % requires job-cost "
        "drill-through which Phase 2 will surface.*",
        "",
    ])

    # Section 7 — What changed
    deltas_section = ["## 7. What changed and why", ""]
    if prior_pl is None:
        deltas_section.extend([
            "No prior-month snapshot in Keystone's history yet — this is the first "
            "Counsel run. Month-over-month deltas will start next month.",
            "",
        ])
    else:
        rev_delta = revenue - prior_pl["revenue"]
        gm_delta = gm_pct - prior_pl["gross_margin_pct"]
        ni_delta = ni - prior_pl["net_income"]
        deltas_section.extend([
            f"**1. Revenue:** {_fmt_dollars_signed(rev_delta)} vs last month. "
            f"Likely driver: appointment volume and close rate — verify against "
            f"Terros funnel data for the month.",
            "",
            f"**2. Gross margin:** {gm_delta:+.1f} pts ({_fmt_pct(prior_pl['gross_margin_pct'])} -> "
            f"{_fmt_pct(gm_pct)}). Likely driver: mix shift between Cam (AZ) and Tegan "
            f"(UT) markets, or per-deal discounting trend. Job-cost variance review "
            f"recommended.",
            "",
            f"**3. Net income:** {_fmt_dollars_signed(ni_delta)}. Driven primarily by "
            f"the revenue and gross-margin moves above plus opex pacing of "
            f"{_fmt_dollars_signed(opex - prior_pl['operating_expenses'])}.",
            "",
        ])

    # Section 8 — Coaching priorities
    coaching_section = ["## 8. Coaching: 3 priorities for next month", ""]
    for i, p in enumerate(priorities, start=1):
        coaching_section.extend([
            f"**Priority {i} — {p['title']}**",
            "",
            p["body"],
            "",
            f"*To Matt: {p['matt_note']}*",
            "",
        ])

    # Stitch it together
    sections = [
        f"# The Counsel — {month}",
        "",
        f"Prepared by Keystone for Josh Holland and Matt. Data pull timestamp at "
        f"the end of the document.",
        "",
    ] + pl_lines + cash_section + forecast_lines + ar_section + bench_section + deltas_section + coaching_section

    sections.extend([
        "---",
        "",
        "— Keystone",
        f"Data pulled: {pull_ts_az} AZ",
    ])

    return "\n".join(sections)


# --- Coaching priorities (data-driven) ------------------------------------


def _derive_priorities(
    pl: dict[str, Any],
    prior_pl: dict[str, Any] | None,
    ar: dict[str, Any],
    ar_buckets: dict[str, float],
    forecast: dict[str, Any],
) -> list[dict[str, str]]:
    """Generate up to 3 specific coaching priorities grounded in this month's data.

    Returns dicts: {"title": ..., "body": ..., "matt_note": ...}
    """
    priorities: list[dict[str, str]] = []
    gm_pct = pl["gross_margin_pct"]
    nm_pct = pl["net_margin_pct"]
    total_ar = ar.get("total_ar", 0.0) or ar_buckets.get("total", 0.0)
    aged_60_plus = ar_buckets.get("61_90", 0.0) + ar_buckets.get("91_plus", 0.0)
    aged_60_pct = (aged_60_plus / total_ar * 100.0) if total_ar else 0.0

    # 1. Margin discipline
    if gm_pct < BENCH_WINDOW_GROSS_MARGIN[0] * 100:
        priorities.append({
            "title": f"Hold the line at {int(BENCH_WINDOW_GROSS_MARGIN[0]*100)}% gross margin on every window deal",
            "body": (
                f"Gross margin came in at {_fmt_pct(gm_pct)} — below the 35% floor "
                f"for residential window installs. Next month: no deal goes out "
                f"of either market under a 32% in-house margin without a "
                f"price-exception sign-off (Josh or Matt). Pull the bottom-5 "
                f"deals by sold margin and walk them with Cam and Tegan in "
                f"L10 — what got discounted, why, and whether the deal would "
                f"have closed at list."
            ),
            "matt_note": (
                "Recommend instituting a sold-margin floor check in QBO at "
                "estimate stage. Variance over 3 points between sold margin "
                "and as-built margin flags a job-cost drift problem; under "
                "the floor flags discount discipline. Both reportable from "
                "the Sales by Customer + Class views with class-level COGS tags."
            ),
        })

    # 2. AR aging discipline
    if aged_60_pct > 10 or aged_60_plus > 25000:
        priorities.append({
            "title": "Clear the 60+ day AR bucket before it ages further",
            "body": (
                f"There's {_fmt_dollars(aged_60_plus)} sitting in 61+ day "
                f"buckets — {_fmt_pct(aged_60_pct)} of the receivables book. "
                f"Each week aged past 60 cuts collection probability "
                f"meaningfully. By the 15th of next month, every customer in "
                f"that bucket gets a Joanne-led call or written demand. If "
                f"the top 5 customers carry a disproportionate share of this "
                f"aging, escalate those to Matt directly — concentration plus "
                f"aging is the combination that bites."
            ),
            "matt_note": (
                "DSO is drifting; recommend tightening the cash-deal "
                "deposit-collection-at-signing standard and adding a written "
                "collection-policy step at 30 / 45 / 60 days. ServiceTitan "
                "benchmarks put healthy >90 AR under 2% of total receivables."
            ),
        })

    # 3. Cash runway / forecast
    lowest = forecast.get("lowest_week")
    if lowest and lowest["ending_cash"] < 75000:
        priorities.append({
            "title": "Protect the week-{} cash trough".format(lowest["week"]),
            "body": (
                f"The 13-week projection shows cash dipping to "
                f"{_fmt_dollars(lowest['ending_cash'])} the week of "
                f"{lowest['week_start']}. That is uncomfortably close to the "
                f"$50K operating floor where Keystone escalates. Two moves "
                f"to make now: (1) confirm with Joanne that scheduled Ramp "
                f"bill pays in that window can be sequenced to the back "
                f"half of the month, and (2) push every open invoice with a "
                f"due date in that window for early payment (a 1% pay-now "
                f"discount costs less than the cash gap)."
            ),
            "matt_note": (
                "The forecast is using a flat weekly outflow run-rate, so the "
                "trough estimate is rough — payroll cadence and tax accruals "
                "could shift it. Recommend overlaying a discrete payroll "
                "calendar in Phase 2. For now treat the trough as a band, "
                "not a point estimate."
            ),
        })

    # 4. Fallback / always-on coaching when nothing red flagged
    if not priorities:
        priorities.append({
            "title": "Codify the margin / cash discipline that produced this month",
            "body": (
                f"Numbers came in healthy — gross margin {_fmt_pct(gm_pct)}, "
                f"net margin {_fmt_pct(nm_pct)}. The risk now is regression. "
                f"Write down what worked: pricing posture, supplier discipline, "
                f"crew assignment. Make it the standard that next month has "
                f"to beat, not match."
            ),
            "matt_note": (
                "Recommend snapshotting the current pricing-floor, material-"
                "markup, and labor-burden assumptions as the baseline. Track "
                "drift monthly going forward — regression typically starts "
                "with one discounted deal that becomes the new normal."
            ),
        })

    # Net margin coaching as #3 if we still have room
    if len(priorities) < 3:
        if nm_pct < NET_MARGIN_FLOOR * 100:
            priorities.append({
                "title": f"Net margin under the 5% floor — investigate now",
                "body": (
                    f"Net margin came in at {_fmt_pct(nm_pct)}. Below 5% means "
                    f"something is structurally off — either revenue is too "
                    f"thin to support the overhead, or operating expenses "
                    f"have grown ahead of the top line. Pull the operating "
                    f"expense list and challenge every line over $5K with "
                    f"Matt. No new recurring software, subscription, or "
                    f"headcount until next month's net clears 8%."
                ),
                "matt_note": (
                    "Below 5% net is the threshold I'd treat as a "
                    "going-concern conversation in a mature business; in a "
                    "4-month-old growth-stage company it's a 'are we "
                    "investing or are we leaking' question. Recommend "
                    "splitting opex into 'build' vs 'run' for the next "
                    "review to distinguish."
                ),
            })
        elif nm_pct < BENCH_NET_MARGIN_HEALTHY[0] * 100:
            priorities.append({
                "title": "Lift net margin into the 8-15% healthy band",
                "body": (
                    f"Net margin at {_fmt_pct(nm_pct)} is below the 8% healthy "
                    f"floor for home services. Closing the gap doesn't take "
                    f"heroics — it takes either two more sold deals per week "
                    f"at sold margin, or a 200 bps reduction in operating "
                    f"expense ratio. Pick which lever you want to pull and "
                    f"name an owner for it in next L10."
                ),
                "matt_note": (
                    "Frank Blau line is 20% net minimum aspirational; "
                    "ServiceTitan / Tommy Mello peer cohort runs 15-20% for "
                    "mature operators. The 8% threshold is the 'we are "
                    "actually running a business not a job' line."
                ),
            })

    return priorities[:3]


# --- Main entrypoint -------------------------------------------------------


def run_counsel(month: str | None = None) -> dict[str, Any]:
    """Monthly walkthrough. Returns dict with full_report, headline, stats, priorities.

    `month` is 'YYYY-MM'. Defaults to previous calendar month relative to AZ today.

    Does not send Slack / email. Caller owns delivery.
    """
    now_az = datetime.now(AZ_TZ_OFFSET)
    today_az = now_az.date()
    target_month = month or _prev_month_str(today_az)
    start_str, end_str = _month_bounds(target_month)
    pull_ts_az = now_az.strftime("%Y-%m-%d %H:%M")

    # 1. Sanity / freshness gate — pull a current balance sheet first.
    bs_today = qbo.get_balance_sheet(today_az.strftime("%Y-%m-%d"))
    freshness = _check_freshness(bs_today, datetime.now(timezone.utc))
    if freshness["stale"]:
        halt_text = (
            f"Halt: QBO data is stale. Last BalanceSheet timestamp "
            f"{freshness['as_of']} ({freshness['hours_old']}h old, threshold "
            f"{STALE_DATA_HOURS}h). Reason: {freshness['reason'] or 'exceeds freshness window'}. "
            f"The Counsel is not running on a guess — verify the QBO feed and "
            f"re-trigger when the sync is current."
            f"\n\n— Keystone\nData pulled: {pull_ts_az} AZ"
        )
        return {
            "full_report": halt_text,
            "headline": "Halted: stale QBO data",
            "stats": {"halted": True, "freshness": freshness, "month": target_month},
            "priorities": [],
        }

    # 2. P&L for the target month
    pl_report = qbo.get_profit_loss(start_str, end_str)
    pl = _summarize_pl(pl_report)

    # 3. Balance sheets at month-open and month-close for cash delta
    open_date = start_str
    close_date = end_str
    try:
        bs_open = qbo.get_balance_sheet(open_date)
        cash_open = _extract_cash_from_bs(bs_open)
    except Exception:
        cash_open = 0.0
    try:
        bs_close = qbo.get_balance_sheet(close_date)
        cash_close = _extract_cash_from_bs(bs_close)
    except Exception:
        cash_close = 0.0

    # 4. AR — aging summary + detail
    ar_summary = qbo.get_ar_aging_summary(close_date)
    ar_buckets = _summarize_ar_summary(ar_summary)
    ar_detail = qbo.get_ar_aging_detail(close_date)
    ar = _summarize_ar_detail(ar_detail)

    # 5. 13-week forecast
    open_invoices = qbo.get_open_invoices()
    starting_cash_today = _extract_cash_from_bs(bs_today)
    weekly_inflows = _project_ar_inflows(open_invoices, ar, today_az)
    weekly_outflows, outflow_detail = _project_outflows(pl)
    forecast = _build_forecast(starting_cash_today, weekly_inflows, weekly_outflows, today_az)

    # 6. History for comparisons
    history = _load_history()
    prior_pl = None
    yoy_pl = None
    prior_snap = _prior_month_snapshot(history, target_month)
    if prior_snap:
        prior_pl = prior_snap.get("pl")
    yoy_snap = _same_month_prior_year(history, target_month)
    if yoy_snap:
        yoy_pl = yoy_snap.get("pl")

    confidence = _forecast_confidence(history)

    # 7. Coaching priorities (data-driven)
    priorities = _derive_priorities(pl, prior_pl, ar, ar_buckets, forecast)

    # 8. Build the report
    full_report = _build_report(
        target_month, pl, prior_pl, yoy_pl,
        cash_open, cash_close,
        ar, ar_buckets,
        forecast, outflow_detail,
        confidence, pull_ts_az,
        priorities,
    )

    headline = (
        f"Revenue {_fmt_dollars(pl['revenue'])}, gross margin "
        f"{_fmt_pct(pl['gross_margin_pct'])}, net "
        f"{_fmt_dollars(pl['net_income'])} ({_fmt_pct(pl['net_margin_pct'])}), "
        f"cash {('up' if cash_close >= cash_open else 'down')} "
        f"{_fmt_dollars(abs(cash_close - cash_open))} to {_fmt_dollars(cash_close)}."
    )

    stats: dict[str, Any] = {
        "month": target_month,
        "pull_timestamp_az": pull_ts_az,
        "freshness": freshness,
        "pl": pl,
        "prior_pl_available": prior_pl is not None,
        "yoy_pl_available": yoy_pl is not None,
        "cash_open": cash_open,
        "cash_close": cash_close,
        "cash_delta": cash_close - cash_open,
        "ar": ar,
        "ar_buckets": ar_buckets,
        "forecast": forecast,
        "forecast_outflow_detail": outflow_detail,
        "forecast_confidence": confidence,
    }

    # 9. Persist this month for next time
    history["months"].append({
        "month": target_month,
        "pulled_at": now_az.isoformat(),
        "pl": {
            "revenue": pl["revenue"],
            "cogs": pl["cogs"],
            "gross_profit": pl["gross_profit"],
            "gross_margin_pct": pl["gross_margin_pct"],
            "operating_expenses": pl["operating_expenses"],
            "net_income": pl["net_income"],
            "net_margin_pct": pl["net_margin_pct"],
        },
        "cash_open": cash_open,
        "cash_close": cash_close,
        "ar_total": ar_buckets.get("total", 0.0),
    })
    try:
        _save_history(history)
    except OSError as e:
        stats["history_write_error"] = str(e)

    return {
        "full_report": full_report,
        "headline": headline,
        "stats": stats,
        "priorities": priorities,
    }
