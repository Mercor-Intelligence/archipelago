"""Trajectory utilities for extracting and analyzing agent trajectory data.

This module provides helpers for working with agent trajectories, including
extracting tool calls and their outputs from message histories.
"""

from typing import Any


def _content_to_str(content: Any) -> str:
    """Normalize message content to string. Handles list content blocks [{\"text\": \"...\"}]
    and Pydantic-validated iterables (e.g. ValidatorIterator from LiteLLM schema)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    # Handle list or any iterable that yields content blocks (e.g. from Pydantic validation)
    try:
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        if parts:
            return "\n".join(parts)
    except (TypeError, ValueError):
        pass
    return str(content)


def extract_final_assistant_response(input: Any) -> str:
    """Return the text content of the last assistant message in the trajectory.

    Handles both string content and block-format content lists
    (``[{"type": "text", "text": "..."}]``). Returns an empty string if the
    trajectory is absent or the last message is not from the assistant role.
    """
    trajectory = getattr(input, "trajectory", None)
    if trajectory is None or not trajectory.messages:
        return ""
    last_msg = trajectory.messages[-1]
    if last_msg.get("role") != "assistant":
        return ""
    return _content_to_str(last_msg.get("content", ""))


def extract_tool_calls_with_outputs(
    messages: list[Any],
) -> list[dict[str, Any]]:
    """
    Extract all tool calls from a trajectory with their corresponding outputs.

    Parses a list of chat messages to find all tool calls made by the agent
    (from assistant messages) and matches them with their outputs (from tool
    messages).

    Args:
        messages: List of chat messages in OpenAI format. Expected roles:
            - "assistant" with "tool_calls" field for tool invocations
            - "tool" with "tool_call_id" field for tool responses

    Returns:
        A list of dicts with:
        - call_number: Sequential number of the tool call (1, 2, 3, ...)
        - tool_call_id: The unique ID of the tool call
        - tool_name: The name of the tool
        - arguments: The arguments passed to the tool (as string)
        - output: The output/response from the tool (from role="tool" messages),
                  or None if no matching output found

    Example:
        >>> messages = [
        ...     {"role": "user", "content": "Search for X"},
        ...     {"role": "assistant", "tool_calls": [
        ...         {"id": "call_1", "function": {"name": "search", "arguments": "{}"}}
        ...     ]},
        ...     {"role": "tool", "tool_call_id": "call_1", "content": "Results..."},
        ... ]
        >>> extract_tool_calls_with_outputs(messages)
        [{"call_number": 1, "tool_name": "search", "arguments": "{}", "output": "Results..."}]
    """
    # First pass: collect all tool calls with their IDs (in order)
    tool_calls_ordered: list[tuple[str, dict[str, Any]]] = []

    for message in messages:
        role = message.get("role")
        tool_calls = message.get("tool_calls")

        # Extract tool calls from assistant messages
        if role == "assistant" and tool_calls:
            for tc in tool_calls:
                # Handle both object and dict formats
                if isinstance(tc, dict):
                    tc_id = tc.get("id", "")
                    name = tc.get("function", {}).get("name", "")
                    arguments = tc.get("function", {}).get("arguments", "")
                else:
                    tc_id = getattr(tc, "id", "") or ""
                    func = getattr(tc, "function", None)
                    name = func.name if func else ""
                    arguments = func.arguments if func else ""

                if tc_id and name:
                    tool_calls_ordered.append(
                        (
                            tc_id,
                            {
                                "tool_call_id": tc_id,
                                "tool_name": name,
                                "arguments": arguments,
                                "output": None,  # Will be filled in second pass
                            },
                        )
                    )

    # Build a lookup dict for second pass
    tool_calls_by_id = {tc_id: data for tc_id, data in tool_calls_ordered}

    # Second pass: match tool outputs to tool calls
    for message in messages:
        role = message.get("role")
        if role == "tool":
            # Get tool_call_id from the message
            tc_id = message.get("tool_call_id")
            if tc_id and tc_id in tool_calls_by_id:
                raw = message.get("content")
                if raw is None:
                    tool_calls_by_id[tc_id]["output"] = "(empty output)"
                else:
                    normalized = _content_to_str(raw)
                    tool_calls_by_id[tc_id]["output"] = (
                        normalized if normalized else "(empty output)"
                    )

    # Return as list with sequential call numbers (1, 2, 3, ...)
    result = []
    for i, (_tc_id, data) in enumerate(tool_calls_ordered, start=1):
        data["call_number"] = i
        result.append(data)
    return result
