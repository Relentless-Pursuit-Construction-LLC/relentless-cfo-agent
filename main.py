"""Keystone — FastAPI entrypoint.

Endpoint surface:
  GET  /health                  — liveness probe
  GET  /qbo/connect             — start OAuth (Josh navigates here once, query-secret gated)
  GET  /qbo/callback            — Intuit redirects here with auth code
  POST /admin/sanity-pull       — manual QBO sanity check (Bearer-gated)
  POST /cron/ar-aging-digest    — AR digest (Mon-Sat 6:30 AM MT)
  POST /cron/pulse              — daily cash heartbeat (6:32 AM MT daily)
  POST /cron/watch              — hourly anomaly sweep (6 AM – 10 PM MT)
  POST /cron/audit              — weekly audit (Mon 7:00 AM MT)
  POST /cron/counsel            — monthly walkthrough (5th of month 7:00 AM MT)

Admin/cron endpoints are gated by ADMIN_SECRET (Bearer token).
The /qbo/connect endpoint accepts the secret as a query parameter so Josh can
hit it from a browser (one-time only).
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from keystone import delivery, qbo, qbo_oauth

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://cfo-agent-production-3b9e.up.railway.app"
)
QBO_REDIRECT_URI = f"{PUBLIC_BASE_URL}/qbo/callback"

app = FastAPI(title="Keystone — Relentless CFO Agent", version="0.1.0")


def _require_admin(authorization: str | None) -> None:
    if not ADMIN_SECRET:
        raise HTTPException(503, "ADMIN_SECRET not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != ADMIN_SECRET:
        raise HTTPException(403, "Invalid token")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "keystone",
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


@app.get("/qbo/connect")
def qbo_connect(secret: str = Query(default="")) -> RedirectResponse:
    """Start the QBO OAuth dance. Josh hits this URL once with ?secret=<ADMIN_SECRET>.

    Redirects to Intuit's authorize URL. Intuit will redirect back to /qbo/callback.
    """
    if not ADMIN_SECRET:
        raise HTTPException(503, "ADMIN_SECRET not configured")
    if secret != ADMIN_SECRET:
        raise HTTPException(403, "Invalid secret")
    auth_url = qbo_oauth.build_authorize_url(QBO_REDIRECT_URI)
    return RedirectResponse(url=auth_url, status_code=302)


@app.get("/qbo/callback")
def qbo_callback(
    code: str = Query(default=""),
    realmId: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
) -> HTMLResponse:
    """Intuit's redirect target. Exchanges the auth code for tokens."""
    if error:
        return HTMLResponse(
            f"<h1>QBO authorization failed</h1><p>Error: {error}</p>",
            status_code=400,
        )
    if not code or not realmId or not state:
        return HTMLResponse(
            "<h1>Missing code / realmId / state</h1>", status_code=400
        )
    if not qbo_oauth.consume_state(state):
        return HTMLResponse(
            "<h1>Invalid or expired state</h1><p>Restart from /qbo/connect.</p>",
            status_code=400,
        )

    try:
        result = qbo_oauth.exchange_code_for_tokens(code, realmId, QBO_REDIRECT_URI)
    except Exception as e:
        return HTMLResponse(
            f"<h1>Token exchange failed</h1><pre>{e}</pre>", status_code=500
        )

    return HTMLResponse(
        f"""
        <h1>Keystone is connected to QuickBooks.</h1>
        <p><strong>Realm ID:</strong> {result['realm_id']}</p>
        <p><strong>Refresh token valid until:</strong> {datetime.utcfromtimestamp(result['x_refresh_token_expires_at']).isoformat()}Z</p>
        <p>You can close this tab. Keystone will refresh the access token automatically.</p>
        <p>Next step: hit <code>/admin/sanity-pull</code> with your Bearer token to validate data access.</p>
        """,
        status_code=200,
    )


@app.post("/admin/slack-whoami")
def slack_whoami(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Diagnostic: returns which Slack bot identity is wired in the live container.

    Calls Slack's auth.test using the SLACK_BOT_TOKEN env var the container
    sees right now. If the result says 'lighthouse' instead of 'keystone',
    the env var wasn't actually updated despite Railway tooling claims.
    """
    _require_admin(authorization)
    import httpx

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return {"ok": False, "error": "SLACK_BOT_TOKEN env var is empty in this container"}

    resp = httpx.post(
        "https://slack.com/api/auth.test",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    payload = resp.json()
    return {
        "container_token_first_30": token[:30],
        "container_token_last_10": token[-10:],
        "container_token_len": len(token),
        "slack_auth_test": payload,
    }


@app.post("/admin/sanity-pull")
def sanity_pull(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Validate QBO plumbing: pull company info + balance sheet + AR summary."""
    _require_admin(authorization)

    out: dict[str, Any] = {"checks": {}}

    try:
        company = qbo.get_company_info()
        company_info = company.get("CompanyInfo", {})
        out["checks"]["company_info"] = {
            "ok": True,
            "company_name": company_info.get("CompanyName"),
            "legal_name": company_info.get("LegalName"),
            "country": company_info.get("Country"),
        }
    except Exception as e:
        out["checks"]["company_info"] = {"ok": False, "error": str(e)}

    try:
        bs = qbo.get_balance_sheet()
        out["checks"]["balance_sheet"] = {
            "ok": True,
            "report_name": bs.get("Header", {}).get("ReportName"),
            "as_of": bs.get("Header", {}).get("EndPeriod"),
        }
    except Exception as e:
        out["checks"]["balance_sheet"] = {"ok": False, "error": str(e)}

    try:
        ar = qbo.get_ar_aging_summary()
        out["checks"]["ar_aging_summary"] = {
            "ok": True,
            "report_name": ar.get("Header", {}).get("ReportName"),
            "as_of": ar.get("Header", {}).get("EndPeriod"),
        }
    except Exception as e:
        out["checks"]["ar_aging_summary"] = {"ok": False, "error": str(e)}

    return out


@app.post("/cron/ar-aging-digest")
def cron_ar_aging(
    authorization: str | None = Header(default=None),
    no_deliver: bool = Query(default=False),
) -> dict[str, Any]:
    """Pulls AR aging detail, returns + (by default) auto-delivers to Slack."""
    _require_admin(authorization)
    from keystone.jobs.ar_aging import run_ar_aging_digest

    result = run_ar_aging_digest()
    if no_deliver:
        return {**result, "delivery": "skipped (no_deliver=true)"}
    return {**result, "delivery": delivery.deliver_ar_aging(result)}


@app.post("/cron/pulse")
def cron_pulse(
    authorization: str | None = Header(default=None),
    no_deliver: bool = Query(default=False),
) -> dict[str, Any]:
    """Daily cash heartbeat. Auto-delivers unless no_deliver=true."""
    _require_admin(authorization)
    from keystone.jobs.pulse import run_pulse

    result = run_pulse()
    if no_deliver:
        return {**result, "delivery": "skipped (no_deliver=true)"}
    return {**result, "delivery": delivery.deliver_pulse(result)}


@app.post("/cron/watch")
def cron_watch(
    authorization: str | None = Header(default=None),
    no_deliver: bool = Query(default=False),
) -> dict[str, Any]:
    """Anomaly sweep. Auto-delivers critical+important unless no_deliver=true."""
    _require_admin(authorization)
    from keystone.jobs.watch import run_watch

    result = run_watch()
    if no_deliver:
        return {**result, "delivery": "skipped (no_deliver=true)"}
    return {**result, "delivery": delivery.deliver_watch(result)}


@app.post("/cron/audit")
def cron_audit(
    authorization: str | None = Header(default=None),
    no_deliver: bool = Query(default=False),
) -> dict[str, Any]:
    """Weekly audit. Auto-delivers Josh digest + Matt full unless no_deliver=true."""
    _require_admin(authorization)
    from keystone.jobs.audit import run_audit

    result = run_audit()
    if no_deliver:
        return {**result, "delivery": "skipped (no_deliver=true)"}
    return {**result, "delivery": delivery.deliver_audit(result)}


@app.post("/cron/counsel")
def cron_counsel(
    authorization: str | None = Header(default=None),
    no_deliver: bool = Query(default=False),
) -> dict[str, Any]:
    """Monthly walkthrough. Auto-delivers (chunked) unless no_deliver=true."""
    _require_admin(authorization)
    from keystone.jobs.counsel import run_counsel

    result = run_counsel()
    if no_deliver:
        return {**result, "delivery": "skipped (no_deliver=true)"}
    return {**result, "delivery": delivery.deliver_counsel(result)}
