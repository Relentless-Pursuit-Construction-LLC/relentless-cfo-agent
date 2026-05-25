"""Keystone's persona — loaded into every Claude call.

The system prompt lives in docs/KEYSTONE_SYSTEM_PROMPT.md as the source of truth
(so Josh + Matt can edit it without touching code). This module loads it at
runtime and exposes helpers for generating prose in Keystone's voice.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from anthropic import Anthropic

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("KEYSTONE_MODEL", "claude-opus-4-5")

# Path to system prompt — relative to repo root in dev, /app in Railway container
_HERE = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT_PATH = _HERE / "docs" / "KEYSTONE_SYSTEM_PROMPT.md"

_client: Anthropic | None = None
_system_prompt: str | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def get_system_prompt() -> str:
    """Load the Keystone system prompt from disk. Cached after first load."""
    global _system_prompt
    if _system_prompt is None:
        if not SYSTEM_PROMPT_PATH.exists():
            raise RuntimeError(
                f"Keystone system prompt missing at {SYSTEM_PROMPT_PATH}"
            )
        _system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    return _system_prompt


def speak(
    task_brief: str,
    data: dict[str, Any] | None = None,
    *,
    audience: str = "josh",
    max_tokens: int = 800,
) -> str:
    """Generate prose in Keystone's voice for a given task + data payload.

    audience: "josh" | "matt" | "joanne" — controls voice translation
    """
    client = _get_client()
    system = get_system_prompt()

    user_msg = (
        f"# Task\n{task_brief}\n\n"
        f"# Audience\n{audience}\n\n"
        f"# Data\n{data!r}"
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )

    # Extract text from the response content blocks
    parts = []
    for block in resp.content:
        if block.type == "text":
            parts.append(block.text)
    return "".join(parts).strip()
