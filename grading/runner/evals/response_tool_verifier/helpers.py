"""
Helper functions for the Response Tool Verifier.

Contains utility functions for:
- Base64 decoding and text extraction
- Task prompt extraction from trajectory
- Agent response extraction and formatting
- Tool artifact formatting
"""

import base64
import re
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput


def decode_base64_content(content: str) -> str:
    """
    Decode base64 content from data URI format to plain text.

    This ensures we never inject raw base64 strings into the grading prompt.
    The LLM judge should see readable text, not encoded data.

    Handles formats like:
    - data:text/plain;base64,<base64_data>
    - data:text/plain;base64,<base64_data> additional text after...

    Returns:
        Decoded text content, or original content if not base64/decoding fails.
    """
    if not content:
        return ""

    if content.startswith("data:") and ";base64," in content:
        try:
            parts = content.split(";base64,", 1)
            if len(parts) == 2:
                remaining = parts[1]

                # The base64 data might be followed by additional text
                # Common patterns:
                # 1. "base64data The content is below:\n..."
                # 2. "base64data\n\nSome text..."
                # 3. Just "base64data"

                # Find where base64 ends (base64 chars: A-Z, a-z, 0-9, +, /, =)
                base64_chars = set(
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
                )
                base64_end = 0
                for i, char in enumerate(remaining):
                    if char in base64_chars:
                        base64_end = i + 1
                    elif char in " \n\t":
                        break
                    else:
                        break

                base64_part = remaining[:base64_end].strip()
                trailing_text = remaining[base64_end:].strip()

                if base64_part:
                    decoded = base64.b64decode(base64_part).decode("utf-8")

                    if trailing_text:
                        trailing_text = trailing_text.lstrip(" \n\t")
                        if trailing_text.startswith("The content is below:"):
                            trailing_text = trailing_text[
                                len("The content is below:") :
                            ].strip()

                        if trailing_text:
                            return f"{decoded}\n\n[Additional context: {trailing_text}]"

                    return decoded

        except Exception as e:
            logger.warning(f"Failed to decode base64 content: {e}")
            return f"[Base64 content - decoding failed: {str(e)[:100]}]"

    if len(content) > 100:
        stripped = content.strip()
        base64_like = re.fullmatch(r"[A-Za-z0-9+/=]+", stripped)
        if base64_like:
            try:
                decoded = base64.b64decode(stripped, validate=True).decode("utf-8")
                return decoded
            except Exception:
                pass

    return content


def ensure_no_base64_in_text(text: str) -> str:
    """
    Scan text for any remaining base64 patterns and decode them.

    This is a safety check to ensure no base64 strings slip through to the prompt.
    """
    if not text:
        return text

    if "data:" in text and ";base64," in text:
        pattern = r"data:[^;]+;base64,[A-Za-z0-9+/=]+"
        matches = re.findall(pattern, text)

        for match in matches:
            decoded = decode_base64_content(match)
            text = text.replace(match, decoded)

    return text


def extract_task_prompt(input: EvalImplInput) -> str:
    """
    Extract the task prompt from trajectory messages.

    The task prompt is the first user message in the trajectory.
    """
    if not input.trajectory or not input.trajectory.messages:
        return "(No task prompt found)"

    for msg in input.trajectory.messages:
        if msg.get("role") == "user" and msg.get("content"):
            content = msg.get("content")
            return str(content) if content else "(Empty user message)"

    return "(No user message found)"


def extract_agent_response(input: EvalImplInput) -> str:
    """
    Extract the agent's response from trajectory.

    Looks for assistant messages and decodes any base64 content.
    Ensures NO base64 strings are present in the final output.
    """
    if not input.trajectory or not input.trajectory.messages:
        return "(No response found)"

    responses = []
    for msg in input.trajectory.messages:
        if msg.get("role") == "assistant":
            content = msg.get("content")
            if content:
                decoded = decode_base64_content(str(content))
                decoded = ensure_no_base64_in_text(decoded)
                responses.append(decoded)

    if not responses:
        return "(No assistant response found)"

    return "\n\n---\n\n".join(responses)


def format_tool_artifacts(output: dict[str, Any] | None) -> tuple[str, str]:
    """
    Format tool artifacts for display in the prompt.

    Only includes the actual tool outputs (text responses, file content).
    All base64 content is decoded to plain text before formatting.

    Returns:
        Tuple of (formatted_string, artifact_names_for_display)
    """
    if not output:
        return "(No tool artifacts)", ""

    tool_artifacts = output.get("tool_artifacts") or {}

    formatted_parts = []
    artifact_names = []

    text_responses = tool_artifacts.get("text_responses") or []
    for i, resp in enumerate(text_responses, 1):
        decoded = decode_base64_content(str(resp))
        decoded = ensure_no_base64_in_text(decoded)
        formatted_parts.append(decoded)
        artifact_names.append(f"tool_output_{i}")

    if not formatted_parts:
        return "(No tool artifacts found)", ""

    result = "\n\n---\n\n".join(formatted_parts)
    result = ensure_no_base64_in_text(result)

    return result, ", ".join(artifact_names)
