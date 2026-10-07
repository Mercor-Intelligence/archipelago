"""Typed MCP output (outputSchema + structuredContent) for text-returning tools.

Every individual docs tool answers with a human-readable text block, and agents read
that text. This module adds a typed ``structuredContent`` payload next to it without
changing a byte of the text:

* On success a tool returns :func:`structured` instead of ``str(result)``. That is a
  ``str`` subclass holding the exact text plus the JSON dump of the model the text was
  rendered from, so direct callers (tests, the ``docs`` meta-tool) still get a plain
  string.
* Error paths keep returning a plain ``str``. :func:`structured_tool` reports those as
  ``{"status": "error", "error": <the same text>}``.
* :func:`output_schema_for` publishes the matching schema: a ``oneOf`` over the success
  model(s) and the error shape, written so ``mcp_schema.flatten_schema`` (run over every
  output schema in ``main.py``) leaves it intact. ``flatten_schema`` would otherwise turn
  ``X | None`` into a bare ``X`` and a union into its first member, and a client
  validating ``structuredContent`` would then reject legitimate nulls and variants.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any, Literal, Union

from fastmcp.tools import ToolResult
from mcp.types import TextContent
from pydantic import BaseModel, Field, TypeAdapter


class StructuredText(str):
    """The exact text a tool returns, carrying its structured payload alongside."""

    structured: dict[str, Any]

    def __new__(cls, text: str, structured: dict[str, Any]) -> StructuredText:
        obj = super().__new__(cls, text)
        obj.structured = structured
        return obj


def structured(result: BaseModel, text: str | None = None) -> StructuredText:
    """Return ``text`` (default ``str(result)``) with ``result`` as structured content."""
    return StructuredText(
        str(result) if text is None else text, result.model_dump(mode="json")
    )


class ToolErrorOutput(BaseModel):
    """Structured content for a call that returned an error message."""

    status: Literal["error"] = Field(
        ..., description="Always 'error' when the operation did not succeed"
    )
    error: str = Field(
        ..., description="The error message; identical to the text content"
    )


def _to_structured_content(result: str) -> dict[str, Any]:
    if isinstance(result, StructuredText):
        return result.structured
    return ToolErrorOutput(status="error", error=result).model_dump(mode="json")


def structured_tool(
    fn: Callable[..., Awaitable[str]],
) -> Callable[..., Awaitable[ToolResult]]:
    """Wrap a text-returning tool so it also emits structured content.

    The wrapper keeps ``fn``'s name, docstring and parameters, so the published
    input schema is unchanged; only the return type differs.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> ToolResult:
        result = await fn(*args, **kwargs)
        return ToolResult(
            content=[TextContent(type="text", text=str(result))],
            structured_content=_to_structured_content(result),
        )

    signature = inspect.signature(fn)
    wrapper.__signature__ = signature.replace(return_annotation=ToolResult)  # type: ignore[attr-defined]
    wrapper.__annotations__ = {
        **getattr(fn, "__annotations__", {}),
        "return": ToolResult,
    }
    return wrapper


def _normalize(node: Any, defs: dict[str, Any]) -> Any:
    """Inline ``$ref`` and rewrite ``anyOf`` into forms ``flatten_schema`` keeps."""
    if isinstance(node, list):
        return [_normalize(item, defs) for item in node]
    if not isinstance(node, dict):
        return node

    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        target = deepcopy(defs[ref.split("/")[-1]])
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        return _normalize({**target, **siblings}, defs)

    out = {
        key: _normalize(value, defs)
        for key, value in node.items()
        if key not in ("$defs", "anyOf")
    }

    any_of = node.get("anyOf")
    if isinstance(any_of, list):
        branches = [_normalize(branch, defs) for branch in any_of]
        non_null = [b for b in branches if b.get("type") != "null"]
        nullable = len(non_null) < len(branches)
        if len(non_null) == 1 and isinstance(non_null[0].get("type"), str):
            # Optional[X] -> {"type": [X, "null"]}, which flatten_schema leaves alone.
            merged = {**non_null[0], **out}
            if nullable:
                merged["type"] = [non_null[0]["type"], "null"]
            return merged
        if len(non_null) == 1:
            # Optional[Any] or similar with no declared type: any value is allowed.
            return {**non_null[0], **out}
        out["oneOf"] = non_null + ([{"type": "null"}] if nullable else [])
    return out


def output_schema_for(*success_models: type[BaseModel]) -> dict[str, Any]:
    """Output schema accepting any of ``success_models`` or :class:`ToolErrorOutput`.

    ``oneOf`` is exact here: every success model requires fields the error shape lacks
    and vice versa, and the variants of a multi-action tool each require fields the
    others lack.
    """
    variants = (*success_models, ToolErrorOutput)
    raw = TypeAdapter(Union[variants]).json_schema(mode="serialization")  # noqa: UP007
    schema = _normalize(raw, raw.get("$defs", {}))
    return {"type": "object", **schema}
