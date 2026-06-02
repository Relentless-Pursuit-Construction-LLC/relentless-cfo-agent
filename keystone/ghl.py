"""GoHighLevel client — read-only access to opportunities (deals).

GHL is Relentless's system of record for sales. This client pulls opportunities
so Keystone can report "sales booked" (deals marked Won) per month — the true
growth number, independent of when QBO invoices get created or dated.

Read-only. Never writes to GHL.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

GHL_TOKEN = os.environ.get("GHL_TOKEN", "")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID", "")
GHL_API_BASE = "https://services.leadconnectorhq.com"
GHL_API_VERSION = "2021-07-28"

# Safety cap on pagination so a runaway never loops forever.
MAX_PAGES = 30  # 30 * 100 = 3,000 opportunities


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {GHL_TOKEN}",
        "Version": GHL_API_VERSION,
        "Accept": "application/json",
    }


def is_configured() -> bool:
    """True only if both token and location are present."""
    return bool(GHL_TOKEN and GHL_LOCATION_ID)


def search_all_opportunities() -> list[dict[str, Any]]:
    """Return ALL opportunities for the location, following pagination.

    Each opportunity dict includes: id, name, monetaryValue, status
    ('won'|'open'|'abandoned'|'lost'), assignedTo, source, createdAt,
    lastStatusChangeAt, pipelineId, pipelineStageId, contactId.
    """
    if not is_configured():
        raise RuntimeError("GHL_TOKEN / GHL_LOCATION_ID not configured")

    out: list[dict[str, Any]] = []
    url: str | None = (
        f"{GHL_API_BASE}/opportunities/search"
        f"?location_id={GHL_LOCATION_ID}&limit=100"
    )
    pages = 0
    with httpx.Client(timeout=30.0) as client:
        while url and pages < MAX_PAGES:
            resp = client.get(url, headers=_headers())
            resp.raise_for_status()
            data = resp.json()
            out.extend(data.get("opportunities", []) or [])
            meta = data.get("meta", {}) or {}
            url = meta.get("nextPageUrl")
            pages += 1
    return out


def monetary_value(opp: dict[str, Any]) -> float:
    """Parse an opportunity's monetaryValue defensively."""
    v = opp.get("monetaryValue")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", "").replace("$", ""))
        except ValueError:
            return 0.0
    return 0.0
