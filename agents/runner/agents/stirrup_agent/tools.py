"""
Tools for the Stirrup agent

Implements the finish tool with Stirrup's exact schema
- reason: str - reason for finishing
- paths: list[str] - list of file paths created or modified

Plus ``abandon_task`` (AA GDPval-AA v2), the second terminal tool. Upstream
Stirrup models this class of tool explicitly — ``constants.py`` notes that
"custom finish tools may use any name; only the default is bound to this
constant" (``DEFAULT_FINISH_TOOL_NAME = "finish"``) and its base system prompt
says to call "a finish tool", not "the finish tool". So abandonment is a
terminal tool of the same class as ``finish``, not a special case: it ends the
run, and the harness treats reaching it as the model having deliberately
concluded (see ``main.py``'s ``_finalized`` handling), never as a failure.

It carries only ``reason`` — no ``paths`` — because abandoning is precisely
declining to submit files.
"""

import json
from typing import Any

from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam

FINISH_TOOL_NAME = "finish"
ABANDON_TASK_TOOL_NAME = "abandon_task"

FINISH_TOOL: ChatCompletionToolParam = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": (
            "Signal task completion. Call this tool when the task is finished or "
            "cannot proceed further. IMPORTANT: The 'reason' parameter you pass to "
            "this tool IS your final answer - it will be extracted and used as your "
            "response. Any text you write before or after calling this tool will be "
            "ignored. Put your complete final answer, summary, or explanation directly "
            "in the 'reason' parameter. Do not write your answer outside this tool call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "Your complete final answer or response to the task. This is "
                        "the ONLY text that will be returned to the user. Include all "
                        "relevant details, summaries, and conclusions here."
                    ),
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of file paths created or modified. "
                        "Do not include directories, only files."
                    ),
                },
            },
            "required": ["reason", "paths"],
        },
    },
}


def parse_finish_tool(arguments: str) -> tuple[str, list[str]]:
    """
    Parse finish tool arguments

    Args:
        arguments: json string of tool arguments

    Returns:
        tuple of (reason, paths)
    """
    try:
        args: dict[str, Any] = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        return arguments, []

    if not isinstance(args, dict):
        return str(args), []

    reason = str(args.get("reason", ""))
    paths_raw = args.get("paths", [])
    paths = [str(p) for p in paths_raw] if isinstance(paths_raw, list) else []

    return reason, paths


ABANDON_TASK_TOOL: ChatCompletionToolParam = {
    "type": "function",
    "function": {
        "name": ABANDON_TASK_TOOL_NAME,
        "description": (
            "Abandon the task. Call this tool INSTEAD of finish when you do not "
            "believe the task can be completed - for example the required data "
            "is unavailable, the request is self-contradictory, or the tools "
            "provided cannot accomplish it. Give a brief reason explaining what "
            "blocked you. This ends the run without submitting any files, so do "
            "not call it merely because the task is difficult or you are running "
            "low on turns - in those cases keep working and then call finish "
            "with your best effort."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "A brief explanation of why the task cannot be "
                        "completed. This is the only text that will be returned."
                    ),
                },
            },
            "required": ["reason"],
        },
    },
}


def parse_abandon_tool(arguments: str) -> str:
    """
    Parse abandon_task tool arguments

    Mirrors ``parse_finish_tool``'s tolerance: malformed JSON degrades to the raw
    argument string rather than raising, because a terminal tool that throws
    would turn a deliberate abandonment into an unhandled harness error.

    Args:
        arguments: json string of tool arguments

    Returns:
        the reason string
    """
    try:
        args: dict[str, Any] = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        return arguments

    if not isinstance(args, dict):
        return str(args)

    return str(args.get("reason", ""))
