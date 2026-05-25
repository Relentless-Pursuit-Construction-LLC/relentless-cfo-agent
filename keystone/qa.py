"""Keystone Q&A engine — conversational finance lookups over Slack DM.

Flow:
  1. main.py /slack/events validates the webhook + dedups + allowlists user.
  2. For DMs / @mentions, main.py calls process_message() in a background task.
  3. process_message() loads Keystone's persona + knowledge, builds a Claude
     tool-use loop, and asks Claude to answer the user's question.
  4. Claude can call any of the QBO read-only helpers via tool use; we loop
     until Claude returns a final text response (or hits the iteration cap).
  5. We post the answer back to the user's DM (or thread when @mentioned).

Read-only contract: every tool here is a getter. No writes. No new deps.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from anthropic import Anthropic

from keystone import qbo, slack_client, voice
from keystone.slack_verify import audience_for_user

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
QA_MODEL = os.environ.get("KEYSTONE_QA_MODEL", "claude-sonnet-4-5")
MAX_TOOL_ITERATIONS = 8
MAX_TOKENS = 1500

AZ_TZ = ZoneInfo("America/Phoenix")


# --- Tool definitions exposed to Claude -----------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_balance_sheet",
        "description": (
            "Pull the QuickBooks Balance Sheet as of a given date. Use this for "
            "current cash, AR totals, AP totals, equity. If as_of_date omitted, "
            "QBO returns the report as of today."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "as_of_date": {
                    "type": "string",
                    "description": "YYYY-MM-DD. Omit for today.",
                },
            },
        },
    },
    {
        "name": "get_ar_aging_summary",
        "description": (
            "AR aging summary by bucket (current, 1-30, 31-60, 61-90, 91+). "
            "Use when the user wants a top-line view of receivables aging."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "as_of_date": {
                    "type": "string",
                    "description": "YYYY-MM-DD. Omit for today.",
                },
            },
        },
    },
    {
        "name": "get_ar_aging_detail",
        "description": (
            "AR aging detail — every open invoice with customer, age, balance. "
            "Use when the user asks who owes us money or which invoices are "
            "stale."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "as_of_date": {
                    "type": "string",
                    "description": "YYYY-MM-DD. Omit for today.",
                },
            },
        },
    },
    {
        "name": "get_profit_loss",
        "description": (
            "P&L report for a date range. Use for revenue, COGS, operating "
            "expenses, net income questions over any window."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "YYYY-MM-DD",
                },
                "end_date": {
                    "type": "string",
                    "description": "YYYY-MM-DD",
                },
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_invoices_for_date",
        "description": (
            "Every invoice created on a specific date. Use to confirm "
            "yesterday's revenue or check what was billed on day X."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_str": {
                    "type": "string",
                    "description": "YYYY-MM-DD",
                },
            },
            "required": ["date_str"],
        },
    },
    {
        "name": "get_open_invoices",
        "description": (
            "All invoices with an outstanding balance, ordered by due date. "
            "Use for collections triage and DSO questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max invoices to return (default 1000).",
                },
            },
        },
    },
    {
        "name": "qbo_query",
        "description": (
            "Raw QuickBooks Query Language SELECT (advanced fallback). Use only "
            "when none of the higher-level helpers fit. Read-only. Never "
            "construct an UPDATE/DELETE — QBO QL only supports SELECT here."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query_string": {
                    "type": "string",
                    "description": "A QBO QL SELECT statement.",
                },
            },
            "required": ["query_string"],
        },
    },
]


# --- Tool dispatcher -------------------------------------------------------


def _run_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute a tool call. Always returns a dict; errors are wrapped so
    Claude can see them and respond gracefully instead of crashing the loop.
    """
    try:
        if name == "get_balance_sheet":
            return qbo.get_balance_sheet(args.get("as_of_date"))
        if name == "get_ar_aging_summary":
            return qbo.get_ar_aging_summary(args.get("as_of_date"))
        if name == "get_ar_aging_detail":
            return qbo.get_ar_aging_detail(args.get("as_of_date"))
        if name == "get_profit_loss":
            start = args["start_date"]
            end = args["end_date"]
            return qbo.get_profit_loss(start, end)
        if name == "get_invoices_for_date":
            return qbo.get_invoices_for_date(args["date_str"])
        if name == "get_open_invoices":
            return qbo.get_open_invoices(int(args.get("limit", 1000)))
        if name == "qbo_query":
            q = args["query_string"]
            # Defensive guard — only allow SELECT.
            if not q.strip().upper().startswith("SELECT"):
                return {
                    "error": "qbo_query only accepts SELECT statements (read-only).",
                }
            return qbo.qbo_query(q)
        return {"error": f"unknown tool: {name}"}
    except Exception as e:
        logger.exception("Tool %s failed", name)
        return {"error": str(e), "tool": name}


# --- Persona + prompt assembly --------------------------------------------


def _qa_system_prompt(audience: str) -> str:
    """System prompt for Q&A: base persona + knowledge + Q&A addendum."""
    base = voice.get_system_prompt()
    knowledge = voice.get_knowledge()

    now_az = datetime.now(AZ_TZ).strftime("%Y-%m-%d %H:%M %Z")

    addendum = (
        "\n\n---\n\n"
        "# Q&A mode\n\n"
        "You are now answering a question over Slack DM in real time. The user "
        "is asking a finance question; pull the data you need using the tools "
        "available, then answer in your voice.\n\n"
        f"Current time: {now_az}.\n"
        f"Audience for this reply: {audience}. Translate accordingly:\n"
        "- josh -> plain trades English, under 200 words unless he asks for more, "
        "lead with the number and the one move.\n"
        "- matt -> accountant-precise, ratios and GAAP terms OK, give him the "
        "underlying figures.\n"
        "- joanne -> bookkeeper-precise, transaction-level detail.\n\n"
        "Rules:\n"
        "- Read-only. Never propose a write, never instruct a transaction.\n"
        "- Dollars and percents together. Round to actual, never in our favor.\n"
        "- If a tool errors or returns nothing useful, say so honestly. Don't "
        "guess. Recommend a next step (e.g. \"confirm with Joanne\", \"check "
        "the Chase feed\").\n"
        "- No exclamation points. No emojis. No motivational language.\n"
        "- End every reply with the sign-off:\n\n"
        f"— Keystone\n"
        f"Responded: {now_az}\n"
    )

    parts = [base]
    if knowledge:
        parts.append("\n\n---\n\n" + knowledge)
    parts.append(addendum)
    return "".join(parts)


# --- Anthropic client ------------------------------------------------------


_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


# --- Main entrypoint -------------------------------------------------------


def process_message(
    user_id: str,
    channel: str,
    text: str,
    thread_ts: str | None = None,
) -> None:
    """Run the full Q&A flow for one Slack message and post the reply.

    Called from a FastAPI BackgroundTasks runner so the webhook itself can
    ACK within Slack's 3-second window.
    """
    cleaned = _strip_bot_mention(text).strip()
    if not cleaned:
        _safe_reply(
            channel,
            "I didn't catch a question there. Ask me about cash, AR, or P&L "
            "and I'll pull it.",
            thread_ts=thread_ts,
            user_id=user_id,
        )
        return

    audience = audience_for_user(user_id)

    try:
        answer = _run_claude_loop(cleaned, audience=audience)
    except Exception as e:
        logger.exception("Q&A loop failed")
        now_az = datetime.now(AZ_TZ).strftime("%Y-%m-%d %H:%M %Z")
        answer = (
            "I couldn't answer that one. Reason: "
            f"{e}\n\n"
            "— Keystone\n"
            f"Responded: {now_az}"
        )

    _safe_reply(channel, answer, thread_ts=thread_ts, user_id=user_id)


def _strip_bot_mention(text: str) -> str:
    """Remove a leading <@BOTID> mention from @app_mention payloads."""
    if not text:
        return ""
    t = text.strip()
    if t.startswith("<@"):
        end = t.find(">")
        if end != -1:
            return t[end + 1 :].strip()
    return t


def _run_claude_loop(question: str, *, audience: str) -> str:
    """Run the tool-use loop until Claude returns a final text response.

    Caps at MAX_TOOL_ITERATIONS to avoid runaway tool chains.
    """
    client = _get_client()
    system = _qa_system_prompt(audience)

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": question},
    ]

    for iteration in range(MAX_TOOL_ITERATIONS):
        resp = client.messages.create(
            model=QA_MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        stop_reason = getattr(resp, "stop_reason", None)
        content_blocks = resp.content or []

        # If Claude is done (end_turn) and there are no tool_use blocks,
        # extract the final text and return.
        tool_uses = [b for b in content_blocks if getattr(b, "type", None) == "tool_use"]

        if stop_reason != "tool_use" and not tool_uses:
            return _extract_text(content_blocks) or _fallback_no_answer()

        # Otherwise Claude wants to call tools. Echo the assistant turn into
        # messages so the next request has full context.
        messages.append({"role": "assistant", "content": [_block_to_dict(b) for b in content_blocks]})

        tool_results: list[dict[str, Any]] = []
        for block in tool_uses:
            tool_name = block.name
            tool_args = block.input or {}
            logger.info("Q&A tool call: %s args=%s", tool_name, tool_args)
            result = _run_tool(tool_name, tool_args)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": _stringify_tool_result(result),
                }
            )

        messages.append({"role": "user", "content": tool_results})

    # Hit the iteration cap — ask Claude for a final answer with no more tools.
    logger.warning("Q&A hit max tool iterations; forcing final answer.")
    final = client.messages.create(
        model=QA_MODEL,
        max_tokens=MAX_TOKENS,
        system=system
        + "\n\nYou have exhausted the tool budget. Answer now with what you "
        "have. If the answer is incomplete, say so plainly.",
        messages=messages,
    )
    return _extract_text(final.content) or _fallback_no_answer()


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Convert an Anthropic content block (text or tool_use) into the dict
    shape required when echoing the assistant turn back into messages.
    """
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": block.text}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    # Fallback — best-effort dict-ification.
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return {"type": btype or "unknown"}


def _extract_text(content_blocks: list[Any]) -> str:
    parts: list[str] = []
    for b in content_blocks or []:
        if getattr(b, "type", None) == "text":
            parts.append(b.text)
    return "".join(parts).strip()


def _stringify_tool_result(result: Any) -> str:
    """Tool results get fed back to Claude as a string. Truncate to avoid
    blowing the context window on giant QBO reports.
    """
    import json

    try:
        s = json.dumps(result, default=str)
    except Exception:
        s = str(result)
    # Hard cap — QBO reports can be massive. 60k chars is plenty for one tool.
    MAX_CHARS = 60_000
    if len(s) > MAX_CHARS:
        s = s[:MAX_CHARS] + f"\n... [truncated, {len(s) - MAX_CHARS} more chars]"
    return s


def _fallback_no_answer() -> str:
    now_az = datetime.now(AZ_TZ).strftime("%Y-%m-%d %H:%M %Z")
    return (
        "I pulled the data but couldn't compose a clean answer. Try the "
        "question again with a tighter scope (specific date or report).\n\n"
        "— Keystone\n"
        f"Responded: {now_az}"
    )


# --- Slack reply -----------------------------------------------------------


def _safe_reply(
    channel: str,
    text: str,
    *,
    thread_ts: str | None = None,
    user_id: str | None = None,
) -> None:
    """Post to the Slack channel. For DMs (channel starts with 'D'), we send
    directly. For @mentions in a channel, we thread the reply.

    Falls back to send_dm by user_id if the direct post fails.
    """
    try:
        if thread_ts:
            # @mention reply — post into the thread so the channel isn't noisy.
            client = slack_client._get_client()  # noqa: SLF001 — internal helper reuse
            client.chat_postMessage(
                channel=channel,
                text=text,
                thread_ts=thread_ts,
                unfurl_links=False,
                unfurl_media=False,
            )
        else:
            # DM channel — post directly.
            slack_client.send_to_channel(channel, text)
    except Exception:
        logger.exception("Primary Slack post failed; falling back to DM")
        if user_id:
            try:
                slack_client.send_dm(user_id, text)
            except Exception:
                logger.exception("Fallback DM also failed for user %s", user_id)
