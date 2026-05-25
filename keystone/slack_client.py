"""Slack helpers — Keystone's voice into the team's DMs.

Uses a NEW Keystone bot (separate identity from the GHL agent's bot).
"""

from __future__ import annotations

import os
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")

_client: WebClient | None = None


def _get_client() -> WebClient:
    global _client
    if _client is None:
        if not SLACK_BOT_TOKEN:
            raise RuntimeError("SLACK_BOT_TOKEN not set")
        _client = WebClient(token=SLACK_BOT_TOKEN)
    return _client


def send_dm(user_id: str, text: str, blocks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Send a DM to a Slack user. user_id is the Slack U... ID."""
    client = _get_client()
    # Open conversation first (Slack pattern for DMs)
    try:
        conv = client.conversations_open(users=user_id)
        channel_id = conv["channel"]["id"]
        resp = client.chat_postMessage(
            channel=channel_id,
            text=text,
            blocks=blocks,
            unfurl_links=False,
            unfurl_media=False,
        )
        return dict(resp.data)
    except SlackApiError as e:
        raise RuntimeError(f"Slack DM failed for user {user_id}: {e.response['error']}") from e


def send_to_channel(channel_id: str, text: str, blocks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Post to a channel by ID."""
    client = _get_client()
    try:
        resp = client.chat_postMessage(
            channel=channel_id,
            text=text,
            blocks=blocks,
            unfurl_links=False,
            unfurl_media=False,
        )
        return dict(resp.data)
    except SlackApiError as e:
        raise RuntimeError(f"Slack channel post failed: {e.response['error']}") from e
