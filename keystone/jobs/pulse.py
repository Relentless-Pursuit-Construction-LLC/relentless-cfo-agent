"""The Pulse — daily cash heartbeat.

Runs 6:30 AM MT (Mon-Sun). Single message + email to Josh, Matt, Joanne.

Body (under 50 words):
  - Cash position (Chase aggregate from QBO balance sheet)
  - Day-over-day change (from prior snapshot in /data/pulse_history.json)
  - Yesterday's revenue (sum of QBO invoices dated yesterday — TxnDate basis)
  - Vs. $12,500 daily target ($75K/week / 6 days)
  - 7-day rolling avg revenue (once history has data)
  - One anomaly flag (cash drop >$10K w/no scheduled outflow, or revenue <50% of 7-day avg)
  - Data freshness — halt if QBO BalanceSheet report timestamp >48h old

Revenue source of truth: QBO Invoices with TxnDate = yesterday (gross invoiced).
Cash collection (payments-based) is a Phase 2 enhancement.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from keystone import qbo

# --- Configuration ---------------------------------------------------------

STATE_DIR = os.environ.get("STATE_DIR", "/data")
PULSE_HISTORY_PATH = f"{STATE_DIR}/pulse_history.json"

DAILY_REVENUE_TARGET = 12_500.00          # $75K/week / 6 days
CASH_DROP_FLAG_THRESHOLD = 10_000.00      # absolute dollars
REVENUE_LOW_FLAG_RATIO = 0.50             # <50% of 7-day avg
STALE_DATA_HOURS = 48
ROLLING_WINDOW_DAYS = 7

# Arizona is UTC-7 year-round (no DST). MT cron == AZ time for our purposes.
AZ_TZ_OFFSET = timezone(timedelta(hours=-7))


# --- History persistence ---------------------------------------------------


def _load_history() -> dict[str, Any]:
    """Load pulse history. Returns empty shell on first run."""
    path = Path(PULSE_HISTORY_PATH)
    if not path.exists():
        return {"snapshots": []}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if "snapshots" not in data or not isinstance(data["snapshots"], list):
            return {"snapshots": []}
        return data
    except (json.JSONDecodeError, OSError):
        # Corrupt file — start clean rather than crash the morning run.
        return {"snapshots": []}


def _save_history(history: dict[str, Any]) -> None:
    """Atomic write to pulse_history.json. Keeps last 60 snapshots."""
    history["snapshots"] = history["snapshots"][-60:]
    path = Path(PULSE_HISTORY_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(history, f, indent=2)
    tmp_path.replace(path)


def _prior_snapshot(history: dict[str, Any], for_date: str) -> dict[str, Any] | None:
    """Return the most recent snapshot strictly before for_date (YYYY-MM-DD)."""
    snaps = [s for s in history["snapshots"] if s.get("as_of") and s["as_of"] < for_date]
    if not snaps:
        return None
    return max(snaps, key=lambda s: s["as_of"])


def _rolling_avg_revenue(history: dict[str, Any], for_date: str) -> float | None:
    """7-day rolling avg of revenue from snapshots strictly before for_date.

    Returns None if we don't have at least 3 days of data — refuse to average
    on too-thin a base.
    """
    cutoff = (datetime.strptime(for_date, "%Y-%m-%d") - timedelta(days=ROLLING_WINDOW_DAYS)).strftime("%Y-%m-%d")
    window = [
        s for s in history["snapshots"]
        if s.get("as_of") and cutoff <= s["as_of"] < for_date and s.get("yesterday_revenue") is not None
    ]
    if len(window) < 3:
        return None
    return sum(s["yesterday_revenue"] for s in window) / len(window)


# --- QBO parsing -----------------------------------------------------------


def _walk_rows(rows_obj: Any, out: list[dict[str, Any]]) -> None:
    """Recursively walk BalanceSheet Rows.Row tree, collecting leaf data rows.

    QBO's BalanceSheet JSON looks like:
      Rows.Row[]  — each Row is either:
        - type='Section' with Header + Rows.Row (nested) + Summary
        - type='Data' with ColData[{value, id}]  (leaf)
    Bank accounts are leaves under a Section whose Header.ColData[0].value
    is 'Bank Accounts' (or similar). We just collect every data row and
    let the caller filter.
    """
    if not isinstance(rows_obj, dict):
        return
    row_list = rows_obj.get("Row", [])
    if not isinstance(row_list, list):
        return
    for row in row_list:
        if not isinstance(row, dict):
            continue
        rtype = row.get("type")
        if rtype == "Data":
            col_data = row.get("ColData", [])
            if isinstance(col_data, list) and len(col_data) >= 2:
                out.append({
                    "name": col_data[0].get("value", "") if isinstance(col_data[0], dict) else "",
                    "account_id": col_data[0].get("id", "") if isinstance(col_data[0], dict) else "",
                    "value_str": col_data[-1].get("value", "") if isinstance(col_data[-1], dict) else "",
                    "group": row.get("group", ""),
                })
        # Sections may contain nested Rows
        nested = row.get("Rows")
        if nested:
            _walk_rows(nested, out)


def _find_bank_section(rows_obj: Any) -> dict[str, Any] | None:
    """Locate the 'Bank Accounts' section inside the report tree.

    Walks until it finds a Section whose Header.ColData[0].value matches
    'Bank Accounts' (QBO's default label for the AccountType=Bank cluster
    under Current Assets).
    """
    if not isinstance(rows_obj, dict):
        return None
    for row in rows_obj.get("Row", []) or []:
        if not isinstance(row, dict):
            continue
        if row.get("type") == "Section":
            header = row.get("Header", {})
            cd = header.get("ColData", []) if isinstance(header, dict) else []
            label = cd[0].get("value", "") if cd and isinstance(cd[0], dict) else ""
            if label.strip().lower() in ("bank accounts", "checking", "checking/savings"):
                return row
            # Recurse deeper — bank section sits nested under Current Assets / Assets
            nested = row.get("Rows")
            found = _find_bank_section(nested) if nested else None
            if found:
                return found
    return None


def _parse_money(s: str) -> float:
    """Parse a QBO money string. Handles '', '1,234.56', '(123.45)' (negative)."""
    if not s:
        return 0.0
    s = s.strip().replace(",", "").replace("$", "")
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return -v if neg else v


def extract_cash_position(balance_sheet: dict[str, Any]) -> dict[str, Any]:
    """Walk the BalanceSheet report and return Chase / bank cash aggregate.

    Returns:
      {
        "total": float,             # sum of all bank accounts
        "accounts": [{name, balance}],
        "report_as_of": "YYYY-MM-DD" or None,
      }
    """
    header = balance_sheet.get("Header", {})
    report_as_of = header.get("EndPeriod") or header.get("StartPeriod")

    rows_obj = balance_sheet.get("Rows", {})
    bank_section = _find_bank_section(rows_obj)

    accounts: list[dict[str, Any]] = []
    if bank_section is not None:
        leaves: list[dict[str, Any]] = []
        _walk_rows(bank_section.get("Rows", {}), leaves)
        for leaf in leaves:
            accounts.append({
                "name": leaf["name"],
                "balance": _parse_money(leaf["value_str"]),
            })

    total = sum(a["balance"] for a in accounts)
    return {"total": total, "accounts": accounts, "report_as_of": report_as_of}


def extract_invoiced_revenue(invoice_query_result: dict[str, Any]) -> float:
    """Sum TotalAmt across the invoices returned by get_invoices_for_date()."""
    qr = invoice_query_result.get("QueryResponse", {})
    invoices = qr.get("Invoice", []) or []
    total = 0.0
    for inv in invoices:
        amt = inv.get("TotalAmt")
        if isinstance(amt, (int, float)):
            total += float(amt)
        elif isinstance(amt, str):
            total += _parse_money(amt)
    return total


# --- Freshness check -------------------------------------------------------


def _check_freshness(balance_sheet: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Return {'stale': bool, 'as_of': str|None, 'hours_old': float|None, 'reason': str}."""
    header = balance_sheet.get("Header", {})
    # QBO returns 'Time' as the report generation timestamp (ISO-ish).
    report_time = header.get("Time") or header.get("EndPeriod")
    if not report_time:
        return {"stale": True, "as_of": None, "hours_old": None,
                "reason": "QBO BalanceSheet returned no timestamp"}

    # Header.Time looks like '2026-05-25T06:30:14-07:00' — strip and parse.
    parsed: datetime | None = None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(report_time[:25], fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        # We can't determine age — be defensive and flag.
        return {"stale": True, "as_of": report_time, "hours_old": None,
                "reason": f"Could not parse QBO timestamp '{report_time}'"}

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age_hours = (now - parsed).total_seconds() / 3600.0
    return {
        "stale": age_hours > STALE_DATA_HOURS,
        "as_of": report_time,
        "hours_old": round(age_hours, 1),
        "reason": "",
    }


# --- Voice (hand-built, no Claude call for the daily heartbeat) -----------


def _fmt_dollars(v: float) -> str:
    """Plain '$12,345' — no decimals for the heartbeat body."""
    return f"${v:,.0f}"


def _fmt_signed(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}{_fmt_dollars(abs(v))}"


def _build_message(stats: dict[str, Any], anomalies: list[str], pull_ts_az: str) -> str:
    """Compose the body Keystone speaks to Josh. Under 50 words."""
    cash = stats["cash_position"]
    dod = stats.get("cash_dod_change")
    rev = stats["yesterday_revenue"]
    tgt = stats["daily_revenue_target"]
    pct_target = (rev / tgt * 100) if tgt else 0
    rolling = stats.get("rolling_7d_avg_revenue")

    lines = []
    if dod is None:
        lines.append(f"Cash: {_fmt_dollars(cash)} (no prior snapshot).")
    else:
        lines.append(f"Cash: {_fmt_dollars(cash)} ({_fmt_signed(dod)} vs yesterday).")

    lines.append(
        f"Yesterday's revenue: {_fmt_dollars(rev)} ({pct_target:.0f}% of {_fmt_dollars(tgt)} target)."
    )

    if rolling is None:
        lines.append("7-day avg: not enough history yet.")
    else:
        lines.append(f"7-day avg: {_fmt_dollars(rolling)}/day.")

    if anomalies:
        lines.append("Flag: " + "; ".join(anomalies))
    else:
        lines.append("Anomalies: none flagged.")

    body = " ".join(lines)
    sign_off = f"\n\n— Keystone\nData pulled: {pull_ts_az} AZ"
    return body + sign_off


# --- Main entrypoint -------------------------------------------------------


def run_pulse(as_of: date | None = None) -> dict[str, Any]:
    """Daily cash heartbeat. Returns dict with message_text, stats, anomalies.

    Does not send to Slack/email — caller owns delivery.
    """
    now_az = datetime.now(AZ_TZ_OFFSET)
    today = as_of or now_az.date()
    yesterday = today - timedelta(days=1)
    today_str = today.strftime("%Y-%m-%d")
    yesterday_str = yesterday.strftime("%Y-%m-%d")
    pull_ts_az = now_az.strftime("%Y-%m-%d %H:%M")

    # 1. Pull balance sheet (as-of today) for cash position.
    balance_sheet = qbo.get_balance_sheet(today_str)

    # 2. Freshness gate — halt if QBO data is too stale.
    freshness = _check_freshness(balance_sheet, datetime.now(timezone.utc))
    if freshness["stale"]:
        halt_text = (
            f"Halt: QBO data is stale. Last report timestamp {freshness['as_of']} "
            f"({freshness['hours_old']}h old, threshold {STALE_DATA_HOURS}h). "
            f"Reason: {freshness['reason'] or 'exceeds freshness window'}. "
            f"Not guessing the number — verify the QBO feed before relying on today's pulse."
            f"\n\n— Keystone\nData pulled: {pull_ts_az} AZ"
        )
        return {
            "message_text": halt_text,
            "stats": {"halted": True, "freshness": freshness},
            "anomalies": ["stale_qbo_data"],
        }

    # 3. Cash position from balance sheet.
    cash_info = extract_cash_position(balance_sheet)
    cash_total = cash_info["total"]

    # 4. Yesterday's revenue from QBO invoices dated yesterday.
    invoices = qbo.get_invoices_for_date(yesterday_str)
    yesterday_revenue = extract_invoiced_revenue(invoices)

    # 5. History — load, compute deltas, append today's snapshot.
    history = _load_history()
    prior = _prior_snapshot(history, today_str)
    rolling_avg = _rolling_avg_revenue(history, today_str)

    if prior is not None:
        cash_dod_change: float | None = cash_total - float(prior.get("cash_position", 0.0))
    else:
        cash_dod_change = None

    # 6. Anomaly detection — at most one surfaced in the body.
    anomalies: list[str] = []
    if cash_dod_change is not None and cash_dod_change < -CASH_DROP_FLAG_THRESHOLD:
        anomalies.append(
            f"cash down {_fmt_dollars(abs(cash_dod_change))} day-over-day — confirm with Joanne which outflow drove it"
        )
    if rolling_avg is not None and rolling_avg > 0 and yesterday_revenue < rolling_avg * REVENUE_LOW_FLAG_RATIO:
        anomalies.append(
            f"revenue {_fmt_dollars(yesterday_revenue)} is below 50% of 7-day avg {_fmt_dollars(rolling_avg)}"
        )
    # System prompt says "one anomaly if any" — keep the most material.
    surfaced = anomalies[:1]

    stats: dict[str, Any] = {
        "as_of": today_str,
        "yesterday_date": yesterday_str,
        "cash_position": cash_total,
        "cash_accounts": cash_info["accounts"],
        "cash_dod_change": cash_dod_change,
        "yesterday_revenue": yesterday_revenue,
        "daily_revenue_target": DAILY_REVENUE_TARGET,
        "revenue_pct_of_target": (yesterday_revenue / DAILY_REVENUE_TARGET * 100) if DAILY_REVENUE_TARGET else None,
        "rolling_7d_avg_revenue": rolling_avg,
        "freshness": freshness,
        "prior_snapshot_date": prior["as_of"] if prior else None,
        "pull_timestamp_az": pull_ts_az,
    }

    message_text = _build_message(stats, surfaced, pull_ts_az)

    # 7. Persist today's snapshot for tomorrow's run.
    history["snapshots"].append({
        "as_of": today_str,
        "cash_position": cash_total,
        "yesterday_revenue": yesterday_revenue,
        "pulled_at": now_az.isoformat(),
    })
    try:
        _save_history(history)
    except OSError as e:
        # Don't fail the morning report because the volume hiccupped — surface it.
        stats["history_write_error"] = str(e)

    return {
        "message_text": message_text,
        "stats": stats,
        "anomalies": surfaced,
    }
