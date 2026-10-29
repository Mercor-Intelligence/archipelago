"""Render an agent's starting messages for its ``configure`` log line."""

from __future__ import annotations

from typing import Any

CONTEXT_PREVIEW_CHARS = 500
_TEXT_BLOCK_TYPES = frozenset({"text", "input_text", "output_text"})


def _text(content: Any) -> str:
    if not isinstance(content, list):
        return "" if content is None else str(content)
    return "\n".join(
        str(block.get("text", ""))
        if isinstance(block, dict) and block.get("type", "text") in _TEXT_BLOCK_TYPES
        else f"[{block.get('type', 'block') if isinstance(block, dict) else 'block'}]"
        for block in content
    )


def _render(messages: list[Any]) -> str:
    return "\n\n".join(
        f"{m['role'].capitalize()}: {_text(m.get('content'))}" for m in messages
    )


def _is_prior_work(message: Any) -> bool:
    # An authored, empty assistant placeholder is not a prior turn.
    if message["role"] == "tool":
        return True
    return message["role"] == "assistant" and bool(
        message.get("content") or message.get("tool_calls")
    )


def format_initial_messages(messages: list[Any]) -> str:
    """Head and tail of the earlier turns, then everything after the last reply or tool result in full."""
    last_reply = max(
        (i for i, m in enumerate(messages) if _is_prior_work(m)), default=-1
    )
    new = _render(messages[last_reply + 1 :])
    if last_reply < 0:
        return new
    earlier = _render(messages[: last_reply + 1])
    if len(earlier) > 2 * CONTEXT_PREVIEW_CHARS:
        omitted = len(earlier) - 2 * CONTEXT_PREVIEW_CHARS
        earlier = (
            f"{earlier[:CONTEXT_PREVIEW_CHARS]}\n\n"
            f"[… {omitted:,} characters omitted …]\n\n"
            f"{earlier[-CONTEXT_PREVIEW_CHARS:]}"
        )
    return (
        f"Earlier context ({last_reply + 1} message(s)):\n\n{earlier}\n\n"
        f"New this turn ({len(messages) - last_reply - 1} message(s)):\n\n{new}"
    )
