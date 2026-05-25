"""Slack request verification + event dedup helpers.

Slack signs every webhook with HMAC-SHA256 over `v0:{timestamp}:{body}` keyed
by the app's signing secret. We verify that signature on every /slack/events
POST and reject anything older than 5 minutes (replay defense).

Slack retries events when it doesn't see a 200 fast enough, so we also keep
a rolling window of the last N processed event_ids in a JSON file under
/data so duplicates get dropped after the first run.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
STATE_DIR = os.environ.get("STATE_DIR", "/data")
SEEN_EVENTS_PATH = Path(STATE_DIR) / "slack_events_seen.json"

# Max age (seconds) for an incoming Slack request. Slack docs recommend 5 min.
MAX_REQUEST_AGE_SECS = 60 * 5

# How many event_ids to remember (FIFO eviction).
SEEN_EVENTS_MAX = 1000


# --- Signature verification ------------------------------------------------


def verify_slack_signature(
    body: bytes,
    timestamp: str | None,
    signature: str | None,
) -> tuple[bool, str]:
    """Verify the Slack signature on an incoming request.

    Returns (ok, reason). reason is "" when ok.
    """
    if not SLACK_SIGNING_SECRET:
        return False, "SLACK_SIGNING_SECRET not configured"
    if not timestamp or not signature:
        return False, "missing signature headers"

    # Replay defense — reject anything older than 5 min.
    try:
        ts_int = int(timestamp)
    except ValueError:
        return False, "bad timestamp header"
    if abs(time.time() - ts_int) > MAX_REQUEST_AGE_SECS:
        return False, "stale request (>5 min)"

    basestring = f"v0:{timestamp}:".encode("utf-8") + body
    expected = (
        "v0="
        + hmac.new(
            SLACK_SIGNING_SECRET.encode("utf-8"),
            basestring,
            hashlib.sha256,
        ).hexdigest()
    )

    if not hmac.compare_digest(expected, signature):
        return False, "signature mismatch"
    return True, ""


# --- Event dedup -----------------------------------------------------------


def _load_seen() -> list[str]:
    """Read the rolling window of seen event_ids. Returns [] on any error."""
    try:
        if SEEN_EVENTS_PATH.exists():
            with open(SEEN_EVENTS_PATH, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return [str(x) for x in data]
    except Exception:
        logger.exception("Failed to read seen events file")
    return []


def _save_seen(seen: list[str]) -> None:
    """Persist the rolling window. Atomic write."""
    try:
        SEEN_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SEEN_EVENTS_PATH.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(seen[-SEEN_EVENTS_MAX:], f)
        tmp.replace(SEEN_EVENTS_PATH)
    except Exception:
        logger.exception("Failed to write seen events file")


def is_duplicate_event(event_id: str | None) -> bool:
    """Return True if we've already processed this event_id.

    Side effect: if not a duplicate, records the event_id and trims to the
    rolling window size.
    """
    if not event_id:
        # No event_id means we can't dedup — treat as fresh, log it.
        logger.warning("Slack event with no event_id; cannot dedup")
        return False
    seen = _load_seen()
    if event_id in seen:
        return True
    seen.append(event_id)
    _save_seen(seen)
    return False


# --- User allowlist --------------------------------------------------------


def get_allowed_users() -> set[str]:
    """Slack user IDs allowed to Q&A Keystone.

    Default: JOSH_SLACK_ID + MATT_SLACK_ID from env.
    Override: set KEYSTONE_QA_USERS to a CSV of Slack user IDs.
    """
    raw = os.environ.get("KEYSTONE_QA_USERS", "").strip()
    if raw:
        return {u.strip() for u in raw.split(",") if u.strip()}
    out: set[str] = set()
    josh = os.environ.get("JOSH_SLACK_ID", "").strip()
    matt = os.environ.get("MATT_SLACK_ID", "").strip()
    if josh:
        out.add(josh)
    if matt:
        out.add(matt)
    return out


def is_user_allowed(user_id: str | None) -> bool:
    if not user_id:
        return False
    allowed = get_allowed_users()
    if not allowed:
        # If no allowlist resolved, default-deny to be safe.
        logger.warning("No Q&A allowlist resolved; denying user_id=%s", user_id)
        return False
    return user_id in allowed


# --- Audience routing ------------------------------------------------------


def audience_for_user(user_id: str) -> str:
    """Map a Slack user ID to a Keystone audience label for voice tuning.

    'josh' → plain trades English. 'matt' → accountant-precise.
    Falls back to 'josh' for unknown allowed users so the answer stays plain.
    """
    if user_id == os.environ.get("MATT_SLACK_ID", "").strip():
        return "matt"
    if user_id == os.environ.get("FINANCE_SLACK_ID", "").strip():
        return "joanne"
    return "josh"


def _truthy(s: str) -> bool:
    return s.strip().lower() in {"1", "true", "yes", "y", "on"}


def signature_verification_enabled() -> bool:
    """Allow disabling signature verification ONLY for local dev via env.

    Defaults to enabled. Set KEYSTONE_SKIP_SLACK_VERIFY=true to bypass.
    """
    return not _truthy(os.environ.get("KEYSTONE_SKIP_SLACK_VERIFY", ""))
