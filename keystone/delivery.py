"""Keystone delivery — routes job outputs to Slack DMs.

Each job's `run_*()` function produces a dict with message text. This module
takes that dict and sends to the configured Slack recipients. Designed to be
called from the FastAPI cron endpoints in main.py, so jobs stay pure data
producers.

Design choices:
- Defensive on every send: a Slack failure logs but never crashes the job.
- Recipient IDs read from env vars (JOSH_SLACK_ID, MATT_SLACK_ID, FINANCE_SLACK_ID).
- Counsel report is split into chunks if it exceeds CHUNK_MAX so it threads
  cleanly in DM rather than wall-of-text.
- Returns a delivery log dict so endpoints can show what was sent.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from keystone import slack_client

logger = logging.getLogger(__name__)

# --- Recipient resolution --------------------------------------------------

JOSH_SLACK_ID = os.environ.get("JOSH_SLACK_ID", "")
MATT_SLACK_ID = os.environ.get("MATT_SLACK_ID", "")
FINANCE_SLACK_ID = os.environ.get("FINANCE_SLACK_ID", "")  # Joanne

# Audience symbolic names → Slack user IDs. Empty IDs are dropped.
AUDIENCE_MAP = {
    "josh": JOSH_SLACK_ID,
    "matt": MATT_SLACK_ID,
    "joanne": FINANCE_SLACK_ID,
    "finance": FINANCE_SLACK_ID,
}

# Soft-launch gate: restricts EVERY delivery to this whitelist of symbolic names.
# Default: just Josh, until he explicitly loops in Matt + Joanne.
# Set env var KEYSTONE_AUDIENCE="josh,matt" or "josh,matt,joanne" to expand.
_ALLOWED_NAMES = {
    n.strip().lower()
    for n in os.environ.get("KEYSTONE_AUDIENCE", "josh").split(",")
    if n.strip()
}

# Counsel splitting threshold — Slack allows 40K chars per msg but readability
# breaks at long ones; split on section headers when over this.
CHUNK_MAX = 3_500


def _resolve_audience(names: list[str]) -> list[str]:
    """Translate symbolic names ('josh', 'matt') to Slack IDs.

    Applies the soft-launch gate — if KEYSTONE_AUDIENCE env var is set, only
    names in that whitelist actually deliver. Empty IDs are dropped.
    """
    out: list[str] = []
    for n in names:
        n_lower = n.lower()
        if n_lower not in _ALLOWED_NAMES:
            continue
        slack_id = AUDIENCE_MAP.get(n_lower)
        if slack_id:
            out.append(slack_id)
    return out


def _send_one(slack_id: str, text: str) -> dict[str, Any]:
    """Send a single DM. Catches errors so the calling job survives."""
    try:
        resp = slack_client.send_dm(slack_id, text)
        return {"to": slack_id, "ok": True, "ts": resp.get("ts")}
    except Exception as e:
        logger.exception(f"Slack send failed to {slack_id}")
        return {"to": slack_id, "ok": False, "error": str(e)}


def _send_many(slack_ids: list[str], text: str) -> list[dict[str, Any]]:
    """Send the same text to multiple recipients."""
    return [_send_one(sid, text) for sid in slack_ids if sid]


# --- Chunking for long reports --------------------------------------------


def _chunk_counsel(report: str) -> list[str]:
    """Split a long Counsel report into Slack-sized chunks.

    Splits on H2 section headers (## ...). Keeps each chunk under CHUNK_MAX.
    If a single section is over the limit, breaks on blank lines.
    """
    if len(report) <= CHUNK_MAX:
        return [report]

    # Split on H2 headers; keep the header attached to its section.
    sections: list[str] = []
    current = ""
    for line in report.splitlines(keepends=True):
        if line.startswith("## ") and current:
            sections.append(current)
            current = line
        else:
            current += line
    if current:
        sections.append(current)

    # Re-merge small sections, break large ones further.
    chunks: list[str] = []
    buf = ""
    for sec in sections:
        if len(sec) > CHUNK_MAX:
            # Section too big — flush buf first, then break by blank lines
            if buf:
                chunks.append(buf)
                buf = ""
            paras = sec.split("\n\n")
            sub = ""
            for p in paras:
                if len(sub) + len(p) + 2 > CHUNK_MAX:
                    if sub:
                        chunks.append(sub)
                    sub = p
                else:
                    sub = sub + "\n\n" + p if sub else p
            if sub:
                chunks.append(sub)
        elif len(buf) + len(sec) > CHUNK_MAX:
            chunks.append(buf)
            buf = sec
        else:
            buf += sec
    if buf:
        chunks.append(buf)

    # Prefix chunks 2+ with a continuation indicator
    if len(chunks) > 1:
        chunks = [chunks[0]] + [f"_(continued, {i+1}/{len(chunks)})_\n\n{c}" for i, c in enumerate(chunks[1:], start=1)]
    return chunks


# --- Job-specific delivery functions ---------------------------------------


def deliver_pulse(result: dict[str, Any]) -> dict[str, Any]:
    """Pulse → Josh + Matt (plus Joanne when FINANCE_SLACK_ID is set)."""
    text = result.get("message_text", "")
    if not text:
        return {"sent": [], "skipped_reason": "no message_text"}
    audience = _resolve_audience(["josh", "matt", "joanne"])
    sent = _send_many(audience, text)
    return {"sent": sent, "recipient_count": len(sent)}


def deliver_ar_aging(result: dict[str, Any]) -> dict[str, Any]:
    """AR aging → per-rep messages to each rep; matt_message to Matt + Joanne."""
    sent_to_reps: list[dict[str, Any]] = []
    per_rep = result.get("per_rep_messages") or {}
    for rep_slack_id, message in per_rep.items():
        if rep_slack_id and message:
            sent_to_reps.append(_send_one(rep_slack_id, message))

    matt_message = result.get("matt_message", "")
    sent_to_matt: list[dict[str, Any]] = []
    if matt_message:
        audience = _resolve_audience(["matt", "joanne"])
        sent_to_matt = _send_many(audience, matt_message)

    return {
        "per_rep_sent": sent_to_reps,
        "matt_summary_sent": sent_to_matt,
        "rep_count": len(sent_to_reps),
    }


def deliver_audit(result: dict[str, Any]) -> dict[str, Any]:
    """Audit → josh_message to Josh; matt_message to Matt + Joanne."""
    sent: dict[str, Any] = {}
    josh_msg = result.get("josh_message", "")
    if josh_msg and JOSH_SLACK_ID:
        sent["josh"] = _send_one(JOSH_SLACK_ID, josh_msg)

    matt_msg = result.get("matt_message", "")
    if matt_msg:
        audience = _resolve_audience(["matt", "joanne"])
        sent["matt_and_finance"] = _send_many(audience, matt_msg)

    return sent


def deliver_counsel(result: dict[str, Any]) -> dict[str, Any]:
    """Counsel → Josh + Matt + Joanne, chunked if long."""
    report = result.get("full_report", "")
    if not report:
        return {"sent": [], "skipped_reason": "no full_report"}

    chunks = _chunk_counsel(report)
    audience = _resolve_audience(["josh", "matt", "joanne"])
    sent_per_recipient: dict[str, list[dict[str, Any]]] = {}
    for sid in audience:
        sent_chunks = []
        for chunk in chunks:
            sent_chunks.append(_send_one(sid, chunk))
        sent_per_recipient[sid] = sent_chunks

    return {
        "sent_per_recipient": sent_per_recipient,
        "chunk_count": len(chunks),
        "recipient_count": len(audience),
    }


def deliver_watch(result: dict[str, Any]) -> dict[str, Any]:
    """The Watch → routes by severity.

    Critical: Slack to Josh + Matt (SMS not wired in Phase 1).
    Important: Slack to Matt + Joanne (suppressed in quiet hours).
    Informational: logs only, no delivery.
    """
    sent: dict[str, Any] = {"critical": [], "important": [], "skipped": 0}

    msgs = result.get("messages_to_send") or {}
    quiet = msgs.get("deferred_during_quiet_hours", False)

    # Critical — route SMS audiences to Slack as Phase 1 fallback
    for finding in msgs.get("sms", []) or []:
        audience_names = finding.get("audience") or ["josh", "matt"]
        ids = _resolve_audience(audience_names)
        for sid in ids:
            sent["critical"].append(_send_one(sid, finding.get("text", "")))

    # Important — Slack DM, only if not quiet hours
    if not quiet:
        for finding in msgs.get("slack", []) or []:
            audience_names = finding.get("audience") or ["matt"]
            ids = _resolve_audience(audience_names)
            for sid in ids:
                sent["important"].append(_send_one(sid, finding.get("text", "")))
    else:
        sent["skipped"] = len(msgs.get("slack", []) or [])

    return sent
