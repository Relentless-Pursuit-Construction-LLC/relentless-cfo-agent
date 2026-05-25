"""QBO OAuth 2.0 flow — one-time setup to obtain a refresh token.

Flow:
  1. Josh hits /qbo/connect?secret=<ADMIN_SECRET>
  2. Keystone redirects him to Intuit's authorization URL
  3. Josh signs in, picks Relentless QBO file, grants access
  4. Intuit redirects to /qbo/callback?code=...&realmId=...&state=...
  5. Keystone exchanges the code for access + refresh tokens
  6. Tokens written to /data/qbo_tokens.json
  7. Subsequent API calls use get_qbo_access_token() from qbo.py

After this one-time dance, Keystone refreshes the access token automatically
every 50 min (well under the 60-min expiry).
"""

from __future__ import annotations

import os
import secrets
import time
import urllib.parse
from typing import Any

import httpx

from keystone.qbo import (
    INTUIT_TOKEN_URL,
    QBO_CLIENT_ID,
    QBO_CLIENT_SECRET,
    _qbo_basic_auth_header,
    _qbo_token_save,
)

# Authorization endpoint (separate from the token endpoint)
INTUIT_AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"

# Scopes we need. accounting covers balance sheet, P&L, AR/AP, invoices, customers,
# vendors, bills — everything Keystone reads. We do NOT request com.intuit.quickbooks.payment
# (no payment processing) or payroll (using Ramp for that).
QBO_SCOPES = "com.intuit.quickbooks.accounting"

# In-memory state cache (CSRF protection on the OAuth round-trip).
# Single instance, restarts wipe — that's fine, OAuth is rare.
_state_cache: dict[str, float] = {}
_STATE_TTL_SECS = 600  # state expires after 10 min


def _purge_expired_states() -> None:
    now = time.time()
    for state, created in list(_state_cache.items()):
        if now - created > _STATE_TTL_SECS:
            del _state_cache[state]


def build_authorize_url(redirect_uri: str) -> str:
    """Build the Intuit authorization URL. Returns URL Josh navigates to."""
    _purge_expired_states()
    state = secrets.token_urlsafe(24)
    _state_cache[state] = time.time()

    params = {
        "client_id": QBO_CLIENT_ID,
        "response_type": "code",
        "scope": QBO_SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{INTUIT_AUTH_URL}?{urllib.parse.urlencode(params)}"


def consume_state(state: str) -> bool:
    """Validate the state parameter on the OAuth callback. Single-use."""
    _purge_expired_states()
    if state in _state_cache:
        del _state_cache[state]
        return True
    return False


def exchange_code_for_tokens(
    code: str, realm_id: str, redirect_uri: str
) -> dict[str, Any]:
    """Exchange the authorization code for access + refresh tokens.
    Writes tokens to /data/qbo_tokens.json on success.
    """
    resp = httpx.post(
        INTUIT_TOKEN_URL,
        headers={
            "Accept": "application/json",
            "Authorization": _qbo_basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=20.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed ({resp.status_code}): {resp.text[:300]}"
        )

    payload = resp.json()
    now = int(time.time())
    tokens = {
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "access_token_expires_at": now + int(payload["expires_in"]),
        "x_refresh_token_expires_at": now + int(
            payload.get("x_refresh_token_expires_in", 8726400)
        ),
        "realm_id": realm_id,
        "token_type": payload.get("token_type", "Bearer"),
        "issued_at": now,
    }
    _qbo_token_save(tokens)
    return {
        "realm_id": realm_id,
        "access_token_expires_at": tokens["access_token_expires_at"],
        "x_refresh_token_expires_at": tokens["x_refresh_token_expires_at"],
    }
