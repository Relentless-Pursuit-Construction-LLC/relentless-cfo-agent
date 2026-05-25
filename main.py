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

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from keystone import delivery, qbo, qbo_oauth, slack_verify

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


@app.post("/admin/signing-secret-check")
def signing_secret_check(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Diagnostic: confirms whether SLACK_SIGNING_SECRET is loaded in the container,
    and returns its first 4 / last 4 chars so Josh can compare against the value
    in api.slack.com/apps -> Keystone -> Basic Information -> Signing Secret.
    """
    _require_admin(authorization)
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    return {
        "is_set": bool(secret),
        "length": len(secret),
        "first_4": secret[:4] if secret else "",
        "last_4": secret[-4:] if secret else "",
        "expected_length": 32,
        "expected_pattern": "32-char lowercase hex",
        "hint": (
            "If first_4/last_4 don't match what you see in Slack, you copied "
            "the wrong field. The right one is labeled 'Signing Secret' "
            "(NOT Client Secret, NOT Verification Token)."
        ),
    }


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


# --- Slack events: conversational Q&A -------------------------------------


@app.post("/slack/events")
async def slack_events(
    request: Request,
    background_tasks: BackgroundTasks,
    x_slack_signature: str | None = Header(default=None),
    x_slack_request_timestamp: str | None = Header(default=None),
) -> Response:
    """Slack Events API webhook.

    Handles three event types:
      - url_verification: handshake; echo the challenge.
      - event_callback / message (channel_type=im): DM to Keystone.
      - event_callback / app_mention: @Keystone in a channel.

    We verify the Slack signature, dedup retried events, allowlist the user,
    ACK fast (Slack requires <3s), and process the message in a background
    task so the Claude call doesn't block the webhook response.
    """
    import logging

    logger = logging.getLogger("keystone.slack_events")

    body_bytes = await request.body()

    # 1) Signature verification (skippable only via KEYSTONE_SKIP_SLACK_VERIFY).
    if slack_verify.signature_verification_enabled():
        ok, reason = slack_verify.verify_slack_signature(
            body_bytes, x_slack_request_timestamp, x_slack_signature
        )
        if not ok:
            logger.warning("Rejecting Slack request: %s", reason)
            raise HTTPException(401, f"signature check failed: {reason}")

    # 2) Parse payload.
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")

    payload_type = payload.get("type")

    # 3) URL verification handshake — echo the challenge back.
    if payload_type == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge", "")})

    # 4) Event callback — dedup, then route by inner event type.
    if payload_type == "event_callback":
        event_id = payload.get("event_id")
        if slack_verify.is_duplicate_event(event_id):
            logger.info("Dropping duplicate Slack event %s", event_id)
            return Response(status_code=200)

        event = payload.get("event") or {}
        event_type = event.get("type")
        user_id = event.get("user")
        channel = event.get("channel", "")
        text = event.get("text", "")
        thread_ts = event.get("thread_ts")  # Only set when already in a thread

        # Ignore the bot's own messages + edits + bot-authored events.
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return Response(status_code=200)

        # Allowlist check — drop anyone who isn't Josh/Matt (or the configured set).
        if not slack_verify.is_user_allowed(user_id):
            logger.info(
                "Ignoring Slack event from non-allowlisted user user_id=%s type=%s",
                user_id,
                event_type,
            )
            return Response(status_code=200)

        # DM to the bot.
        if event_type == "message" and event.get("channel_type") == "im":
            background_tasks.add_task(
                _process_slack_qa, user_id, channel, text, None
            )
            return Response(status_code=200)

        # @mention in a channel — reply in-thread.
        if event_type == "app_mention":
            # Use event_ts as the thread anchor if we're not already in a thread.
            anchor = thread_ts or event.get("ts")
            background_tasks.add_task(
                _process_slack_qa, user_id, channel, text, anchor
            )
            return Response(status_code=200)

        # Anything else (e.g. message.channels we didn't subscribe to) — ack.
        return Response(status_code=200)

    # Unknown top-level type — ACK so Slack doesn't retry forever.
    return Response(status_code=200)


def _process_slack_qa(
    user_id: str, channel: str, text: str, thread_ts: str | None
) -> None:
    """Background task wrapper — keeps the import lazy so the webhook handler
    stays fast on cold-start and any import-time error in qa.py gets logged
    instead of crashing the webhook ACK.
    """
    import logging

    logger = logging.getLogger("keystone.slack_events")
    try:
        from keystone import qa

        qa.process_message(user_id, channel, text, thread_ts)
    except Exception:
        logger.exception(
            "Q&A background task failed for user=%s channel=%s", user_id, channel
        )
