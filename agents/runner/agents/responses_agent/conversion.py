"""Shared Chat Completions -> OpenAI Responses API conversion helpers.

These translate the loop's Chat-Completions-shaped message/tool history into the
Responses API `input`/`tools` shapes, and pull reasoning summaries back out of a
Responses API response. Kept here (rather than inside a single agent) so every
Responses-API agent uses one implementation.
"""

from __future__ import annotations

from typing import Any

from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam

from runner.agents.models import LitellmAnyMessage


def _get(msg: LitellmAnyMessage, key: str) -> Any:
    """Read a field from either a Pydantic message or a TypedDict message."""
    if hasattr(msg, "model_dump"):
        return getattr(msg, key, None)
    return msg.get(key)  # type: ignore[union-attr]


def _tool_output_text(content: Any, content_str: str) -> str:
    """Flatten a tool message's content into a string for function_call_output.

    Tool results may be stored as list-of-blocks (e.g. truncated text parts from
    the MCP gateway). ``str(list)`` would emit a Python repr, so join the text
    blocks explicitly; scalar content falls back to its string form.
    """
    if isinstance(content, list):
        return " ".join(
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return content_str


def _to_responses_content_blocks(content: list[Any]) -> list[Any]:
    """Translate Chat Completions content blocks to Responses API input blocks.

    The Responses API rejects Chat-Completions block types (``image_url``,
    ``text``, ``file``) on ``input[...].content`` — it wants ``input_image`` /
    ``input_text`` / ``input_file``. Some callers (e.g. the toolbelt-responses
    agent's file-prep) normalize before conversion, but the loop agent's
    deferred tool-image user messages arrive as raw ``image_url`` blocks. Mirror
    ``responses_agent_v2.utils.normalize_to_responses_format`` block handling and
    pass already-Responses / unknown blocks through unchanged (idempotent).
    """
    out: list[Any] = []
    for block in content:
        if isinstance(block, str):
            out.append({"type": "input_text", "text": block})
            continue
        if not isinstance(block, dict):
            out.append(block)
            continue
        btype = block.get("type")
        if btype == "text":
            out.append({"type": "input_text", "text": block.get("text", "")})
        elif btype == "image_url":
            image_url = block.get("image_url")
            if isinstance(image_url, str):
                out.append(
                    {"type": "input_image", "image_url": image_url, "detail": "auto"}
                )
            elif isinstance(image_url, dict) and image_url.get("url"):
                out.append(
                    {
                        "type": "input_image",
                        "image_url": image_url["url"],
                        "detail": image_url.get("detail", "auto"),
                    }
                )
            else:
                out.append(block)
        elif btype == "file":
            file_info = block.get("file") or {}
            file_block: dict[str, Any] = {"type": "input_file"}
            for k in ("file_data", "file_id", "filename"):
                if file_info.get(k):
                    file_block[k] = file_info[k]
            out.append(file_block)
        else:
            # Already-Responses blocks (input_text/input_image/input_file) and
            # anything unrecognized pass through unchanged.
            out.append(block)
    return out


def convert_messages_for_responses_api(
    messages: list[LitellmAnyMessage],
) -> list[dict[str, Any]]:
    """Convert Chat Completions format messages to Responses API input format.

    - system -> {"role": "developer", "content": "..."}
    - user -> {"role": "user", "content": "..."}  (list content passed through)
    - assistant (text only) -> {"role": "assistant", "content": "..."}
    - assistant with tool_calls -> function_call items
    - tool -> {"type": "function_call_output", "call_id": "...", "output": "..."}
    """
    converted: list[dict[str, Any]] = []

    for msg in messages:
        role = _get(msg, "role") or ""
        content = _get(msg, "content")
        tool_calls = _get(msg, "tool_calls")

        # Ensure content is always a string, never None
        content_str = str(content) if content else ""

        if role == "system":
            converted.append({"role": "developer", "content": content_str})

        elif role == "user":
            # Multimodal/file content is a list of blocks. Translate any
            # Chat-Completions block types (image_url/text/file) to Responses
            # input blocks; already-Responses blocks pass through unchanged. This
            # is what keeps the loop agent's deferred tool-image messages (raw
            # image_url) from hitting the Responses API as an invalid type.
            if isinstance(content, list):
                converted.append(
                    {"role": "user", "content": _to_responses_content_blocks(content)}
                )
            else:
                converted.append({"role": "user", "content": content_str})

        elif role == "assistant":
            if tool_calls:
                # Emit text content as assistant message if present
                if content_str:
                    converted.append({"role": "assistant", "content": content_str})

                # Convert each tool_call to a function_call item
                for tc in tool_calls:
                    if hasattr(tc, "id"):
                        # Pydantic ChatCompletionMessageToolCall
                        tc_id = getattr(tc, "id", "")
                        func = getattr(tc, "function", None)
                        tc_name = getattr(func, "name", "") if func else ""
                        tc_args = getattr(func, "arguments", "{}") if func else "{}"
                    else:
                        # dict format
                        tc_id = tc.get("id", "")
                        func = tc.get("function", {})
                        tc_name = func.get("name", "")
                        tc_args = func.get("arguments", "{}")

                    converted.append(
                        {
                            "type": "function_call",
                            "id": tc_id,
                            "call_id": tc_id,
                            "name": tc_name,
                            "arguments": tc_args or "{}",
                        }
                    )
            else:
                converted.append({"role": "assistant", "content": content_str})

        elif role == "tool":
            # Convert tool message to function_call_output. Flatten list-block
            # content to text so tool outputs aren't repr-stringified.
            tool_call_id = _get(msg, "tool_call_id")
            converted.append(
                {
                    "type": "function_call_output",
                    "call_id": tool_call_id or "",
                    "output": _tool_output_text(content, content_str),
                }
            )

        else:
            # Pass through unknown roles as-is
            converted.append({"role": role, "content": content_str})

    return converted


def to_responses_tool(tool: ChatCompletionToolParam) -> dict[str, Any]:
    """Convert a ChatCompletionToolParam to Responses API tool format.

    Chat Completions: {"type": "function", "function": {"name", "description", "parameters"}}
    Responses API:    {"type": "function", "name", "description", "parameters"}
    """
    func = tool.get("function", {})
    result: dict[str, Any] = {
        "type": "function",
        "name": func.get("name"),
    }
    desc = func.get("description")
    if desc:
        result["description"] = desc
    params = func.get("parameters")
    if params:
        result["parameters"] = params
    return result


def extract_reasoning_summary(response: Any) -> str | None:
    """Extract reasoning summary from Responses API output items.

    The Responses API returns reasoning items with a `summary` field containing
    summary_text items when reasoning.summary is configured (e.g. "concise"/"auto").
    The shared parse_responses_api_output only checks the `content` field with
    reasoning_text items, so this handles the `summary` path.
    """
    response_dict = (
        response.model_dump() if hasattr(response, "model_dump") else dict(response)
    )
    parts: list[str] = []
    for item in response_dict.get("output", []) or []:
        if item.get("type") != "reasoning":
            continue
        # Collect this item's text; the content fallback is scoped per-item so a
        # summary on an earlier item doesn't suppress a later item's content text.
        item_parts: list[str] = []
        # Primary: summary field (summary_text items)
        summary = item.get("summary") or []
        if isinstance(summary, list):
            for s in summary:
                if isinstance(s, dict) and s.get("type") == "summary_text":
                    text = s.get("text", "")
                    if text:
                        item_parts.append(text)
                elif hasattr(s, "type") and getattr(s, "type", None) == "summary_text":
                    text = getattr(s, "text", "")
                    if text:
                        item_parts.append(text)
        elif isinstance(summary, str) and summary:
            item_parts.append(summary)
        # Fallback: this item's content field (reasoning_text items)
        if not item_parts:
            for c in item.get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "reasoning_text":
                    text = c.get("text", "")
                    if text:
                        item_parts.append(text)
        parts.extend(item_parts)
    return "\n".join(parts) if parts else None
