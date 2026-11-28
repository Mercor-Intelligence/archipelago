"""Answer-text helpers shared by the final-answer graders."""

from __future__ import annotations

import re
from typing import cast

# Only the tail of a long final message can hold the answer line.
MAX_ANSWER_CHARS = 10_000

_EMPHASIS = "*_`~"
_QUOTE_PREFIX = re.compile(r"^\s*(?:>\s?)+")


def extract_answer(
    final_message: str, marker: str, limit: int | None = MAX_ANSWER_CHARS
) -> str:
    """The first line after the last ``marker`` (case-insensitive), else the whole message.

    A wrapped marker (`**Final answer:**`) keeps its closing run: it is skipped only when it
    mirrors the emphasis run directly before the marker. The answer text itself is never edited.
    In a blockquoted reply the quote prefixes of the lines after the marker line are ignored.
    Only the last ``limit`` characters are searched; ``None`` searches all of it.
    """
    if limit is not None:
        final_message = final_message[-limit:]
    hits = (
        list(re.finditer(re.escape(marker), final_message, re.IGNORECASE))
        if marker
        else []
    )
    if not hits:
        return final_message.strip()
    last = hits[-1]
    end = last.end()
    opener = last.start()
    while opener > 0 and final_message[opener - 1] in _EMPHASIS:
        opener -= 1
    closer = final_message[opener : last.start()][::-1]
    if closer and final_message.startswith(closer, end):
        end += len(closer)
    lines = final_message[end:].splitlines()
    if lines:
        line_start = final_message.rfind("\n", 0, last.start()) + 1
        if final_message[line_start : last.start()].lstrip().startswith(">"):
            lines[1:] = [_QUOTE_PREFIX.sub("", line, count=1) for line in lines[1:]]
    return next((line.strip() for line in lines if line.strip()), "")


def as_text(value: object) -> str:
    """A field value as text; ``None`` is empty, while ``0`` and ``False`` are values."""
    return "" if value is None else str(value).strip()


def as_text_list(value: object) -> list[str]:
    """Accepted variants from a list field, one per line from a text field, or one scalar."""
    if isinstance(value, list):
        items = [as_text(v) for v in cast(list[object], value)]
    elif isinstance(value, str):
        items = value.splitlines()
    elif isinstance(value, (int, float)):
        items = [as_text(value)]
    else:
        return []
    return [v.strip() for v in items if v.strip()]
