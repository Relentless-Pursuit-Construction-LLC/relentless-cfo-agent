"""AR Aging Digest — Keystone's first feature.

Runs Mon-Sat 6:30 AM MT. Pulls AR aging detail from QBO, joins customers to
assigned reps via the shared rep registry, generates:
  - Per-rep DM (their stuck customers, aged buckets, total at risk)
  - Matt summary DM (full picture, concentration flags, week-over-week delta)

Read-only. Never writes to QBO. Never sends Slack directly — returns text
payload that main.py / cron wires through slack_client.

Defensive by design — early-stage QBO data is messy. Missing customer.SalesRep,
missing custom fields, zero balances, empty AR, registry lookup failures all
fall through to "unassigned" with a flag for Matt.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from keystone import qbo, rep_mapping, voice

logger = logging.getLogger(__name__)

# Where we persist last-run totals so we can compute week-over-week delta.
STATE_DIR = os.environ.get("STATE_DIR", "/data")
LAST_RUN_PATH = Path(STATE_DIR) / "ar_aging_last_run.json"

AZ_TZ = ZoneInfo("America/Phoenix")

# AR aging buckets (QBO standard)
BUCKETS = ["current", "1_30", "31_60", "61_90", "91_plus"]
BUCKET_LABELS = {
    "current": "Current",
    "1_30": "1-30 days",
    "31_60": "31-60 days",
    "61_90": "61-90 days",
    "91_plus": "91+ days",
}

# Concentration risk threshold (single customer > X% of total AR)
CONCENTRATION_THRESHOLD_PCT = 5.0

# Minimum balance to be worth flagging in a per-rep digest
MIN_FLAG_BALANCE = 0.01


# --- Helpers ---------------------------------------------------------------


def _fmt_usd(amount: float) -> str:
    """Plain dollar formatting — no rounding in Relentless's favor."""
    if amount < 0:
        return f"-${abs(amount):,.2f}"
    return f"${amount:,.2f}"


def _fmt_pct(numerator: float, denominator: float) -> str:
    if denominator <= 0:
        return "0.0%"
    return f"{(numerator / denominator) * 100:.1f}%"


def _bucket_for_age_days(age_days: int) -> str:
    if age_days <= 0:
        return "current"
    if age_days <= 30:
        return "1_30"
    if age_days <= 60:
        return "31_60"
    if age_days <= 90:
        return "61_90"
    return "91_plus"


def _empty_buckets() -> dict[str, float]:
    return {b: 0.0 for b in BUCKETS}


def _now_stamp() -> str:
    return datetime.now(tz=AZ_TZ).strftime("%Y-%m-%d %H:%M AZ")


def _signoff() -> str:
    return f"\n\n— Keystone\nData pulled: {_now_stamp()}"


# --- QBO report parsing ----------------------------------------------------


def _walk_rows(node: Any) -> list[dict[str, Any]]:
    """Recursively flatten QBO report row structures into leaf rows.

    QBO Aged Receivable Detail nests Rows inside Rows. Leaf rows have a
    'ColData' list. Section rows have 'Header' + nested 'Rows'.
    """
    leaves: list[dict[str, Any]] = []
    if not isinstance(node, dict):
        return leaves

    if "ColData" in node:
        leaves.append(node)
        return leaves

    rows_container = node.get("Rows")
    if isinstance(rows_container, dict):
        for child in rows_container.get("Row", []) or []:
            leaves.extend(_walk_rows(child))
    elif isinstance(rows_container, list):
        for child in rows_container:
            leaves.extend(_walk_rows(child))

    # Some QBO responses also stash rows under "Row" at the top level
    for child in node.get("Row", []) or []:
        leaves.extend(_walk_rows(child))

    return leaves


def _column_index(report: dict[str, Any]) -> dict[str, int]:
    """Map column title (lowercased) -> index into ColData."""
    cols = (
        report.get("Columns", {}).get("Column", [])
        or report.get("Header", {}).get("Option", [])
        or []
    )
    idx: dict[str, int] = {}
    for i, col in enumerate(cols):
        title = (col.get("ColTitle") or col.get("MetaData", [{}])[0].get("Value") or "").strip().lower()
        if title:
            idx[title] = i
    return idx


def _safe_float(s: Any) -> float:
    if s is None or s == "":
        return 0.0
    try:
        return float(str(s).replace(",", "").replace("$", ""))
    except (ValueError, TypeError):
        return 0.0


def _parse_ar_detail_rows(report: dict[str, Any], as_of: date) -> list[dict[str, Any]]:
    """Flatten AR aging detail into per-invoice records.

    Returns list of {customer, txn_date, num, due_date, amount, open_balance,
    age_days, bucket}. Defensive about missing columns.
    """
    col_idx = _column_index(report)

    # Try common column names QBO uses for AR aging detail
    cust_i = col_idx.get("customer") if "customer" in col_idx else col_idx.get("name")
    date_i = col_idx.get("date") or col_idx.get("txn date")
    due_i = col_idx.get("due date")
    num_i = col_idx.get("num") or col_idx.get("doc num")
    bal_i = col_idx.get("open balance") or col_idx.get("amount")
    amt_i = col_idx.get("amount")
    age_i = col_idx.get("aging") or col_idx.get("age")

    rows = _walk_rows(report)
    out: list[dict[str, Any]] = []
    for row in rows:
        cols = row.get("ColData") or []
        if not cols:
            continue

        def _get(i: int | None) -> str:
            if i is None or i >= len(cols):
                return ""
            return (cols[i].get("value") or "").strip()

        customer = _get(cust_i) if cust_i is not None else ""
        # Some reports put the customer in col 0 by convention
        if not customer and cols:
            customer = (cols[0].get("value") or "").strip()

        # Skip section totals / "Total" rows
        if customer.lower().startswith("total"):
            continue

        balance = _safe_float(_get(bal_i))
        amount = _safe_float(_get(amt_i))
        if balance <= 0 and amount <= 0:
            continue

        # Prefer Open Balance; fall back to Amount
        open_balance = balance if balance > 0 else amount

        # Compute age from due date if present, else from txn date
        age_days = 0
        age_str = _get(age_i) if age_i is not None else ""
        if age_str:
            try:
                age_days = int(float(age_str))
            except ValueError:
                age_days = 0

        if not age_days:
            due_str = _get(due_i)
            ref_str = due_str or _get(date_i)
            if ref_str:
                try:
                    ref_d = datetime.strptime(ref_str, "%Y-%m-%d").date()
                    age_days = (as_of - ref_d).days
                except ValueError:
                    age_days = 0

        # Customer link id (if present in row metadata)
        customer_id = None
        if cust_i is not None and cust_i < len(cols):
            customer_id = (cols[cust_i].get("id") or "").strip() or None

        out.append({
            "customer": customer or "(unknown)",
            "customer_id": customer_id,
            "txn_date": _get(date_i),
            "due_date": _get(due_i),
            "num": _get(num_i),
            "amount": amount,
            "open_balance": open_balance,
            "age_days": age_days,
            "bucket": _bucket_for_age_days(age_days),
        })

    return out


# --- Customer → rep join ---------------------------------------------------


def _customer_rep_name(customer_id: str | None, customer_name: str) -> str | None:
    """Resolve the rep assigned to a QBO customer.

    Priority:
      1. Customer.SalesRep (denormalized name string in QBO)
      2. Customer.CustomField with name like "Sales Rep" / "Rep" / "Closer"
      3. None — caller treats as "unassigned"
    """
    if not customer_id:
        return None

    try:
        # QBO Customer endpoint via query — single read
        safe_id = customer_id.replace("'", "\\'")
        result = qbo.qbo_query(
            f"SELECT * FROM Customer WHERE Id = '{safe_id}' MAXRESULTS 1"
        )
        customers = (result.get("QueryResponse") or {}).get("Customer") or []
        if not customers:
            return None
        cust = customers[0]
    except Exception as e:
        logger.warning("Customer lookup failed for id=%s name=%s: %s", customer_id, customer_name, e)
        return None

    # 1. SalesRep field — QBO stores as denormalized string on some entities
    sales_rep = cust.get("SalesRep") or cust.get("SalesRepRef", {}).get("name")
    if isinstance(sales_rep, str) and sales_rep.strip():
        return sales_rep.strip()

    # 2. Custom fields
    for cf in cust.get("CustomField") or []:
        name = (cf.get("Name") or "").lower()
        value = (cf.get("StringValue") or "").strip()
        if value and any(tag in name for tag in ("sales rep", "rep", "closer", "salesperson")):
            return value

    return None


def _rep_to_slack_id(rep_name: str) -> str | None:
    try:
        return rep_mapping.get_slack_id_for_rep(rep_name)
    except Exception as e:
        logger.warning("Rep registry lookup failed for %s: %s", rep_name, e)
        return None


# --- Aggregation -----------------------------------------------------------


def _aggregate(invoices: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate invoice rows into per-rep + per-customer + totals."""
    by_rep: dict[str, dict[str, Any]] = {}  # rep_name -> {slack_id, customers: {name: {...}}, totals: buckets}
    by_customer: dict[str, dict[str, Any]] = {}
    bucket_totals = _empty_buckets()
    grand_total = 0.0
    unassigned_total = 0.0
    unassigned_customers: set[str] = set()

    # cache rep lookups so we don't hit QBO Customer for every invoice
    customer_to_rep_cache: dict[str, str | None] = {}

    for inv in invoices:
        cust_name = inv["customer"]
        cust_id = inv["customer_id"]
        bucket = inv["bucket"]
        balance = inv["open_balance"]

        bucket_totals[bucket] += balance
        grand_total += balance

        # Customer-level aggregation
        if cust_name not in by_customer:
            by_customer[cust_name] = {
                "name": cust_name,
                "id": cust_id,
                "buckets": _empty_buckets(),
                "total": 0.0,
                "invoices": [],
                "oldest_age_days": 0,
            }
        by_customer[cust_name]["buckets"][bucket] += balance
        by_customer[cust_name]["total"] += balance
        by_customer[cust_name]["invoices"].append(inv)
        if inv["age_days"] > by_customer[cust_name]["oldest_age_days"]:
            by_customer[cust_name]["oldest_age_days"] = inv["age_days"]

        # Resolve rep for this customer (cached)
        cache_key = cust_id or cust_name
        if cache_key not in customer_to_rep_cache:
            customer_to_rep_cache[cache_key] = _customer_rep_name(cust_id, cust_name)
        rep_name = customer_to_rep_cache[cache_key]

        if not rep_name:
            unassigned_total += balance
            unassigned_customers.add(cust_name)
            continue

        if rep_name not in by_rep:
            by_rep[rep_name] = {
                "rep_name": rep_name,
                "slack_id": _rep_to_slack_id(rep_name),
                "customers": {},
                "buckets": _empty_buckets(),
                "total": 0.0,
            }
        by_rep[rep_name]["buckets"][bucket] += balance
        by_rep[rep_name]["total"] += balance

        rep_customers = by_rep[rep_name]["customers"]
        if cust_name not in rep_customers:
            rep_customers[cust_name] = {
                "name": cust_name,
                "buckets": _empty_buckets(),
                "total": 0.0,
                "oldest_age_days": 0,
            }
        rep_customers[cust_name]["buckets"][bucket] += balance
        rep_customers[cust_name]["total"] += balance
        if inv["age_days"] > rep_customers[cust_name]["oldest_age_days"]:
            rep_customers[cust_name]["oldest_age_days"] = inv["age_days"]

    return {
        "by_rep": by_rep,
        "by_customer": by_customer,
        "bucket_totals": bucket_totals,
        "grand_total": grand_total,
        "unassigned_total": unassigned_total,
        "unassigned_customer_count": len(unassigned_customers),
        "unassigned_customers": sorted(unassigned_customers),
    }


# --- State (week-over-week) ------------------------------------------------


def _load_last_run() -> dict[str, Any] | None:
    try:
        if LAST_RUN_PATH.exists():
            with open(LAST_RUN_PATH, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.warning("Could not read last-run state: %s", e)
    return None


def _save_last_run(payload: dict[str, Any]) -> None:
    try:
        LAST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = LAST_RUN_PATH.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        tmp.replace(LAST_RUN_PATH)
    except Exception as e:
        logger.warning("Could not write last-run state: %s", e)


# --- Message rendering -----------------------------------------------------


def _render_rep_message_template(rep_name: str, rep_data: dict[str, Any], grand_total: float) -> str:
    """Deterministic template — used when ANTHROPIC_API_KEY unavailable."""
    lines: list[str] = []
    total = rep_data["total"]
    buckets = rep_data["buckets"]

    lines.append(f"AR aging — your book, {rep_name}.")
    lines.append("")
    lines.append(f"Open balance assigned to you: {_fmt_usd(total)} ({_fmt_pct(total, grand_total)} of total AR).")

    # Bucket breakdown — only non-zero buckets
    bucket_lines = [
        f"  {BUCKET_LABELS[b]}: {_fmt_usd(buckets[b])}"
        for b in BUCKETS
        if buckets[b] > 0
    ]
    if bucket_lines:
        lines.append("")
        lines.append("By age:")
        lines.extend(bucket_lines)

    # Customer list — sort by age then balance
    customers = sorted(
        rep_data["customers"].values(),
        key=lambda c: (-c["oldest_age_days"], -c["total"]),
    )

    if customers:
        lines.append("")
        lines.append(f"Customers ({len(customers)}):")
        for c in customers:
            if c["total"] < MIN_FLAG_BALANCE:
                continue
            age_tag = f"{c['oldest_age_days']}d" if c["oldest_age_days"] > 0 else "current"
            lines.append(f"  - {c['name']}: {_fmt_usd(c['total'])} (oldest {age_tag})")

    # Coaching note — flag the worst bucket
    over_60 = buckets["61_90"] + buckets["91_plus"]
    if over_60 > 0:
        lines.append("")
        lines.append(
            f"{_fmt_usd(over_60)} is more than 60 days out. That money needs a call this week — "
            "not a text, not an email. Confirm pay date or escalate to Matt by Friday."
        )
    elif buckets["31_60"] > 0:
        lines.append("")
        lines.append(
            f"{_fmt_usd(buckets['31_60'])} is in the 31-60 bucket. Get ahead of it now before it ages past 60."
        )

    lines.append(_signoff())
    return "\n".join(lines)


def _render_matt_message_template(summary: dict[str, Any], delta: dict[str, Any] | None) -> str:
    """Deterministic template for Matt — accountant-precise."""
    grand = summary["grand_total"]
    buckets = summary["bucket_totals"]
    by_rep = summary["by_rep"]
    by_customer = summary["by_customer"]
    unassigned = summary["unassigned_total"]
    unassigned_n = summary["unassigned_customer_count"]

    lines: list[str] = []
    lines.append("AR Aging Digest — full picture.")
    lines.append("")
    lines.append(f"Total open AR: {_fmt_usd(grand)}")

    if delta and "grand_total" in delta:
        prior = delta["grand_total"]
        change = grand - prior
        direction = "up" if change > 0 else ("down" if change < 0 else "flat")
        lines.append(
            f"  Week-over-week: {direction} {_fmt_usd(abs(change))} "
            f"({_fmt_pct(abs(change), prior) if prior else 'n/a'}) vs prior pull."
        )

    # Bucket table
    lines.append("")
    lines.append("By aging bucket:")
    for b in BUCKETS:
        amt = buckets[b]
        if amt == 0 and grand == 0:
            continue
        lines.append(f"  {BUCKET_LABELS[b]}: {_fmt_usd(amt)} ({_fmt_pct(amt, grand)})")

    over_60 = buckets["61_90"] + buckets["91_plus"]
    if grand > 0:
        lines.append(f"  Over 60 days: {_fmt_usd(over_60)} ({_fmt_pct(over_60, grand)})")

    # Concentration risk (top 5 customers > threshold)
    top_customers = sorted(
        by_customer.values(),
        key=lambda c: -c["total"],
    )[:5]
    concentration = [
        c for c in top_customers
        if grand > 0 and (c["total"] / grand) * 100 >= CONCENTRATION_THRESHOLD_PCT
    ]
    if concentration:
        lines.append("")
        lines.append(f"Concentration risk (>{CONCENTRATION_THRESHOLD_PCT:.0f}% of AR each):")
        for c in concentration:
            age_tag = f"oldest {c['oldest_age_days']}d" if c["oldest_age_days"] > 0 else "current"
            lines.append(
                f"  - {c['name']}: {_fmt_usd(c['total'])} ({_fmt_pct(c['total'], grand)}, {age_tag})"
            )

    # Per-rep totals
    if by_rep:
        lines.append("")
        lines.append("By rep:")
        sorted_reps = sorted(by_rep.values(), key=lambda r: -r["total"])
        for r in sorted_reps:
            slack_tag = "" if r["slack_id"] else " [no Slack ID in registry]"
            lines.append(
                f"  {r['rep_name']}: {_fmt_usd(r['total'])} "
                f"({_fmt_pct(r['total'], grand)}, {len(r['customers'])} customers){slack_tag}"
            )

    # Unassigned
    if unassigned > 0 or unassigned_n > 0:
        lines.append("")
        lines.append(
            f"Unassigned: {_fmt_usd(unassigned)} across {unassigned_n} customers. "
            "No SalesRep field or custom rep field populated in QBO — recommend Joanne tag these so the per-rep digest is complete next pull."
        )
        for name in summary["unassigned_customers"][:10]:
            cust = by_customer.get(name, {})
            lines.append(f"  - {name}: {_fmt_usd(cust.get('total', 0.0))}")
        if len(summary["unassigned_customers"]) > 10:
            lines.append(f"  ... and {len(summary['unassigned_customers']) - 10} more.")

    lines.append(_signoff())
    return "\n".join(lines)


def _try_voice(task_brief: str, data: dict[str, Any], audience: str, fallback: str) -> str:
    """Use voice.speak() if ANTHROPIC_API_KEY is set; else use deterministic fallback."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return fallback
    try:
        return voice.speak(task_brief, data, audience=audience) + _signoff()
    except Exception as e:
        logger.warning("voice.speak failed, using fallback: %s", e)
        return fallback


# --- Entrypoint ------------------------------------------------------------


def run_ar_aging_digest(as_of: date | None = None) -> dict[str, Any]:
    """Main entrypoint called by cron.

    Returns:
        {
          "per_rep_messages": {slack_id: text, ...},
          "matt_message": text,
          "summary": {grand_total, bucket_totals, rep_count, customer_count,
                      unassigned_total, week_over_week, ...},
          "warnings": [...],
        }
    """
    if as_of is None:
        as_of = datetime.now(tz=AZ_TZ).date()
    as_of_str = as_of.isoformat()

    warnings: list[str] = []

    # 1. Pull AR aging detail
    try:
        report = qbo.get_ar_aging_detail(as_of_date=as_of_str)
    except Exception as e:
        logger.exception("AR aging detail pull failed")
        return {
            "per_rep_messages": {},
            "matt_message": (
                f"AR Aging Digest could not run. QBO read failed: {e}. "
                f"Recommend checking the /data volume + QBO refresh token."
                + _signoff()
            ),
            "summary": {"error": str(e)},
            "warnings": [f"qbo_pull_failed: {e}"],
        }

    invoices = _parse_ar_detail_rows(report, as_of)

    # 2. Empty AR — short-circuit cleanly
    if not invoices:
        empty_msg = (
            "AR Aging Digest. "
            "No open invoices in QBO as of this pull. Total AR: $0.00. "
            "Either every invoice is collected or no invoices exist yet — confirm with Joanne if this looks wrong."
            + _signoff()
        )
        return {
            "per_rep_messages": {},
            "matt_message": empty_msg,
            "summary": {
                "grand_total": 0.0,
                "bucket_totals": _empty_buckets(),
                "rep_count": 0,
                "customer_count": 0,
                "unassigned_total": 0.0,
                "as_of": as_of_str,
            },
            "warnings": warnings,
        }

    # 3. Aggregate
    summary = _aggregate(invoices)

    # 4. Week-over-week delta
    last = _load_last_run()
    delta: dict[str, Any] | None = None
    if last:
        delta = {
            "grand_total": last.get("grand_total", 0.0),
            "as_of": last.get("as_of"),
        }

    # 5. Render messages
    per_rep_messages: dict[str, str] = {}
    per_rep_by_name: dict[str, str] = {}  # for observability / unassigned tracking

    for rep_name, rep_data in summary["by_rep"].items():
        if rep_data["total"] < MIN_FLAG_BALANCE:
            continue
        fallback = _render_rep_message_template(
            rep_name, rep_data, summary["grand_total"]
        )
        text = _try_voice(
            task_brief=(
                f"Write an AR aging digest DM to {rep_name} about their stuck customers. "
                "Coaching tone — what to do this week. Trades English, no jargon. "
                "Mention each customer with balance + oldest age. Flag anything over 60 days as a call-this-week item. "
                "No motivational language. No exclamation points. No emojis."
            ),
            data={
                "rep_name": rep_name,
                "total": rep_data["total"],
                "buckets": rep_data["buckets"],
                "customers": [
                    {
                        "name": c["name"],
                        "total": c["total"],
                        "oldest_age_days": c["oldest_age_days"],
                        "buckets": c["buckets"],
                    }
                    for c in rep_data["customers"].values()
                ],
                "grand_total_ar": summary["grand_total"],
            },
            audience="josh",  # closers/setters → trades English voice
            fallback=fallback,
        )
        slack_id = rep_data["slack_id"]
        if slack_id:
            per_rep_messages[slack_id] = text
        else:
            warnings.append(
                f"rep_no_slack_id: {rep_name} — text drafted but no delivery target"
            )
        per_rep_by_name[rep_name] = text

    matt_fallback = _render_matt_message_template(summary, delta)
    matt_message = _try_voice(
        task_brief=(
            "Write the AR Aging Digest summary for Matt (CFO). Accountant-precise. "
            "Lead with total AR, bucket breakdown with percents, concentration risk (>5%), "
            "per-rep totals, unassigned customers needing rep tags. "
            "Include week-over-week delta if prior data exists. "
            "No motivational language. No exclamation points. No emojis."
        ),
        data={
            "as_of": as_of_str,
            "grand_total": summary["grand_total"],
            "bucket_totals": summary["bucket_totals"],
            "by_rep_totals": {
                r["rep_name"]: {"total": r["total"], "n_customers": len(r["customers"])}
                for r in summary["by_rep"].values()
            },
            "top_customers": [
                {
                    "name": c["name"],
                    "total": c["total"],
                    "oldest_age_days": c["oldest_age_days"],
                }
                for c in sorted(summary["by_customer"].values(), key=lambda x: -x["total"])[:5]
            ],
            "unassigned_total": summary["unassigned_total"],
            "unassigned_customers": summary["unassigned_customers"][:10],
            "prior_run": delta,
        },
        audience="matt",
        fallback=matt_fallback,
    )

    # 6. Persist state for next week-over-week
    _save_last_run({
        "as_of": as_of_str,
        "grand_total": summary["grand_total"],
        "bucket_totals": summary["bucket_totals"],
        "saved_at": _now_stamp(),
    })

    return {
        "per_rep_messages": per_rep_messages,
        "per_rep_by_name": per_rep_by_name,  # for logs/observability — main.py won't send these
        "matt_message": matt_message,
        "summary": {
            "as_of": as_of_str,
            "grand_total": summary["grand_total"],
            "bucket_totals": summary["bucket_totals"],
            "rep_count": len(summary["by_rep"]),
            "customer_count": len(summary["by_customer"]),
            "unassigned_total": summary["unassigned_total"],
            "unassigned_customer_count": summary["unassigned_customer_count"],
            "week_over_week": delta,
            "warnings": warnings,
        },
        "warnings": warnings,
    }
