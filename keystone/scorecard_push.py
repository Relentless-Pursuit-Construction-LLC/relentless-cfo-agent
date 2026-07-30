"""Push the weekly audit's finance numbers to the LifeDesign CEO Scorecard.

Called at the end of the Monday audit (keystone/jobs/audit.py::run_audit) so the
scorecard's four Finance metrics carry the exact numbers Matt's audit reports —
no second QBO pull, no divergent math. Writes into the LifeDesign app via its
bridgeOp function (same bridge Lighthouse uses).

Fail-soft by contract: any failure here must NEVER break the audit. run_audit
wraps the call in try/except and only appends a data-quality flag on failure.

Env (set on the cfo-agent Railway service):
  LIFEDESIGN_APP_URL   e.g. https://6a483f1c18831c330924e123.base44.app/api/apps/<id>/functions
  BRIDGE_TOKEN         x-bridge-token for bridgeOp

If either var is missing the push is skipped silently (returns "skipped").
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import date, timedelta
from typing import Any

# ScorecardWeek.metric_key <- audit stats path. gross_margin is a fraction (0-1)
# in stats; the scorecard stores it as a percent number (e.g. 35.0).
# cash_in_bank is intentionally NOT here — it's pushed DAILY by the pulse job
# (push_daily_cash), so the weekly audit must never write or delete it.
_FINANCE_KEYS = {"revenue_collected", "ar_outstanding", "gross_margin"}


def _week_ending(week_window: str) -> str:
    """stats['week_window'] is 'YYYY-MM-DD..YYYY-MM-DD' (prior Mon..Sat).

    The scorecard keys weeks by their Sunday end-date (ISO week Mon-Sun), matching
    the Terros sales feed. Sunday = last_monday + 6 days.
    """
    start = date.fromisoformat(week_window.split("..", 1)[0])
    return (start + timedelta(days=6)).isoformat()


def _quarter(week_ending: str) -> str:
    d = date.fromisoformat(week_ending)
    return f"Q{(d.month - 1) // 3 + 1} {d.year}"


def _rows_from_stats(stats: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    we = _week_ending(stats["week_window"])
    q = _quarter(we)
    gm = (stats.get("margin") or {}).get("gross_margin")
    vals = {
        "revenue_collected": (stats.get("revenue") or {}).get("revenue"),
        "ar_outstanding": (stats.get("ar") or {}).get("total_ar"),
        "gross_margin": round(gm * 100, 1) if gm is not None else None,
    }
    rows = [
        {"metric_key": k, "week_ending": we, "actual": round(v, 2), "quarter": q}
        for k, v in vals.items()
        if v is not None  # never write a null actual (e.g. margin when COGS empty)
    ]
    return we, q, rows


def _bop(base: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        base.rstrip("/") + "/bridgeOp",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "x-bridge-token": token,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (keystone-scorecard)",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def push_finance_scorecard(stats: dict[str, Any]) -> str:
    """Upsert this week's four Finance metrics. Idempotent for the week."""
    base = os.environ.get("LIFEDESIGN_APP_URL")
    token = os.environ.get("BRIDGE_TOKEN")
    if not base or not token:
        return "skipped (no LIFEDESIGN_APP_URL / BRIDGE_TOKEN)"

    we, q, rows = _rows_from_stats(stats)
    if not rows:
        return f"no finance rows to write for {we}"

    # Replace only this week's finance rows — never touch Sales rows or other weeks.
    ex = _bop(base, token, {"op": "query", "entity": "ScorecardWeek",
                            "filter": {"quarter": q}, "limit": 2000})
    stale = [r["id"] for r in (ex.get("results") or [])
             if r.get("metric_key") in _FINANCE_KEYS and r.get("week_ending") == we]
    if stale:
        _bop(base, token, {"op": "bulk_delete", "entity": "ScorecardWeek", "ids": stale})
    _bop(base, token, {"op": "bulk_create", "entity": "ScorecardWeek", "rows": rows})
    return f"wrote {len(rows)} finance rows for week_ending {we} (replaced {len(stale)})"


def _week_ending_of(d: date) -> str:
    """ISO-week (Mon-Sun) Sunday end-date for a given date."""
    return (d + timedelta(days=6 - d.weekday())).isoformat()


def push_daily_cash(cash_total, as_of: date) -> str:
    """Upsert today's Cash in Bank into the CURRENT week's scorecard row.

    Called DAILY from the pulse job so the scorecard's cash number is never more
    than a day stale. Idempotent for the week — replaces the single cash_in_bank
    row for the current week each run. Fail-soft by contract (caller wraps it).
    """
    base = os.environ.get("LIFEDESIGN_APP_URL")
    token = os.environ.get("BRIDGE_TOKEN")
    if not base or not token:
        return "skipped (no LIFEDESIGN_APP_URL / BRIDGE_TOKEN)"
    if cash_total is None:
        return "skipped (no cash figure)"
    we = _week_ending_of(as_of)
    q = _quarter(we)
    row = {"metric_key": "cash_in_bank", "week_ending": we,
           "actual": round(cash_total, 2), "quarter": q}
    ex = _bop(base, token, {"op": "query", "entity": "ScorecardWeek",
                            "filter": {"quarter": q}, "limit": 2000})
    stale = [r["id"] for r in (ex.get("results") or [])
             if r.get("metric_key") == "cash_in_bank" and r.get("week_ending") == we]
    if stale:
        _bop(base, token, {"op": "bulk_delete", "entity": "ScorecardWeek", "ids": stale})
    _bop(base, token, {"op": "bulk_create", "entity": "ScorecardWeek", "rows": [row]})
    return f"cash_in_bank {we} = ${cash_total:,.0f}"
