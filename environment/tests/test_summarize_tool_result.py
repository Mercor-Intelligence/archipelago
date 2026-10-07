"""Tests for summarize_tool_result — the shape written to mcp_calls.jsonl."""

from dataclasses import dataclass
from typing import Any, cast

from fastmcp.tools import ToolResult

from runner.coordinator.utils import (
    MAX_STRUCTURED_CONTENT_BYTES,
    summarize_tool_result,
)


@dataclass
class FakeTextContent:
    text: str


@dataclass
class _FakeResult:
    """Stands in for ToolResult / CallToolResult — only two attributes are read."""

    content: list[Any] | None
    structured_content: dict[str, Any] | None


def FakeResult(
    content: list[Any] | None, structured_content: dict[str, Any] | None
) -> ToolResult:
    """A structural stand-in, cast to satisfy the annotation.

    Building a real ToolResult would pull in content-block validation that has
    nothing to do with what this function reads.
    """
    return cast(ToolResult, cast(object, _FakeResult(content, structured_content)))


def test_structured_content_is_preserved() -> None:
    # The case this exists for: a send response naming who it reached.
    summary = summarize_tool_result(
        FakeResult(
            content=[FakeTextContent(text="sent")],
            structured_content={"delivered_to_user_ids": ["U1", "U2"]},
        )
    )

    assert summary["structured_content"] == {"delivered_to_user_ids": ["U1", "U2"]}
    # The old key stays, so existing readers are untouched.
    assert summary["has_structured_content"] is True
    assert summary["text"] == "sent"
    assert summary["content_items"] == 1


def test_oversized_structured_content_is_dropped() -> None:
    # One line per tool call per actor, so a large payload keeps the old shape
    # rather than growing the log without bound.
    oversized = {"blob": "x" * (MAX_STRUCTURED_CONTENT_BYTES + 1)}
    summary = summarize_tool_result(
        FakeResult(content=[], structured_content=oversized)
    )

    assert summary["has_structured_content"] is True
    assert "structured_content" not in summary


def test_unserializable_structured_content_is_dropped() -> None:
    summary = summarize_tool_result(
        FakeResult(content=[], structured_content={"obj": object()})
    )

    assert summary["has_structured_content"] is True
    assert "structured_content" not in summary


def test_absent_structured_content_reports_neither_key() -> None:
    summary = summarize_tool_result(
        FakeResult(content=[FakeTextContent(text="hi")], structured_content=None)
    )

    assert "has_structured_content" not in summary
    assert "structured_content" not in summary
    assert summary == {"content_items": 1, "text": "hi"}


def test_text_capping_is_unchanged() -> None:
    summary = summarize_tool_result(
        FakeResult(
            content=[FakeTextContent(text="a" * 600) for _ in range(5)],
            structured_content=None,
        )
    )

    # Three items, each truncated to 500 characters.
    assert summary["text"] == "\n".join(["a" * 500] * 3)
    assert summary["content_items"] == 5
