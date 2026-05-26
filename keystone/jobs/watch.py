"""The Watch — hourly anomaly detection on QBO transactions.

Polls QBO Purchase / Deposit / Transfer entities for activity since the
last-checked timestamp. Evaluates each transaction against a list of
rules and classifies findings into critical / important / informational.

Persona note: Keystone is calm. False positives at 2 AM destroy trust,
so the critical bar is intentionally high (negative cash, NSF text, or
operating cash below the $50K escalation threshold). Everything softer
falls to Slack DM (important) or logs (informational).

Read-only on QBO. Only writes are to /data/watch_state.json (our state).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from keystone import qbo

# --- Configuration ---------------------------------------------------------

STATE_DIR = os.environ.get("STATE_DIR", "/data")
WATCH_STATE_PATH = f"{STATE_DIR}/watch_state.json"

# Escalation thresholds — match docs/KEYSTONE_SYSTEM_PROMPT.md "Escalation triggers"
OPERATING_CASH_FLOOR = 50_000.00  # Critical SMS threshold
LARGE_OUTFLOW = 10_000.00         # Important
LARGE_DEPOSIT = 25_000.00         # Important
NEW_VENDOR_FRAUD = 1_000.00       # Critical (fraud signal)
LARGE_FEE = 100.00                # Important
CASH_SWING_INFO = 5_000.00        # Informational

# Duplicate-charge window
DUPLICATE_WINDOW_DAYS = 7

# Recent-transaction memory size (for dupe detection)
RECENT_TX_MEMORY = 50

# Quiet hours (local MT, 24h). Outside this window, only critical alerts
# get queued for delivery; non-critical defer to morning Pulse.
QUIET_START_HOUR = 21  # 9 PM
QUIET_END_HOUR = 6     # 6 AM

# Audiences — same identifiers used elsewhere; main.py wires real Slack IDs.
SMS_AUDIENCE_CRITICAL = ["josh", "matt"]
SMS_AUDIENCE_NSF = ["josh", "matt", "joanne"]
SMS_AUDIENCE_FRAUD = ["matt", "joanne"]
SLACK_AUDIENCE_IMPORTANT = ["matt"]

# NSF / overdraft signal text — case-insensitive
NSF_PATTERNS = [
    r"\bnsf\b",
    r"\boverdraft\b",
    r"\breturned\s+check\b",
    r"\binsufficient\s+funds\b",
    r"\bnon-?sufficient\b",
]
WIRE_PATTERN = re.compile(r"\bwire\s+(transfer|out|outgoing)\b", re.IGNORECASE)
FEE_PATTERN = re.compile(r"\b(fee|service\s+charge|maint(?:enance)?)\b", re.IGNORECASE)

# Arizona + Utah city/state hints. Anything that explicitly names another
# state is treated as out-of-state for fraud purposes. Conservative — we
# only flag when we see a clear non-AZ/UT state token.
HOME_STATE_TOKENS = {"AZ", "ARIZONA", "UT", "UTAH"}
US_STATE_TOKENS = {
    "AL", "AK", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR",
    "PA", "RI", "SC", "SD", "TN", "TX", "VT", "VA", "WA", "WV", "WI", "WY",
}


# --- State file ------------------------------------------------------------


def _state_load() -> dict[str, Any]:
    """Read /data/watch_state.json. Returns empty defaults on first run."""
    path = Path(WATCH_STATE_PATH)
    if not path.exists():
        return {"last_checked_at": None, "recent_transactions": []}
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        # Corrupt or unreadable — treat as first run rather than crash
        return {"last_checked_at": None, "recent_transactions": []}
    data.setdefault("last_checked_at", None)
    data.setdefault("recent_transactions", [])
    return data


def _state_save(state: dict[str, Any]) -> None:
    """Write state atomically."""
    path = Path(WATCH_STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    tmp_path.replace(path)


# --- Helpers ---------------------------------------------------------------


def _now_az() -> datetime:
    """Current time as a naive datetime in Arizona local (MST, UTC-7, no DST)."""
    return datetime.now(timezone.utc) - timedelta(hours=7)


def _fmt_az_timestamp(dt: datetime) -> str:
    """Format for Keystone sign-off."""
    return dt.strftime("%Y-%m-%d %H:%M AZ")


def _is_quiet_hours(local_dt: datetime) -> bool:
    h = local_dt.hour
    if QUIET_START_HOUR <= 23:
        return h >= QUIET_START_HOUR or h < QUIET_END_HOUR
    return QUIET_END_HOUR <= h < QUIET_START_HOUR


def _txn_amount(tx: dict[str, Any]) -> float:
    """Pull a numeric amount out of a QBO entity. Defensive on missing keys."""
    for key in ("TotalAmt", "Amount"):
        v = tx.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _txn_vendor(tx: dict[str, Any]) -> str:
    """Pull the counterparty name. Different entities use different fields."""
    for key in ("EntityRef", "VendorRef", "CustomerRef", "PaymentMethodRef"):
        ref = tx.get(key) or {}
        name = ref.get("name") if isinstance(ref, dict) else None
        if name:
            return str(name)
    # Deposits sometimes carry the name on Line[i].DepositLineDetail.Entity.name
    lines = tx.get("Line") or []
    for line in lines:
        det = line.get("DepositLineDetail") or {}
        ent = det.get("Entity") or {}
        if isinstance(ent, dict) and ent.get("name"):
            return str(ent["name"])
    return ""


def _txn_memo(tx: dict[str, Any]) -> str:
    parts = []
    for key in ("PrivateNote", "Memo", "Description"):
        v = tx.get(key)
        if v:
            parts.append(str(v))
    return " ".join(parts)


def _txn_date(tx: dict[str, Any]) -> str:
    return str(tx.get("TxnDate") or "")


def _txn_id(tx: dict[str, Any]) -> str:
    eid = tx.get("Id") or ""
    etype = tx.get("_entity") or tx.get("domain") or "Txn"
    return f"{etype}:{eid}"


def _txn_state_hint(tx: dict[str, Any]) -> str:
    """Look for a US state token in address / memo fields. Empty if none found."""
    haystack_parts: list[str] = []
    for key in ("BillAddr", "ShipAddr"):
        addr = tx.get(key) or {}
        if isinstance(addr, dict):
            for v in addr.values():
                if v:
                    haystack_parts.append(str(v))
    haystack_parts.append(_txn_memo(tx))
    haystack = " ".join(haystack_parts).upper()
    tokens = set(re.findall(r"\b[A-Z]{2}\b", haystack))
    foreign = (tokens & US_STATE_TOKENS) - HOME_STATE_TOKENS
    if foreign:
        return sorted(foreign)[0]
    return ""


# --- Rules -----------------------------------------------------------------
#
# Each rule is a callable: (tx, context) -> anomaly dict or None.
# Context carries cross-tx info (recent_transactions, known_vendors, cash).
# Keep rules small + testable.


def _rule_nsf(tx: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
    memo = _txn_memo(tx).lower()
    for pat in NSF_PATTERNS:
        if re.search(pat, memo):
            return {
                "severity": "critical",
                "type": "nsf_or_overdraft",
                "details": {
                    "txn_id": _txn_id(tx),
                    "amount": _txn_amount(tx),
                    "memo": _txn_memo(tx)[:200],
                    "date": _txn_date(tx),
                },
                "message": (
                    f"NSF / overdraft signal on {_txn_date(tx)} "
                    f"({_txn_id(tx)}, ${_txn_amount(tx):,.2f}). "
                    f"Confirm with Joanne and Chase."
                ),
                "audience_sms": SMS_AUDIENCE_NSF,
            }
    return None


def _rule_negative_cash(_tx: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
    cash = ctx.get("operating_cash")
    if cash is None:
        return None
    if cash < 0:
        return {
            "severity": "critical",
            "type": "negative_cash",
            "details": {"operating_cash": cash},
            "message": (
                f"Operating cash is negative (${cash:,.2f}). "
                f"Stop outbound payments and reconcile with Chase before next clearing cycle."
            ),
            "audience_sms": SMS_AUDIENCE_NSF,
        }
    return None


def _rule_cash_below_floor(_tx: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
    cash = ctx.get("operating_cash")
    if cash is None:
        return None
    if 0 <= cash < OPERATING_CASH_FLOOR:
        return {
            "severity": "critical",
            "type": "cash_below_floor",
            "details": {
                "operating_cash": cash,
                "floor": OPERATING_CASH_FLOOR,
            },
            "message": (
                f"Operating cash ${cash:,.2f} is below the $50K floor. "
                f"Recommend pausing discretionary AP until next deposit clears."
            ),
            "audience_sms": SMS_AUDIENCE_CRITICAL,
        }
    return None


def _rule_fraud_new_vendor(tx: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
    if tx.get("_entity") != "Purchase":
        return None
    amount = _txn_amount(tx)
    if amount < NEW_VENDOR_FRAUD:
        return None
    vendor = _txn_vendor(tx)
    if not vendor:
        return None
    known = ctx.get("known_vendors") or set()
    if vendor in known:
        return None
    state_hint = _txn_state_hint(tx)
    out_of_state = bool(state_hint)
    if not out_of_state:
        return None  # require both signals — unknown vendor + out-of-state — for critical
    return {
        "severity": "critical",
        "type": "fraud_unknown_vendor_out_of_state",
        "details": {
            "txn_id": _txn_id(tx),
            "vendor": vendor,
            "amount": amount,
            "state_hint": state_hint,
            "date": _txn_date(tx),
        },
        "message": (
            f"Unknown vendor first payment ${amount:,.2f} to {vendor} "
            f"with {state_hint} address. Recommend locking Ramp card until verified."
        ),
        "audience_sms": SMS_AUDIENCE_FRAUD,
    }


def _rule_large_outflow(tx: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
    if tx.get("_entity") != "Purchase":
        return None
    amount = _txn_amount(tx)
    if amount <= LARGE_OUTFLOW:
        return None
    return {
        "severity": "important",
        "type": "large_outflow",
        "details": {
            "txn_id": _txn_id(tx),
            "vendor": _txn_vendor(tx),
            "amount": amount,
            "date": _txn_date(tx),
        },
        "message": (
            f"Large outflow ${amount:,.2f} to {_txn_vendor(tx) or 'unknown vendor'} "
            f"on {_txn_date(tx)}. Confirm this was scheduled."
        ),
        "audience_slack": SLACK_AUDIENCE_IMPORTANT,
    }


def _rule_large_deposit(tx: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
    if tx.get("_entity") != "Deposit":
        return None
    amount = _txn_amount(tx)
    if amount <= LARGE_DEPOSIT:
        return None
    return {
        "severity": "important",
        "type": "large_deposit",
        "details": {
            "txn_id": _txn_id(tx),
            "amount": amount,
            "date": _txn_date(tx),
        },
        "message": (
            f"Large deposit ${amount:,.2f} on {_txn_date(tx)}. "
            f"Joanne to confirm source and tag to the correct job."
        ),
        "audience_slack": SLACK_AUDIENCE_IMPORTANT,
    }


def _rule_duplicate_charge(tx: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
    if tx.get("_entity") != "Purchase":
        return None
    amount = _txn_amount(tx)
    if amount <= 0:
        return None
    vendor = _txn_vendor(tx)
    if not vendor:
        return None
    date_str = _txn_date(tx)
    try:
        this_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    for prior in ctx.get("recent_transactions") or []:
        if prior.get("txn_id") == _txn_id(tx):
            continue
        if prior.get("vendor") != vendor:
            continue
        if abs(float(prior.get("amount", 0)) - amount) > 0.01:
            continue
        try:
            prior_date = datetime.strptime(prior.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if abs((this_date - prior_date).days) <= DUPLICATE_WINDOW_DAYS:
            return {
                "severity": "important",
                "type": "duplicate_charge",
                "details": {
                    "txn_id": _txn_id(tx),
                    "prior_txn_id": prior.get("txn_id"),
                    "vendor": vendor,
                    "amount": amount,
                    "date": date_str,
                    "prior_date": prior.get("date"),
                },
                "message": (
                    f"Possible duplicate: ${amount:,.2f} to {vendor} on {date_str} "
                    f"matches prior charge on {prior.get('date')}. Confirm with Joanne."
                ),
                "audience_slack": SLACK_AUDIENCE_IMPORTANT,
            }
    return None


def _rule_outgoing_wire(tx: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
    if tx.get("_entity") not in ("Purchase", "Transfer"):
        return None
    memo = _txn_memo(tx)
    if not WIRE_PATTERN.search(memo):
        return None
    return {
        "severity": "important",
        "type": "outgoing_wire",
        "details": {
            "txn_id": _txn_id(tx),
            "amount": _txn_amount(tx),
            "vendor": _txn_vendor(tx),
            "memo": memo[:200],
            "date": _txn_date(tx),
        },
        "message": (
            f"Outgoing wire ${_txn_amount(tx):,.2f} to {_txn_vendor(tx) or 'unknown'} "
            f"on {_txn_date(tx)}. Wires are rare here — confirm authorization."
        ),
        "audience_slack": SLACK_AUDIENCE_IMPORTANT,
    }


def _rule_unusual_fee(tx: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
    if tx.get("_entity") != "Purchase":
        return None
    amount = _txn_amount(tx)
    if amount <= LARGE_FEE:
        return None
    memo = _txn_memo(tx)
    vendor = _txn_vendor(tx)
    if not (FEE_PATTERN.search(memo) or FEE_PATTERN.search(vendor)):
        return None
    return {
        "severity": "important",
        "type": "unusual_fee",
        "details": {
            "txn_id": _txn_id(tx),
            "amount": amount,
            "vendor": vendor,
            "memo": memo[:200],
            "date": _txn_date(tx),
        },
        "message": (
            f"Bank or service fee ${amount:,.2f} on {_txn_date(tx)}. "
            f"Verify this is expected — single fees over $100 are unusual."
        ),
        "audience_slack": SLACK_AUDIENCE_IMPORTANT,
    }


def _rule_new_vendor_informational(tx: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
    if tx.get("_entity") != "Purchase":
        return None
    vendor = _txn_vendor(tx)
    if not vendor:
        return None
    known = ctx.get("known_vendors") or set()
    if vendor in known:
        return None
    return {
        "severity": "informational",
        "type": "new_vendor",
        "details": {
            "txn_id": _txn_id(tx),
            "vendor": vendor,
            "amount": _txn_amount(tx),
            "date": _txn_date(tx),
        },
        "message": (
            f"New vendor first transaction: {vendor} ${_txn_amount(tx):,.2f} "
            f"on {_txn_date(tx)}."
        ),
    }


def _rule_cash_swing(_tx: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
    """One-shot informational — checked once per run, not per-tx. See _run_global_rules."""
    return None


def _rule_individual_account_negative(_tx: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
    """Flag any individual bank account with a negative balance.

    Aggregate operating cash can be positive while a single account is overdrawn.
    Real example from 2026-05-25: PERFBUS CHK was -$8,852 while total cash was $109K.
    This rule catches that — _rule_negative_cash and _rule_cash_below_floor would miss it.

    Marked CRITICAL because an overdrawn account triggers bank fees + signals
    cash management issue. Surfaces every negative account in a single finding.
    """
    accounts = ctx.get("cash_accounts") or []
    negatives = [a for a in accounts if isinstance(a, dict) and (a.get("balance") or 0) < 0]
    if not negatives:
        return None
    lines = [
        f"  - {a.get('name', 'unknown')}: ${a.get('balance', 0):,.2f}"
        for a in negatives
    ]
    return {
        "severity": "critical",
        "type": "individual_account_negative",
        "details": {
            "negative_accounts": negatives,
            "count": len(negatives),
        },
        "message": (
            f"{len(negatives)} bank account(s) overdrawn:\n"
            + "\n".join(lines)
            + "\n\nReview with Matt today. Overdraft fees + sequencing risk."
        ),
    }


# Per-transaction rules — order does not matter; we collect all matches.
_RULES: list[Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None]] = [
    _rule_nsf,
    _rule_fraud_new_vendor,
    _rule_large_outflow,
    _rule_large_deposit,
    _rule_duplicate_charge,
    _rule_outgoing_wire,
    _rule_unusual_fee,
    _rule_new_vendor_informational,
]


# --- QBO fetch -------------------------------------------------------------


def _qbo_since_clause(last_checked_at: str | None) -> str:
    """Build a 'WHERE MetaData.LastUpdatedTime >= ...' clause.

    last_checked_at: ISO timestamp (UTC). If None, look back 2 hours so the
    very first run still produces meaningful coverage without flooding.
    """
    if last_checked_at:
        ts = last_checked_at
    else:
        ts = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S-00:00")
    return f"WHERE MetaData.LastUpdatedTime >= '{ts}'"


def _fetch_recent_transactions(last_checked_at: str | None) -> list[dict[str, Any]]:
    """Pull Purchase / Deposit / Transfer entities since last_checked_at."""
    where = _qbo_since_clause(last_checked_at)
    entities = ["Purchase", "Deposit", "Transfer"]
    out: list[dict[str, Any]] = []
    for entity in entities:
        q = f"SELECT * FROM {entity} {where} MAXRESULTS 200"
        try:
            resp = qbo.qbo_query(q)
        except qbo.QBOError:
            # Stale or unavailable — surface as zero pulls for that entity rather than crash
            continue
        rows = (resp.get("QueryResponse") or {}).get(entity) or []
        for row in rows:
            row["_entity"] = entity
            out.append(row)
    return out


def _fetch_operating_cash() -> float | None:
    """Sum bank-type accounts from the balance sheet. Returns None on failure."""
    try:
        bs = qbo.get_balance_sheet()
    except qbo.QBOError:
        return None

    total = 0.0
    found = False

    def walk(node: Any) -> None:
        nonlocal total, found
        if isinstance(node, dict):
            # QBO balance sheet rows carry a "group" of "BankAccounts" or
            # individual Account rows with AccountType "Bank".
            if node.get("group") == "BankAccounts":
                summary = node.get("Summary") or {}
                cols = summary.get("ColData") or []
                for col in cols:
                    val = col.get("value", "")
                    try:
                        total += float(val.replace(",", ""))
                        found = True
                        return
                    except (ValueError, AttributeError):
                        continue
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(bs)
    return total if found else None


def _fetch_individual_cash_accounts() -> list[dict[str, Any]]:
    """Return per-account bank balances. Reuses Pulse's BalanceSheet walker.

    Returns list of {name, balance} dicts. Empty list on failure (so the rule
    that consumes it becomes a no-op rather than firing false positives).
    """
    try:
        from keystone.jobs.pulse import extract_cash_position
        bs = qbo.get_balance_sheet()
        cash_info = extract_cash_position(bs)
        return cash_info.get("accounts") or []
    except Exception:
        return []


def _fetch_known_vendors() -> set[str]:
    """List of every vendor name already in QBO (active or otherwise)."""
    try:
        resp = qbo.qbo_query("SELECT DisplayName FROM Vendor MAXRESULTS 1000")
    except qbo.QBOError:
        return set()
    rows = (resp.get("QueryResponse") or {}).get("Vendor") or []
    return {str(r.get("DisplayName")) for r in rows if r.get("DisplayName")}


# --- Orchestration ---------------------------------------------------------


def _run_per_transaction_rules(
    transactions: list[dict[str, Any]], ctx: dict[str, Any]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for tx in transactions:
        for rule in _RULES:
            try:
                hit = rule(tx, ctx)
            except Exception as e:
                # A misbehaving rule should not take down the whole run
                hit = {
                    "severity": "informational",
                    "type": "rule_error",
                    "details": {"rule": rule.__name__, "error": str(e)},
                    "message": f"Rule {rule.__name__} raised: {e}",
                }
            if hit:
                findings.append(hit)
    return findings


def _run_global_rules(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    # NOTE: _rule_individual_account_negative is temporarily disabled — it was
    # firing every hour because PERFBUS CHK remains in a persistent negative
    # state (likely owner-draw pattern). Once dedup logic is added OR Matt
    # writes the PERFBUS pattern into KNOWLEDGE.md, re-enable.
    for rule in (_rule_negative_cash, _rule_cash_below_floor):
        try:
            hit = rule({}, ctx)
        except Exception as e:
            hit = {
                "severity": "informational",
                "type": "rule_error",
                "details": {"rule": rule.__name__, "error": str(e)},
                "message": f"Rule {rule.__name__} raised: {e}",
            }
        if hit:
            findings.append(hit)

    # Cash swing informational — needs prior reading from state.
    prior_cash = ctx.get("prior_operating_cash")
    current_cash = ctx.get("operating_cash")
    if prior_cash is not None and current_cash is not None:
        delta = current_cash - prior_cash
        if abs(delta) >= CASH_SWING_INFO:
            findings.append({
                "severity": "informational",
                "type": "cash_swing",
                "details": {
                    "prior": prior_cash,
                    "current": current_cash,
                    "delta": delta,
                },
                "message": (
                    f"Cash position moved ${delta:+,.2f} since last check "
                    f"(${prior_cash:,.2f} -> ${current_cash:,.2f})."
                ),
            })
    return findings


def _build_messages(
    findings: list[dict[str, Any]], quiet: bool, as_of_az: datetime
) -> dict[str, Any]:
    """Translate findings into the dict of messages that *would* be sent.

    Keystone voice. No motivational language, no emoji, no exclamations.
    Signed off with timestamp.
    """
    signoff = f"\n\n— Keystone\nData pulled: {_fmt_az_timestamp(as_of_az)}"

    sms: list[dict[str, Any]] = []
    slack: list[dict[str, Any]] = []

    for f in findings:
        if f["severity"] == "critical":
            sms.append({
                "audience": f.get("audience_sms") or SMS_AUDIENCE_CRITICAL,
                "type": f["type"],
                "text": f["message"] + signoff,
            })
        elif f["severity"] == "important":
            if quiet:
                # Defer non-critical during quiet hours; log it but don't send.
                continue
            slack.append({
                "audience": f.get("audience_slack") or SLACK_AUDIENCE_IMPORTANT,
                "type": f["type"],
                "text": f["message"] + signoff,
            })
        # informational — never delivered; logs only.

    return {"sms": sms, "slack": slack, "deferred_during_quiet_hours": quiet}


def run_watch(as_of: datetime | None = None) -> dict[str, Any]:
    """Hourly anomaly sweep. Read-only on QBO; updates /data/watch_state.json.

    Returns the structured result so the caller (FastAPI endpoint or test)
    can inspect what was found and what would have been sent.
    """
    az_now = (as_of - timedelta(hours=7)) if (as_of and as_of.tzinfo) else _now_az()
    quiet = _is_quiet_hours(az_now)

    state = _state_load()
    last_checked_at = state.get("last_checked_at")
    recent_transactions = state.get("recent_transactions") or []
    prior_operating_cash = state.get("operating_cash")

    # Pull the data
    transactions = _fetch_recent_transactions(last_checked_at)
    operating_cash = _fetch_operating_cash()
    known_vendors = _fetch_known_vendors()
    cash_accounts = _fetch_individual_cash_accounts()

    ctx: dict[str, Any] = {
        "operating_cash": operating_cash,
        "prior_operating_cash": prior_operating_cash,
        "known_vendors": known_vendors,
        "recent_transactions": recent_transactions,
        "cash_accounts": cash_accounts,
    }

    # Evaluate
    findings = _run_per_transaction_rules(transactions, ctx)
    findings.extend(_run_global_rules(ctx))

    # Bucket
    by_severity: dict[str, list[dict[str, Any]]] = {
        "critical": [],
        "important": [],
        "informational": [],
    }
    for f in findings:
        sev = f.get("severity", "informational")
        by_severity.setdefault(sev, []).append(f)

    messages_to_send = _build_messages(findings, quiet, az_now)

    # Persist new state: bump cursor + add tx IDs to dupe memory + record cash.
    new_recent = list(recent_transactions)
    for tx in transactions:
        new_recent.append({
            "txn_id": _txn_id(tx),
            "vendor": _txn_vendor(tx),
            "amount": _txn_amount(tx),
            "date": _txn_date(tx),
        })
    # Keep the last N to bound the file.
    new_recent = new_recent[-RECENT_TX_MEMORY:]

    new_state = {
        "last_checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-00:00"),
        "recent_transactions": new_recent,
        "operating_cash": operating_cash,
    }
    try:
        _state_save(new_state)
    except OSError as e:
        # State persistence is best-effort — log into the result, don't crash.
        by_severity["informational"].append({
            "severity": "informational",
            "type": "state_write_failed",
            "details": {"error": str(e)},
            "message": f"watch_state.json write failed: {e}",
        })

    return {
        "as_of": _fmt_az_timestamp(az_now),
        "quiet_hours": quiet,
        "transactions_scanned": len(transactions),
        "operating_cash": operating_cash,
        "critical": by_severity["critical"],
        "important": by_severity["important"],
        "informational": by_severity["informational"],
        "messages_to_send": messages_to_send,
    }
