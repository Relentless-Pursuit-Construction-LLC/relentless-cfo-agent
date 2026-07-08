"""Base44 "Relentless" app client — read-only rep-attribution source.

The Base44 app (measure + pricing) is the forward-looking system of record
for who owns a job. Each Job record carries customer_name plus
sales_rep_email / sales_rep_name. Keystone pulls Jobs once per run (cached)
and builds a normalized customer-name -> rep index, so AR digests can
attribute QBO customers to reps without relying on QBO SalesRep fields
(which Relentless never populates — rep info historically lived in GHL).

Read-only. Fails soft: missing env vars or any fetch error -> empty index,
and callers fall back to the QBO lookup chain.

Env (set in Railway on the cfo-agent service):
  BASE44_APP_ID   — the Relentless app id
  BASE44_API_KEY  — read key from the app's Dashboard -> API page
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE44_APP_ID = os.environ.get("BASE44_APP_ID", "")
BASE44_API_KEY = os.environ.get("BASE44_API_KEY", "")
BASE44_API_BASE = "https://app.base44.com/api/apps"

# In-memory cache — one Jobs pull per cron run is plenty.
_CACHE_TTL_SECS = 900
_cache: dict[str, Any] = {"index": None, "fetched_at": 0.0}

# QBO sub-customers carry trade suffixes ("Cynthia Byward Windows",
# "Anthony Landato Roof", "Rosa Blanco SGD"); the app stores plain customer
# names ("Cynthia Byward"). Strip trailing trade words before matching.
_TRADE_SUFFIXES = (
    "windows", "window", "roof", "roofing", "sgd", "doors", "door",
    "gc", "kitchen", "bath", "deck", "siding", "landscaping", "hardscape",
)


def is_configured() -> bool:
    return bool(BASE44_APP_ID and BASE44_API_KEY)


def normalize_customer_name(name: str) -> str:
    """Lowercase, flip "Last, First" -> "first last", strip punctuation and
    trailing trade suffixes, collapse whitespace."""
    if not name:
        return ""
    n = name.strip().lower()
    if "," in n:
        last, _, first = n.partition(",")
        n = f"{first.strip()} {last.strip()}"
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    words = [w for w in n.split() if w]
    while words and words[-1] in _TRADE_SUFFIXES:
        words.pop()
    return " ".join(words)


def _fetch_jobs() -> list[dict[str, Any]]:
    """Pull all Job records, paginated. Raises on HTTP errors."""
    out: list[dict[str, Any]] = []
    skip = 0
    limit = 200
    with httpx.Client(timeout=20.0) as client:
        while True:
            resp = client.get(
                f"{BASE44_API_BASE}/{BASE44_APP_ID}/entities/Job",
                params={"limit": limit, "skip": skip},
                headers={"api_key": BASE44_API_KEY},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < limit:
                break
            skip += limit
            if skip > 20_000:  # runaway guard
                logger.warning("Base44 Job pagination hit 20k guard; truncating")
                break
    return out


def get_rep_index() -> dict[str, dict[str, Any]]:
    """normalized customer name -> {rep_email, rep_name, job_id, created_date}.

    Cached 15 min. When a customer has multiple jobs, the most recently
    created job with a rep wins. Returns {} when unconfigured; returns the
    stale cache (if any) on fetch failure rather than erasing attribution.
    """
    now = time.time()
    if _cache["index"] is not None and (now - _cache["fetched_at"]) < _CACHE_TTL_SECS:
        return _cache["index"]
    if not is_configured():
        return {}
    try:
        jobs = _fetch_jobs()
    except Exception as e:
        logger.warning("Base44 Job fetch failed: %s", e)
        return _cache["index"] or {}

    index: dict[str, dict[str, Any]] = {}
    for job in jobs:
        cust = normalize_customer_name(job.get("customer_name") or "")
        email = (job.get("sales_rep_email") or "").strip().lower()
        rep_nm = (job.get("sales_rep_name") or "").strip()
        if not cust or not (email or rep_nm):
            continue
        created = job.get("created_date") or ""
        prev = index.get(cust)
        if prev is None or created > prev.get("created_date", ""):
            index[cust] = {
                "rep_email": email,
                "rep_name": rep_nm,
                "job_id": job.get("id"),
                "created_date": created,
            }
    _cache["index"] = index
    _cache["fetched_at"] = now
    logger.info("Base44 rep index built: %d customers", len(index))
    return index


def rep_for_customer(qbo_customer_name: str) -> dict[str, Any] | None:
    """Look up the app's rep for a QBO customer display name. None if no match."""
    if not qbo_customer_name:
        return None
    return get_rep_index().get(normalize_customer_name(qbo_customer_name))
