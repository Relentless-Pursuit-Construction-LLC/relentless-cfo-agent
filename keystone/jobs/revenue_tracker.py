"""Monthly Revenue Tracker — the three revenue numbers, six months side by side.

Runs the 1st of each month. Answers Josh's question "how do we track gross
revenue each month" by showing all three meanings of "revenue" so they never
get confused (the 2026-06-01 $405K incident):

  1. SALES BOOKED — total $ of deals marked Won in GoHighLevel that month.
     The true growth number. Source: GHL opportunities, status='won',
     grouped by month of lastStatusChangeAt.

  2. REVENUE RECOGNIZED — QBO "Total Income" for the month (accrual P&L).
     What the accountant/taxes care about. Monthly grain, so intra-month
     invoice backdating doesn't distort it.

  3. CASH COLLECTED — customer Payments + bank Deposits that hit in the month.
     What survival depends on. Source: QBO Payment + Deposit, TxnDate basis.

Read-only on QBO + GHL. Never sends Slack directly — returns text for delivery.
"""

from __future__ import annotations

import calendar
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from keystone import ghl, qbo

logger = logging.getLogger(__name__)

AZ_TZ_OFFSET = timezone(timedelta(hours=-7))
DEFAULT_MONTHS_BACK = 6

# Monthly revenue target from CONFIG (ramp pace).
MONTHLY_REVENUE_TARGET = 325_000.00


# --- Month helpers ---------------------------------------------------------


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    """Return (first_day, last_day) as YYYY-MM-DD for a calendar month."""
    last = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}"


def _recent_months(n: int, today: date) -> list[tuple[int, int]]:
    """Return the last n (year, month) tuples, oldest first, including current."""
    out: list[tuple[int, int]] = []
    y, m = today.year, today.month
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(out))


def _month_label(year: int, month: int) -> str:
    return f"{calendar.month_abbr[month]} {year}"


# --- Number extractors -----------------------------------------------------


def _sum_entity(query_result: dict[str, Any], entity: str, field: str = "TotalAmt") -> float:
    qr = query_result.get("QueryResponse", {})
    rows = qr.get(entity, []) or []
    total = 0.0
    for r in rows:
        v = r.get(field)
        if isinstance(v, (int, float)):
            total += float(v)
        elif isinstance(v, str):
            try:
                total += float(v.replace(",", "").replace("$", ""))
            except ValueError:
                pass
    return total


def _extract_total_income(pl_report: dict[str, Any]) -> float:
    """Walk a QBO ProfitAndLoss report and return Total Income.

    The Income section is a Row with group='Income' (or a Section whose Header
    says 'Income'); its Summary.ColData carries the total in the last column.
    Defensive: returns 0.0 if not found.
    """
    def walk(node: Any) -> float | None:
        if isinstance(node, dict):
            # A section keyed by group 'Income' carries the total in Summary.
            if node.get("group") == "Income":
                summary = node.get("Summary") or {}
                cols = summary.get("ColData") or []
                for col in reversed(cols):
                    val = (col or {}).get("value", "")
                    try:
                        return float(str(val).replace(",", "").replace("$", ""))
                    except (ValueError, AttributeError):
                        continue
            for v in node.values():
                r = walk(v)
                if r is not None:
                    return r
        elif isinstance(node, list):
            for v in node:
                r = walk(v)
                if r is not None:
                    return r
        return None

    result = walk(pl_report)
    return result if result is not None else 0.0


# --- GHL sales-booked aggregation ------------------------------------------


def _sales_booked_by_month(months: list[tuple[int, int]]) -> dict[tuple[int, int], dict[str, Any]]:
    """Sum Won-deal monetaryValue per month, keyed by (year, month).

    Returns {(y,m): {"total": float, "count": int}}. Empty/zero if GHL not
    configured or unreachable — the report degrades gracefully.
    """
    buckets: dict[tuple[int, int], dict[str, Any]] = {
        (y, m): {"total": 0.0, "count": 0} for (y, m) in months
    }
    if not ghl.is_configured():
        return buckets
    try:
        opps = ghl.search_all_opportunities()
    except Exception as e:
        logger.warning("GHL fetch failed: %s", e)
        return buckets

    wanted = set(months)
    for opp in opps:
        if opp.get("status") != "won":
            continue
        # lastStatusChangeAt is the best proxy for "booked date"
        ts = opp.get("lastStatusChangeAt") or opp.get("updatedAt") or opp.get("createdAt")
        if not ts:
            continue
        try:
            d = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
        except ValueError:
            continue
        key = (d.year, d.month)
        if key in wanted:
            buckets[key]["total"] += ghl.monetary_value(opp)
            buckets[key]["count"] += 1
    return buckets


# --- Formatting ------------------------------------------------------------


def _fmt(v: float) -> str:
    return f"${v:,.0f}"


def _build_message(rows: list[dict[str, Any]], pull_ts_az: str) -> str:
    """Compose the monthly tracker. Plain English, clearly-labeled columns."""
    L: list[str] = ["RELENTLESS — MONTHLY REVENUE TRACKER", ""]
    L.append("Three ways to count revenue, six months side by side:")
    L.append("")
    L.append("  SALES BOOKED   = deals we closed (GoHighLevel)")
    L.append("  RECOGNIZED     = revenue earned/billed (QuickBooks P&L)")
    L.append("  CASH COLLECTED = money that hit the bank")
    L.append("")
    header = f"{'Month':<10} {'Sales Booked':>14} {'Recognized':>14} {'Cash In':>14}"
    L.append(header)
    L.append("-" * len(header))
    for r in rows:
        L.append(
            f"{r['label']:<10} "
            f"{_fmt(r['sales_booked']):>14} "
            f"{_fmt(r['recognized']):>14} "
            f"{_fmt(r['cash_collected']):>14}"
        )
    L.append("")

    # Trend read on the most recent COMPLETE month vs the one before it.
    if len(rows) >= 2:
        cur, prev = rows[-1], rows[-2]
        booked_delta = cur["sales_booked"] - prev["sales_booked"]
        direction = "up" if booked_delta >= 0 else "down"
        L.append(
            f"Sales booked {direction} {_fmt(abs(booked_delta))} "
            f"({cur['label']} vs {prev['label']})."
        )
        tgt_pct = (cur["sales_booked"] / MONTHLY_REVENUE_TARGET * 100) if MONTHLY_REVENUE_TARGET else 0
        L.append(f"{cur['label']} sales booked is {tgt_pct:.0f}% of the {_fmt(MONTHLY_REVENUE_TARGET)} monthly target.")

    L.append("")
    L.append(
        "Note: SALES BOOKED comes from GoHighLevel (deals marked Won). "
        "RECOGNIZED and CASH come from QuickBooks. The three differ because "
        "we bill and collect on a delay from when we close — especially on "
        "financed deals."
    )

    body = "\n".join(L)
    return body + f"\n\n— Keystone\nData pulled: {pull_ts_az} AZ"


# --- Main entrypoint -------------------------------------------------------


def run_revenue_tracker(months_back: int = DEFAULT_MONTHS_BACK) -> dict[str, Any]:
    """Build the monthly revenue tracker. Returns message_text + stats."""
    now_az = datetime.now(AZ_TZ_OFFSET)
    today = now_az.date()
    pull_ts_az = now_az.strftime("%Y-%m-%d %H:%M")

    months = _recent_months(months_back, today)

    # 1. Sales booked (GHL) — one fetch, bucketed by month.
    sales = _sales_booked_by_month(months)

    # 2 + 3. Recognized (P&L) and cash collected (payments+deposits) per month.
    rows: list[dict[str, Any]] = []
    for (y, m) in months:
        start, end = _month_bounds(y, m)
        try:
            pl = qbo.get_profit_loss(start, end)
            recognized = _extract_total_income(pl)
        except Exception as e:
            logger.warning("P&L fetch failed for %s-%s: %s", y, m, e)
            recognized = 0.0
        try:
            pays = _sum_entity(qbo.get_payments_for_range(start, end), "Payment")
            deps = _sum_entity(qbo.get_deposits_for_range(start, end), "Deposit")
            cash_collected = pays + deps
        except Exception as e:
            logger.warning("Cash fetch failed for %s-%s: %s", y, m, e)
            cash_collected = 0.0

        rows.append({
            "label": _month_label(y, m),
            "year": y,
            "month": m,
            "sales_booked": sales.get((y, m), {}).get("total", 0.0),
            "sales_booked_count": sales.get((y, m), {}).get("count", 0),
            "recognized": recognized,
            "cash_collected": cash_collected,
        })

    message_text = _build_message(rows, pull_ts_az)
    return {
        "message_text": message_text,
        "stats": {
            "months": rows,
            "ghl_configured": ghl.is_configured(),
            "pull_timestamp_az": pull_ts_az,
        },
    }
