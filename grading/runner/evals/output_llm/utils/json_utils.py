"""Utilities for parsing LLM JSON responses defensively."""

import re

_FENCE_RE = re.compile(
    r"^```(?:json|JSON)?\s*\n?(.*?)\n?\s*```$",
    re.DOTALL,
)


def strip_json_fences(text: str) -> str:
    """Strip Markdown code fences from an LLM JSON response.

    Some providers (notably Anthropic via LiteLLM) wrap structured
    JSON output in Markdown code fences even when response_format
    is set to json_object, because LiteLLM simulates json mode for
    providers that do not natively enforce it. This helper makes
    JSON parsing tolerant to that wrapping.

    The function is idempotent: bare JSON is returned unchanged.
    """
    if not text:
        return text
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    return match.group(1).strip() if match else stripped
