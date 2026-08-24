"""Trajectory utilities for extracting and analyzing agent trajectory data.

This module provides helpers for working with agent trajectories, including
extracting tool calls and their outputs from message histories.
"""

import json
import re
from collections.abc import Mapping
from typing import Any

from loguru import logger


def _content_text_parts(content: Any) -> list[str]:
    """Text carried by block-format content, in order.

    Empty when the content carries no text at all — a tool-call-only turn, say,
    or a value that is not iterable. That is what lets a caller tell "this turn
    said nothing" apart from "this turn's text is an empty string", which
    :func:`_content_to_str` alone cannot express: it falls back to ``str()``.
    """
    parts: list[str] = []
    try:
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
    except (TypeError, ValueError):
        return []
    return parts


def _content_to_str(content: Any) -> str:
    """Normalize message content to string. Handles list content blocks [{\"text\": \"...\"}]
    and Pydantic-validated iterables (e.g. ValidatorIterator from LiteLLM schema)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    # Handle list or any iterable that yields content blocks (e.g. from Pydantic validation)
    parts = _content_text_parts(content)
    if parts:
        return "\n".join(parts)
    return str(content)


def content_text(content: Any) -> str:
    """The text ``content`` carries, or "" when it carries none.

    Differs from :func:`_content_to_str` only in the no-text case: block content
    with no text blocks yields "" here instead of a ``str()`` repr of the
    blocks. Use this when scanning for the last turn that actually said
    something — a turn carrying only tool-use blocks, or only an empty text
    block, is a non-empty list either way, so raw truthiness cannot skip it.

    A mapping is excluded rather than walked. Iterating one yields its keys, and
    keys are strings, so the block walk would return "role\\ncontent" — noise
    shaped like an answer, which is the thing this function exists to withhold.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        return ""
    return "\n".join(_content_text_parts(content))


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
    resolve_lazy_content(last_msg)
    return _content_to_str(last_msg.get("content", ""))


def extract_first_user_message(trajectory: Any) -> str:
    """Return the text content of the first user message in the trajectory.

    This is the prompt the model was given. Handles both string content and
    block-format content lists. Returns an empty string if there is no
    non-empty user message.
    """
    for msg in getattr(trajectory, "messages", None) or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            resolve_lazy_content(msg)
            text = _content_to_str(msg.get("content", "")).strip()
            if text:
                return text
    return ""


def extract_last_assistant_text(trajectory: Any) -> str:
    """Return the text of the last assistant message that has non-empty text.

    Unlike ``extract_final_assistant_response`` (which only inspects the very
    last message), this scans backwards — so an agentic/multi-turn trajectory
    that ends on a tool or user message still yields the model's final answer
    text from its last assistant turn. Assistant turns that are tool-call-only
    (no text) are skipped.
    """
    for msg in reversed(getattr(trajectory, "messages", None) or []):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            resolve_lazy_content(msg)
            text = _content_to_str(msg.get("content", "")).strip()
            if text:
                return text
    return ""


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
                resolve_lazy_content(message)
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


# Platform gateway CLI: CLI-style agents (codex / cursor / claude_code / opencode /
# gemini_cli / toolbelt / antigravity, etc.) invoke MCP tools via a shell command
# `mcp_cli --method=execute --connector=<c> --tool=<NAME> --params=<JSON>` instead of a
# native tool call (grammar documented in archipelago/agents/runner/agents/registry.py).
# Without unwrapping, the recorded tool call is the shell runner (e.g. run_command) and
# the real tool is invisible to verifiers. Match on the gateway grammar only — no
# app/tool-specific names.
_GATEWAY_TOOL_RE = re.compile(r"--tool[=\s]+['\"]?([A-Za-z0-9_.\-]+)")
_GATEWAY_METHOD_RE = re.compile(r"--method[=\s]+['\"]?([A-Za-z]+)")
# mcp_cli only counts when it's an executed command (start of string or after a
# shell separator) — not when "mcp_cli" merely appears inside free-form text.
_GATEWAY_CMD_RE = re.compile(r"(?:^|[\n;&|])\s*mcp_cli\b")
# Only treat string values under command-ish keys as candidate shell commands, so a
# gateway-looking fragment pasted into a content/query field can't synthesize a call.
_COMMAND_KEY_RE = re.compile(
    r"command|cmd|shell|script|bash|terminal|exec|\brun\b", re.I
)


def _candidate_command_strings(arguments: str) -> list[str]:
    """Recover candidate shell-command strings from a tool call's arguments.

    A runner tool's arguments are usually JSON like {"command_line": "mcp_cli ..."}.
    Only string values under command-ish keys (command/cmd/shell/script/...) are
    returned, so a gateway-looking fragment pasted into an unrelated field (file
    content, search query, doc) can't be mistaken for an executed command. Falls
    back to the raw text when arguments aren't JSON (a bare command string).
    """
    if not arguments:
        return []
    try:
        obj = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return [arguments]
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [
            v
            for k, v in obj.items()
            if isinstance(v, str) and _COMMAND_KEY_RE.search(str(k))
        ]
    return [arguments]


def _extract_params_json(command: str) -> str | None:
    """Return the JSON object passed to ``--params`` via a quote-aware brace scan.

    Braces inside JSON string values are ignored so ``{``/``}`` in the data don't
    end the scan early.
    """
    marker = command.find("--params")
    if marker == -1:
        return None
    start = command.find("{", marker)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(command)):
        ch = command[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
        elif ch == '"':
            in_string = not in_string
        elif in_string:
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return command[start : i + 1]
    return None


def unwrap_gateway_cli_calls(
    tool_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Surface MCP tools invoked indirectly through the gateway CLI.

    Scans each extracted tool call for a ``mcp_cli --method=execute ... --tool=<NAME>
    --params=<JSON>`` invocation (the platform-standard gateway grammar) and returns
    synthesized tool-call dicts so consumers can match the *real* tool the agent called,
    not just the shell runner that wrapped it. Only ``--method=execute`` is treated as a
    call; discovery methods (``--method=list`` / ``--method=schema``) are skipped. Returns
    an empty list when no gateway calls are present, so the native path is unaffected.
    """
    synthesized: list[dict[str, Any]] = []
    for tc in tool_calls:
        arguments = tc.get("arguments") or ""
        if not isinstance(arguments, str) or "mcp_cli" not in arguments:
            continue
        for command in _candidate_command_strings(arguments):
            if not _GATEWAY_CMD_RE.search(command):
                continue
            # Parse flags from the segment before --params; the params value is the
            # only free-form data and must not be mistaken for a flag.
            params_at = command.find("--params")
            head = command if params_at == -1 else command[:params_at]
            method = _GATEWAY_METHOD_RE.search(head)
            if not method or method.group(1) != "execute":
                continue
            tool = _GATEWAY_TOOL_RE.search(head)
            if not tool:
                continue
            params = _extract_params_json(command)
            synthesized.append(
                {
                    "tool_call_id": tc.get("tool_call_id"),
                    "call_number": tc.get("call_number"),
                    "tool_name": tool.group(1),
                    "arguments": params if params is not None else "{}",
                    "output": tc.get("output"),
                }
            )
    return synthesized


def format_tool_calls_for_prompt(
    tool_calls: list[dict[str, Any]],
    include_outputs: bool = True,
    max_args_length: int = 500,
    max_output_length: int | None = None,
) -> str:
    """Format tool calls for inclusion in an LLM prompt.

    This is a shared utility for verifiers that need to include tool call
    information in their evaluation prompts.

    Args:
        tool_calls: List of tool calls from extract_tool_calls_with_outputs.
            Each dict has: call_number, tool_name, arguments, output
        include_outputs: Whether to include tool outputs in the formatted text
        max_args_length: Maximum length of arguments before truncation (default 500)
        max_output_length: Maximum length of each tool output before truncation.
            If None, outputs are not truncated.

    Returns:
        Formatted string with tool calls in markdown format, or a message
        indicating no tool calls were found.
    """
    if not tool_calls:
        return "(No tool calls found in trajectory)"

    formatted_parts = []
    for tc in tool_calls:
        args = tc.get("arguments", "")
        if len(args) > max_args_length:
            args = args[:max_args_length] + "... [TRUNCATED]"

        part = (
            f"### Tool Call {tc['call_number']}: {tc['tool_name']}\n"
            f"**Arguments:**\n```\n{args}\n```\n"
        )

        if include_outputs:
            output = tc.get("output") or "(no output captured)"
            if max_output_length and len(output) > max_output_length:
                # Truncate in the middle to show beginning and end
                half = max_output_length // 2
                output = output[:half] + "\n... [TRUNCATED] ...\n" + output[-half:]
            part += f"**Output:**\n```\n{output}\n```\n"

        formatted_parts.append(part)

    return "\n".join(formatted_parts)


def resolve_lazy_content(message: Any) -> None:
    """Replace lazily-validated message content with its concrete value, in place.

    A ``str | Iterable`` union validates into a pydantic ValidatorIterator
    (pydantic/pydantic#9541) that yields once. Every verifier of a run shares
    the message and they are gathered concurrently, so whichever reads first
    must restore what it drained — otherwise the next one reads an empty
    sequence and grades a blank conversation. Call this before serializing.

    The read-drain-writeback is not atomic, and it does not need to be *today*:
    the dispatch gathers coroutines (``runner/main.py``) with no ``to_thread``,
    and nothing here awaits, so no other verifier can observe the half-drained
    message. Both halves of that are load-bearing — an ``await`` inside this
    function, or a dispatch that moves to threads, reintroduces the interleaving
    this exists to prevent.

    A plain string validates into an iterator over its characters, so those are
    rejoined. ``"".join`` is right for that and only that: a concrete list is
    returned untouched above, and the content union's iterable branch is over
    block objects, so a list of bare strings does not survive validation. An
    all-strings drain therefore can only have come from a string. dict and bytes
    are iterable but yield keys and ints, so they are left alone, as are values
    that are already concrete.

    A block the union does not cover raises partway through the drain. What came
    out before the raise is already gone from the iterator, so the prefix is
    written back rather than discarded — leaving a spent iterator in place would
    hand the next verifier the truncated remainder, which is the same blank
    conversation reached by a raise instead of by a read.

    That write-back is lossy on purpose, and the loss is worth naming: for an
    ``[text, tool_use]`` turn whose ``tool_use`` block the union rejects, the
    text survives and the ``tool_use`` does not, so a later reader that inspects
    non-text blocks sees a one-element list. The alternative is a spent iterator,
    which gives every later reader nothing at all — recovering the text beats
    losing the turn, and the warning records that it happened.
    """
    is_dict = isinstance(message, dict)
    content = message.get("content") if is_dict else getattr(message, "content", None)
    if content is None or isinstance(content, str | list | dict | bytes):
        return
    parts: list[Any] = []
    try:
        for part in content:
            parts.append(part)
    except Exception:  # noqa: BLE001 - a malformed block must not break the payload
        role = (
            message.get("role") if is_dict else getattr(message, "role", None)
        ) or "unknown"
        # opt(exception=True) so the ValidationError's own message survives — the
        # type name alone does not say which block was rejected. The grade still
        # ships, computed on the truncated content, so this line is the only
        # trace that a score was formed on a partial transcript.
        logger.opt(exception=True).warning(
            f"resolve_lazy_content: drain failed on a {role} message after "
            f"{len(parts)} block(s); writing back the prefix, so any block after "
            f"the failing one is lost for later readers of this message"
        )
    resolved: Any = "".join(parts) if all(isinstance(p, str) for p in parts) else parts
    if is_dict:
        as_dict: Any = message
        as_dict["content"] = resolved
    else:
        # object.__setattr__ rather than assignment: the message may be frozen,
        # and a model that validates on assignment would hand back a fresh
        # lazy iterator, reintroducing exactly what this just resolved.
        object.__setattr__(message, "content", resolved)
