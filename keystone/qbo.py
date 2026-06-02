"""QBO client — passive reader pattern.

Reads OAuth tokens from /data/qbo_tokens.json (shared volume with relentless-ghl-agent's
webhook service). The webhook service is the primary token refresher; Keystone refreshes
only if it finds an expired access token and the webhook hasn't beat it to the punch.

Patterns match the GHL agent's QBO helpers so the same realm + same refresh token
work for both services.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

# --- Configuration ---------------------------------------------------------

QBO_CLIENT_ID = os.environ.get("QBO_CLIENT_ID", "")
QBO_CLIENT_SECRET = os.environ.get("QBO_CLIENT_SECRET", "")
QBO_ENVIRONMENT = os.environ.get("QBO_ENVIRONMENT", "production").lower()
QBO_REALM_ID = os.environ.get("QBO_REALM_ID", "9341455482460418")

INTUIT_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_API_BASE = (
    "https://sandbox-quickbooks.api.intuit.com"
    if QBO_ENVIRONMENT == "sandbox"
    else "https://quickbooks.api.intuit.com"
)

STATE_DIR = os.environ.get("STATE_DIR", "/data")
QBO_TOKEN_PATH = f"{STATE_DIR}/qbo_tokens.json"

# Refresh window: refresh access_token if it expires within this many seconds
ACCESS_TOKEN_REFRESH_BUFFER_SECS = 300  # 5 min

# --- Errors ----------------------------------------------------------------


class QBOError(Exception):
    """Raised when a QBO API call fails after retries."""

    def __init__(
        self, status: int, body: str, intuit_tid: str | None = None, message: str = ""
    ):
        self.status = status
        self.body = body
        self.intuit_tid = intuit_tid
        super().__init__(
            message
            or f"QBO API error {status} (intuit_tid={intuit_tid}): {body[:300]}"
        )


# --- Token storage ---------------------------------------------------------


def _qbo_token_load() -> dict[str, Any]:
    """Read the token file written by the webhook service."""
    path = Path(QBO_TOKEN_PATH)
    if not path.exists():
        raise QBOError(
            0,
            f"qbo_tokens.json not found at {QBO_TOKEN_PATH}",
            None,
            "QBO token file missing — is the /data volume mounted?",
        )
    with open(path, "r") as f:
        return json.load(f)


def _qbo_token_save(tokens: dict[str, Any]) -> None:
    """Write tokens back atomically (used only if Keystone refreshes)."""
    path = Path(QBO_TOKEN_PATH)
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(tokens, f, indent=2)
    tmp_path.replace(path)


def _qbo_basic_auth_header() -> str:
    creds = f"{QBO_CLIENT_ID}:{QBO_CLIENT_SECRET}".encode("utf-8")
    return f"Basic {base64.b64encode(creds).decode('utf-8')}"


def _qbo_refresh(tokens: dict[str, Any]) -> dict[str, Any]:
    """Exchange refresh_token for a fresh access_token + rotated refresh_token."""
    resp = httpx.post(
        INTUIT_TOKEN_URL,
        headers={
            "Accept": "application/json",
            "Authorization": _qbo_basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        },
        timeout=20.0,
    )
    if resp.status_code != 200:
        raise QBOError(
            resp.status_code,
            resp.text,
            resp.headers.get("intuit_tid"),
            f"Token refresh failed: {resp.text[:300]}",
        )
    new = resp.json()
    # Intuit returns expires_in (seconds). Convert to absolute timestamp.
    tokens["access_token"] = new["access_token"]
    tokens["refresh_token"] = new.get("refresh_token", tokens["refresh_token"])
    tokens["access_token_expires_at"] = int(time.time()) + int(new["expires_in"])
    tokens["x_refresh_token_expires_at"] = int(time.time()) + int(
        new.get("x_refresh_token_expires_in", 8726400)  # ~100 days
    )
    return tokens


def get_qbo_access_token() -> str:
    """Return a valid access_token. Refreshes if expired or near-expiry.

    Note: under the shared-volume pattern, the webhook service is the primary
    refresher. Keystone refreshes only if it finds the token already expired —
    a defensive fallback in case the webhook hasn't fired recently.
    """
    tokens = _qbo_token_load()
    now = int(time.time())
    expires_at = int(tokens.get("access_token_expires_at", 0))

    if expires_at - now < ACCESS_TOKEN_REFRESH_BUFFER_SECS:
        tokens = _qbo_refresh(tokens)
        _qbo_token_save(tokens)

    return tokens["access_token"]


# --- API helpers -----------------------------------------------------------


def _qbo_request(
    method: str,
    path: str,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    *,
    max_retries: int = 1,
) -> dict[str, Any]:
    """Make a QBO API call. Handles 401 by refreshing + retrying once."""
    url = f"{QBO_API_BASE}/v3/company/{QBO_REALM_ID}{path}"
    attempt = 0
    last_exc: Exception | None = None

    while attempt <= max_retries:
        token = get_qbo_access_token()
        try:
            resp = httpx.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=json_body,
                params=params,
                timeout=30.0,
            )
        except httpx.HTTPError as e:
            last_exc = e
            attempt += 1
            continue

        if resp.status_code == 401 and attempt < max_retries:
            # Token rejected — force refresh by zeroing expiry, then retry
            tokens = _qbo_token_load()
            tokens["access_token_expires_at"] = 0
            _qbo_token_save(tokens)
            attempt += 1
            continue

        if resp.status_code >= 400:
            raise QBOError(
                resp.status_code,
                resp.text,
                resp.headers.get("intuit_tid"),
            )

        return resp.json()

    if last_exc:
        raise QBOError(0, str(last_exc), None, f"QBO request failed: {last_exc}")
    raise QBOError(0, "exhausted retries", None)


def _qbo_escape(value: str) -> str:
    """Escape single quotes for QBO Query Language."""
    return value.replace("'", "\\'")


def qbo_query(query: str) -> dict[str, Any]:
    """Run a QBO Query Language SELECT statement."""
    return _qbo_request("GET", "/query", params={"query": query, "minorversion": "73"})


# --- High-level reads (the Keystone surface area) -------------------------


def get_company_info() -> dict[str, Any]:
    """Sanity check: returns CompanyName, LegalName, etc."""
    return _qbo_request("GET", f"/companyinfo/{QBO_REALM_ID}", params={"minorversion": "73"})


def get_balance_sheet(as_of_date: str | None = None) -> dict[str, Any]:
    """Balance sheet report. as_of_date YYYY-MM-DD; defaults to today."""
    params: dict[str, Any] = {"minorversion": "73", "accounting_method": "Accrual"}
    if as_of_date:
        params["start_date"] = as_of_date
        params["end_date"] = as_of_date
    return _qbo_request("GET", "/reports/BalanceSheet", params=params)


def get_ar_aging_summary(as_of_date: str | None = None) -> dict[str, Any]:
    """AR aging summary (buckets: current, 1-30, 31-60, 61-90, 91+)."""
    params: dict[str, Any] = {"minorversion": "73"}
    if as_of_date:
        params["report_date"] = as_of_date
    return _qbo_request("GET", "/reports/AgedReceivables", params=params)


def get_ar_aging_detail(as_of_date: str | None = None) -> dict[str, Any]:
    """AR aging detail — every open invoice with customer, age, balance."""
    params: dict[str, Any] = {"minorversion": "73"}
    if as_of_date:
        params["report_date"] = as_of_date
    return _qbo_request("GET", "/reports/AgedReceivableDetail", params=params)


def get_profit_loss(
    start_date: str, end_date: str, accounting_method: str = "Accrual"
) -> dict[str, Any]:
    """P&L for a date range. Dates YYYY-MM-DD."""
    return _qbo_request(
        "GET",
        "/reports/ProfitAndLoss",
        params={
            "minorversion": "73",
            "start_date": start_date,
            "end_date": end_date,
            "accounting_method": accounting_method,
        },
    )


def get_invoices_for_date(date_str: str) -> dict[str, Any]:
    """All invoices with TxnDate = the given date (the accounting/service date).

    WARNING: TxnDate is frequently backdated at Relentless (Joanne dates invoices
    to install date or month-end). So this can sweep up old invoices that merely
    carry this date. For 'what actually happened on day X', use
    get_invoices_created_on_date() instead.
    """
    q = (
        f"SELECT * FROM Invoice WHERE TxnDate = '{_qbo_escape(date_str)}' "
        f"MAXRESULTS 1000"
    )
    return qbo_query(q)


def get_invoices_created_on_date(date_str: str) -> dict[str, Any]:
    """All invoices actually CREATED on a date (by MetaData.CreateTime).

    This is 'new bills we genuinely issued that day' — immune to TxnDate
    backdating. CreateTime is a datetime, so we bound it to the full day.
    """
    start = f"{date_str}T00:00:00"
    end = f"{date_str}T23:59:59"
    q = (
        f"SELECT * FROM Invoice "
        f"WHERE MetaData.CreateTime >= '{_qbo_escape(start)}' "
        f"AND MetaData.CreateTime <= '{_qbo_escape(end)}' "
        f"MAXRESULTS 1000"
    )
    return qbo_query(q)


def get_payments_for_date(date_str: str) -> dict[str, Any]:
    """All customer payments received on a date (TxnDate basis).

    This is 'money customers paid us that day' — the closest QBO proxy for
    cash collected. Note: still subject to QBO bank-feed sync lag vs the
    actual bank. For true bank deposits, pair with Ramp/Chase direct (Phase 2).
    """
    q = (
        f"SELECT * FROM Payment WHERE TxnDate = '{_qbo_escape(date_str)}' "
        f"MAXRESULTS 1000"
    )
    return qbo_query(q)


def get_deposits_for_date(date_str: str) -> dict[str, Any]:
    """All bank deposits recorded on a date (TxnDate basis).

    Deposit entities are money landing in a bank account in QBO. Combined with
    payments, gives a fuller 'money in' picture.
    """
    q = (
        f"SELECT * FROM Deposit WHERE TxnDate = '{_qbo_escape(date_str)}' "
        f"MAXRESULTS 1000"
    )
    return qbo_query(q)


def get_open_invoices(limit: int = 1000) -> dict[str, Any]:
    """All invoices with outstanding balance."""
    q = (
        f"SELECT * FROM Invoice WHERE Balance > '0' "
        f"ORDERBY DueDate ASC MAXRESULTS {limit}"
    )
    return qbo_query(q)
