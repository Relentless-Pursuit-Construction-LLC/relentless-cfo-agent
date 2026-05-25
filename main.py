"""Keystone — FastAPI entrypoint.

Three job surfaces:
  GET  /health                  — liveness probe
  POST /cron/ar-aging-digest    — AR digest (Mon-Sat 6:30 AM MT)
  POST /cron/pulse              — daily cash heartbeat
  POST /admin/sanity-pull       — manual QBO sanity check (auth-gated)

Admin endpoints are gated by ADMIN_SECRET (Bearer token).
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException

from keystone import qbo

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")

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
def cron_ar_aging(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _require_admin(authorization)
    from keystone.jobs.ar_aging import run_ar_aging_digest

    return run_ar_aging_digest()


@app.post("/cron/pulse")
def cron_pulse(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _require_admin(authorization)
    from keystone.jobs.pulse import run_pulse

    return run_pulse()
