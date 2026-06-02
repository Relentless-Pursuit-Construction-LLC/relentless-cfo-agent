"""The Pulse — daily cash heartbeat.

Runs 6:32 AM AZ daily. Single Slack message to the KEYSTONE_AUDIENCE.

Shows FOUR distinct "revenue-ish" numbers, each clearly labeled so they never
blur together (the 2026-06-01 incident: a $405K 'revenue' number that was really
old invoices backdated to month-end):

  1. CASH IN THE BANK — total + per-account, day-over-day change (balance sheet)
  2. MONEY COLLECTED YESTERDAY — customer Payments + bank Deposits (real cash in)
  3. NEW BILLS SENT YESTERDAY — invoices CREATED yesterday (MetaData.CreateTime),
     immune to TxnDate backdating
  4. BILLS DATED YESTERDAY — invoices with TxnDate=yesterday. Flagged when it
     looks inflated by backdated invoices.

Plus: 7-day rolling avg of NEW BILLS (real activity), one anomaly flag, and a
freshness gate (halt if QBO BalanceSheet timestamp >48h old).

Phase 2: live bank-deposit verification straight from Ramp/Chase (needs a Ramp
API key) to sit alongside the QBO-recorded "money collected" number.
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


def _sum_entity(query_result: dict[str, Any], entity: str, field: str = "TotalAmt") -> tuple[float, int]:
    """Sum a money field across an entity list in a QBO query response.

    Returns (total, count). Handles numeric and string amounts.
    """
    qr = query_result.get("QueryResponse", {})
    rows = qr.get(entity, []) or []
    total = 0.0
    for r in rows:
        amt = r.get(field)
        if isinstance(amt, (int, float)):
            total += float(amt)
        elif isinstance(amt, str):
            total += _parse_money(amt)
    return total, len(rows)


def extract_invoiced_revenue(invoice_query_result: dict[str, Any]) -> float:
    """Sum TotalAmt across the invoices returned by an invoice query."""
    total, _ = _sum_entity(invoice_query_result, "Invoice")
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


def _build_message(stats: dict[str, Any], anomalies: list[str], pull_ts_az: str, yesterday_label: str) -> str:
    """Compose the daily Pulse. Four clearly-labeled sections, plain English.

    Sections deliberately separate the FOUR different 'revenue-ish' numbers so
    they never blur together:
      1. CASH IN THE BANK — what we actually have right now
      2. MONEY COLLECTED — what customers actually paid us yesterday
      3. NEW BILLS SENT — invoices we genuinely created yesterday
      4. BILLS DATED YESTERDAY — accounting-date total (can include backdated)
    """
    cash = stats["cash_position"]
    dod = stats.get("cash_dod_change")
    accounts = stats.get("cash_accounts", []) or []

    collected = stats.get("money_collected_yesterday", 0.0)
    collected_count = stats.get("money_collected_count", 0)

    new_bills = stats.get("new_bills_sent", 0.0)
    new_bills_count = stats.get("new_bills_count", 0)

    dated = stats.get("yesterday_revenue", 0.0)
    dated_count = stats.get("yesterday_revenue_count", 0)
    backdated_flag = stats.get("backdated_detected", False)

    L: list[str] = [f"RELENTLESS — DAILY PULSE — {yesterday_label}", ""]

    # 1. Cash in the bank
    if dod is None:
        L.append(f"CASH IN THE BANK (what we have): {_fmt_dollars(cash)} (no prior day to compare)")
    else:
        L.append(f"CASH IN THE BANK (what we have): {_fmt_dollars(cash)} ({_fmt_signed(dod)} vs yesterday)")
    for a in accounts:
        nm = a.get("name", "account")
        bal = a.get("balance", 0.0)
        L.append(f"   - {nm}: {_fmt_dollars(bal)}")

    # 2. Money actually collected
    L.append("")
    L.append(
        f"MONEY COLLECTED YESTERDAY (customers paid us): {_fmt_dollars(collected)} "
        f"across {collected_count} payment(s)"
    )
    L.append("   (from QBO records; live bank-deposit verification coming in Phase 2)")

    # 3. New bills actually created
    L.append("")
    L.append(
        f"NEW BILLS SENT YESTERDAY (invoices we created): {_fmt_dollars(new_bills)} "
        f"across {new_bills_count} new invoice(s)"
    )

    # 4. Accounting-date total, with backdating caveat
    L.append("")
    L.append(
        f"BILLS DATED YESTERDAY (accounting date): {_fmt_dollars(dated)} across {dated_count} invoice(s)"
    )
    if backdated_flag:
        L.append(
            "   Heads up: this total includes older invoices stamped with yesterday's "
            "date (backdating), so it overstates real activity. Use the two numbers above "
            "for what actually happened."
        )

    # Anomalies
    L.append("")
    if anomalies:
        L.append("FLAG: " + "; ".join(anomalies))
    else:
        L.append("ANOMALIES: none flagged.")

    body = "\n".join(L)
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

    # 4. The four "revenue-ish" numbers, kept distinct:

    # 4a. Money actually collected yesterday (customer payments + bank deposits)
    payments = qbo.get_payments_for_date(yesterday_str)
    payments_total, payments_count = _sum_entity(payments, "Payment")
    try:
        deposits = qbo.get_deposits_for_date(yesterday_str)
        deposits_total, deposits_count = _sum_entity(deposits, "Deposit")
    except Exception:
        deposits_total, deposits_count = 0.0, 0
    money_collected = payments_total + deposits_total
    money_collected_count = payments_count + deposits_count

    # 4b. New bills genuinely created yesterday (immune to backdating)
    created_invoices = qbo.get_invoices_created_on_date(yesterday_str)
    new_bills_total, new_bills_count = _sum_entity(created_invoices, "Invoice")

    # 4c. Invoices DATED yesterday (TxnDate basis — may include backdated)
    invoices = qbo.get_invoices_for_date(yesterday_str)
    yesterday_revenue, yesterday_revenue_count = _sum_entity(invoices, "Invoice")

    # Detect backdating: if the TxnDate total is much bigger than the
    # genuinely-created total, old invoices are being swept in.
    backdated_detected = (
        yesterday_revenue > new_bills_total * 1.5 and yesterday_revenue > 25_000
    )

    # 5. History — load, compute deltas, append today's snapshot.
    history = _load_history()
    prior = _prior_snapshot(history, today_str)
    rolling_avg = _rolling_avg_revenue(history, today_str)

    if prior is not None:
        cash_dod_change: float | None = cash_total - float(prior.get("cash_position", 0.0))
    else:
        cash_dod_change = None

    # 6. Anomaly detection — at most one surfaced in the body.
    #    Anomalies now key off MONEY COLLECTED + NEW BILLS, not the backdating-
    #    distorted TxnDate number.
    anomalies: list[str] = []
    if cash_dod_change is not None and cash_dod_change < -CASH_DROP_FLAG_THRESHOLD:
        anomalies.append(
            f"cash down {_fmt_dollars(abs(cash_dod_change))} day-over-day — confirm with Joanne which outflow drove it"
        )
    if rolling_avg is not None and rolling_avg > 0 and new_bills_total < rolling_avg * REVENUE_LOW_FLAG_RATIO:
        anomalies.append(
            f"new bills {_fmt_dollars(new_bills_total)} below 50% of 7-day avg {_fmt_dollars(rolling_avg)}"
        )
    # System prompt says "one anomaly if any" — keep the most material.
    surfaced = anomalies[:1]

    yesterday_label = yesterday.strftime("%a %b %d, %Y")

    stats: dict[str, Any] = {
        "as_of": today_str,
        "yesterday_date": yesterday_str,
        "cash_position": cash_total,
        "cash_accounts": cash_info["accounts"],
        "cash_dod_change": cash_dod_change,
        # Money actually collected (customer payments + bank deposits)
        "money_collected_yesterday": money_collected,
        "money_collected_count": money_collected_count,
        "money_collected_payments": payments_total,
        "money_collected_deposits": deposits_total,
        # New bills genuinely created yesterday
        "new_bills_sent": new_bills_total,
        "new_bills_count": new_bills_count,
        # Invoices DATED yesterday (TxnDate — may be backdated)
        "yesterday_revenue": yesterday_revenue,
        "yesterday_revenue_count": yesterday_revenue_count,
        "backdated_detected": backdated_detected,
        "daily_revenue_target": DAILY_REVENUE_TARGET,
        "rolling_7d_avg_revenue": rolling_avg,
        "freshness": freshness,
        "prior_snapshot_date": prior["as_of"] if prior else None,
        "pull_timestamp_az": pull_ts_az,
    }

    message_text = _build_message(stats, surfaced, pull_ts_az, yesterday_label)

    # 7. Persist today's snapshot for tomorrow's run.
    #    Rolling average now tracks NEW BILLS (real daily activity), not the
    #    backdating-distorted TxnDate number.
    history["snapshots"].append({
        "as_of": today_str,
        "cash_position": cash_total,
        "yesterday_revenue": new_bills_total,
        "money_collected": money_collected,
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
