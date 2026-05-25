"""Rep registry fetcher.

Pulls the shared rep ↔ Slack ID mapping maintained at:
  Relentless-Pursuit-Construction-LLC/relentless-shared-config/main/rep-slack-mapping.json

The GHL agent uses this same file. Keystone joins QBO customers → assigned rep
→ Slack ID for per-rep AR digests.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

REGISTRY_URL = (
    "https://raw.githubusercontent.com/"
    "Relentless-Pursuit-Construction-LLC/"
    "relentless-shared-config/main/rep-slack-mapping.json"
)

# In-memory cache — refresh every 15 min so config changes propagate fast
_CACHE_TTL_SECS = 900
_cache: dict[str, Any] = {"data": None, "fetched_at": 0}


def get_rep_registry() -> dict[str, Any]:
    """Return the rep registry as a dict. Cached 15 min."""
    now = time.time()
    if _cache["data"] is not None and (now - _cache["fetched_at"]) < _CACHE_TTL_SECS:
        return _cache["data"]

    resp = httpx.get(REGISTRY_URL, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    _cache["data"] = data
    _cache["fetched_at"] = now
    return data


def get_rep_by_name(name: str) -> dict[str, Any] | None:
    """Find a rep entry by name (case-insensitive)."""
    registry = get_rep_registry()
    name_lower = name.lower()
    for entry in registry.get("reps", []):
        if entry.get("name", "").lower() == name_lower:
            return entry
    return None


def get_slack_id_for_rep(name: str) -> str | None:
    rep = get_rep_by_name(name)
    return rep.get("slack_id") if rep else None
