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

# Paths to persona + knowledge files — relative to repo root in dev, /app in Railway
_HERE = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT_PATH = _HERE / "docs" / "KEYSTONE_SYSTEM_PROMPT.md"
KNOWLEDGE_PATH = _HERE / "docs" / "KEYSTONE_KNOWLEDGE.md"
KNOWLEDGE_INDEX_PATH = _HERE / "docs" / "KEYSTONE_KNOWLEDGE_INDEX.md"

_client: Anthropic | None = None
_system_prompt: str | None = None
_knowledge: str | None = None


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


def get_knowledge() -> str:
    """Load Matt's institutional-memory knowledge file.

    This is the file Matt edits via GitHub to teach Keystone what's normal
    vs anomalous for Relentless. Loaded into every Claude call alongside the
    system prompt so Keystone's interpretations are calibrated to actual
    Relentless patterns instead of generic trade-business defaults.
    """
    global _knowledge
    if _knowledge is None:
        parts = []
        if KNOWLEDGE_PATH.exists():
            parts.append("# Relentless-specific Knowledge (Matt's institutional memory)\n\n")
            parts.append(KNOWLEDGE_PATH.read_text(encoding="utf-8"))
        if KNOWLEDGE_INDEX_PATH.exists():
            parts.append("\n\n# Keystone framework index\n\n")
            parts.append(KNOWLEDGE_INDEX_PATH.read_text(encoding="utf-8"))
        _knowledge = "".join(parts) if parts else ""
    return _knowledge


def reload_persona() -> None:
    """Bust the in-memory cache so next call re-reads from disk.

    Useful after Matt commits a knowledge update — call via an admin endpoint
    or just on container restart (Railway redeploys auto-reload on push).
    """
    global _system_prompt, _knowledge
    _system_prompt = None
    _knowledge = None


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
    system_parts = [get_system_prompt()]
    knowledge = get_knowledge()
    if knowledge:
        system_parts.append("\n\n---\n\n" + knowledge)
    system = "".join(system_parts)

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
