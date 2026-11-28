"""
Tool result processing for handling large outputs.

JSON results are shrunk structurally and stay valid JSON; other text keeps head_tail.
"""

import json
from typing import Any

from litellm import token_counter
from loguru import logger

from runner.agents.models import LitellmInputMessage

# Defaults for head_tail truncation
# With ReSum context summarization, we can afford larger results
MAX_RESULT_TOKENS = 24000  # ~24k tokens before truncation
HEAD_CHARS = 20000  # Keep first 20k chars
TAIL_CHARS = 5000  # Keep last 5k chars

# Absolute maximum - if result exceeds this even after truncation, return error
# This prevents absurdly large results from ever being added to context
ABSOLUTE_MAX_CHARS = 100000

JSON_SHRINK_ATTEMPTS = 4
JSON_TRUNCATION_NOTE = (
    "Tool result exceeded {max_tokens:,} tokens, so trailing entries were dropped "
    "to fit; `result` holds what was kept and `truncation_dropped` lists each cut. "
    "Use filters or pagination to read the rest."
)


def _estimate_tokens(model: str, text: str) -> int:
    """Estimate token count for text content."""
    try:
        return token_counter(model=model, text=text)
    except Exception:
        return len(text) // 4


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _fit_json(value: Any, budget: int, path: str, cuts: list[dict[str, Any]]) -> Any:
    """Return a copy of ``value`` whose compact JSON fits ``budget`` chars, best effort.

    Trailing list items, trailing dict keys and string tails are dropped; each cut
    is appended to ``cuts``.
    """
    size = len(_dumps(value))
    if size <= budget:
        return value

    if isinstance(value, str):
        lo, hi = 0, len(value)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(_dumps(value[:mid])) <= budget:
                lo = mid
            else:
                hi = mid - 1
        keep = lo
        cuts.append(
            {"path": path, "kept": keep, "dropped": len(value) - keep, "unit": "chars"}
        )
        return value[:keep]

    if isinstance(value, list) and value:
        sizes = [len(_dumps(item)) + 1 for item in value]
        used, keep = 1, 0
        while keep < len(value) and used + sizes[keep] <= budget:
            used += sizes[keep]
            keep += 1
        if keep == 0:
            kept = [_fit_json(value[0], budget - 2, f"{path}[0]", cuts)]
            keep = 1
        else:
            kept = value[:keep]
        if keep < len(value):
            cuts.append(
                {
                    "path": path,
                    "kept": keep,
                    "dropped": len(value) - keep,
                    "unit": "items",
                }
            )
        return kept

    if isinstance(value, dict):
        sizes = {key: len(_dumps(item)) for key, item in value.items()}
        shrunk = dict(value)
        key_cuts: dict[str, list[dict[str, Any]]] = {}
        for key in sorted(sizes, key=sizes.__getitem__, reverse=True):
            child_budget = budget - (size - sizes[key])
            if size <= budget or child_budget <= 0:
                break
            key_cuts[key] = []
            shrunk[key] = _fit_json(
                value[key], child_budget, f"{path}.{key}", key_cuts[key]
            )
            new_size = len(_dumps(shrunk[key]))
            size += new_size - sizes[key]
            sizes[key] = new_size
        kept = shrunk
        if size > budget:
            used, kept = 1, {}
            for key, item in shrunk.items():
                entry = len(_dumps(key)) + 2 + sizes[key]
                if used + entry > budget:
                    child_budget = budget - used - entry + sizes[key]
                    if child_budget > 0 and key not in key_cuts:
                        key_cuts[key] = []
                        kept[key] = _fit_json(
                            value[key], child_budget, f"{path}.{key}", key_cuts[key]
                        )
                    break
                kept[key] = item
                used += entry
            cuts.append(
                {
                    "path": path,
                    "kept": len(kept),
                    "dropped": len(shrunk) - len(kept),
                    "unit": "keys",
                }
            )
        for key in kept:
            cuts.extend(key_cuts.get(key, []))
        return kept

    return value


def _shrink_json(text: str, model: str, tokens: int) -> str | None:
    """Return ``text`` shrunk to valid JSON within MAX_RESULT_TOKENS and
    ABSOLUTE_MAX_CHARS, or None if it is not a JSON container or string, or cannot fit."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict | list | str):
        return None

    budget = min(
        int(MAX_RESULT_TOKENS * len(text) / max(tokens, 1)), ABSOLUTE_MAX_CHARS
    )
    note = JSON_TRUNCATION_NOTE.format(max_tokens=MAX_RESULT_TOKENS)
    try:
        for _ in range(JSON_SHRINK_ATTEMPTS):
            cuts: list[dict[str, Any]] = []
            result = _fit_json(data, budget, "$", cuts)
            if not cuts:
                processed = _dumps(result)
            else:
                processed = _dumps(
                    {
                        "truncated": True,
                        "truncation_note": note,
                        "truncation_dropped": cuts,
                        "result": result,
                    }
                )
            over = max(
                _estimate_tokens(model, processed) / MAX_RESULT_TOKENS,
                len(processed) / ABSOLUTE_MAX_CHARS,
            )
            if over <= 1:
                return processed
            budget = int(min(budget, len(processed)) / over * 0.95)
    except RecursionError:
        return None
    return None


def _truncate_text(text: str, model: str) -> str | None:
    """
    Truncate text if it exceeds limits. Returns truncated text or None if no change needed.

    JSON is shrunk structurally (see _shrink_json); other text keeps its first
    HEAD_CHARS and last TAIL_CHARS.
    """
    # Check for absurdly large results first
    if len(text) > ABSOLUTE_MAX_CHARS * 2:
        logger.bind(message_type="tool_result").error(
            f"Tool result is extremely large ({len(text):,} chars), truncating"
        )
        return (
            f"Error: Tool returned extremely large output ({len(text):,} characters). "
            f"This exceeds the maximum allowed size. "
            f"Try a more specific query or break down the request."
        )

    tokens = _estimate_tokens(model, text)
    if tokens <= MAX_RESULT_TOKENS:
        return None  # No truncation needed

    logger.bind(message_type="tool_result").warning(
        f"Tool result is too large ({tokens} tokens > {MAX_RESULT_TOKENS}), truncating"
    )

    processed = _shrink_json(text, model, tokens)
    if processed is None:
        if len(text) <= HEAD_CHARS + TAIL_CHARS:
            return None  # Content is fine as-is

        head = text[:HEAD_CHARS]
        tail = text[-TAIL_CHARS:]
        omitted = len(text) - HEAD_CHARS - TAIL_CHARS
        processed = (
            f"{head}\n\n"
            f"[... {omitted:,} characters omitted. "
            f"Use more specific queries to access full data. ...]\n\n"
            f"{tail}"
        )

    # Final safety check - ensure truncated result is within absolute max
    if len(processed) > ABSOLUTE_MAX_CHARS:
        logger.bind(message_type="tool_result").error(
            f"Truncated result still too large ({len(processed):,} chars)"
        )
        return (
            f"Error: Tool output too large even after truncation. "
            f"Original: {len(text):,} chars. Try a more specific query."
        )

    return processed


def _truncate_content_list(content: list[Any], model: str) -> None:
    """Truncate text blocks within a content list. Mutates in place."""
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "text":
            continue  # Preserve images and other types

        text = item.get("text", "")
        if not isinstance(text, str):
            continue

        truncated = _truncate_text(text, model)
        if truncated is not None:
            item["text"] = truncated


def truncate_tool_messages(
    messages: list[LitellmInputMessage],
    model: str,
) -> None:
    """
    Truncate text content in tool messages, preserving everything else (images, etc).

    Handles both:
    - Dict messages with list content: [{"type": "text", "text": "..."}]
    - Dict messages with string content (legacy)

    Mutates messages in place.
    """
    for msg in messages:
        content = msg.get("content")

        if isinstance(content, list):
            # New format: content is array of blocks
            _truncate_content_list(content, model)

        elif isinstance(content, str):
            # Legacy format: content is string
            truncated = _truncate_text(content, model)
            if truncated is not None:
                msg["content"] = truncated
