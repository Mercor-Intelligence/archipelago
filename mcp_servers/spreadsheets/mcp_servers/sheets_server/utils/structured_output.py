"""Typed MCP output for tools whose contract is a text result.

Every individual tool returns a ``str`` -- the rendered ``__str__`` of a
response model on success, or a plain error message.  Agents and the
``sheets`` meta-tool depend on that text, so it must not change.  This module
adds a typed ``structuredContent`` *alongside* the unchanged text:

* Tools return :func:`structured_text(response) <structured_text>` instead of
  ``str(response)``.  The result is still a ``str`` with identical contents, so
  every existing caller is unaffected; it also carries the response model.
* :func:`with_structured_output` wraps a tool at registration time and emits
  the same text plus ``structuredContent`` built from that model.  A plain
  ``str`` result is a returned error and becomes ``{"error": <text>}``.
* :func:`tool_output_model` builds the declared output schema: every response
  field optional plus ``error``, so both shapes validate.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from pydantic import BaseModel, ConfigDict, Field, create_model

# Response-model fields that only steer how the text is rendered; they are not
# facts about the spreadsheet, so they are left out of structuredContent.
RENDER_ONLY_FIELDS = frozenset({"compact"})

ERROR_DESCRIPTION = (
    "Error message when the call failed. Identical to the text result. "
    "When set, no other field is present."
)


class StructuredText(str):
    """A tool's text result that also carries the response model it renders."""

    model: BaseModel

    def __new__(cls, model: BaseModel) -> StructuredText:
        instance = super().__new__(cls, str(model))
        instance.model = model
        return instance


def structured_text(model: BaseModel) -> str:
    """Return ``str(model)`` that also remembers ``model`` for structuredContent."""
    return StructuredText(model)


def tool_output_model(
    name: str, doc: str, *responses: type[BaseModel]
) -> type[BaseModel]:
    """Build an output model: all fields of ``responses`` optional, plus ``error``.

    Fields keep their descriptions.  When several response models are given
    (a tool that returns one of several shapes), their fields are merged;
    the first model declaring a field name wins.
    """
    fields: dict[str, Any] = {}
    for response in responses:
        for field_name, info in response.model_fields.items():
            if field_name in RENDER_ONLY_FIELDS or field_name in fields:
                continue
            annotation = Any if info.annotation is None else info.annotation
            fields[field_name] = (
                annotation | None,
                Field(default=None, description=info.description),
            )
    fields["error"] = (str | None, Field(default=None, description=ERROR_DESCRIPTION))
    return create_model(
        name,
        __config__=ConfigDict(extra="forbid"),
        __doc__=doc,
        **fields,
    )


def structured_content_for(result: str) -> dict[str, Any]:
    """Build structuredContent for a tool's text result."""
    if isinstance(result, StructuredText):
        return result.model.model_dump(
            mode="json", exclude=set(RENDER_ONLY_FIELDS), fallback=str
        )
    return {"error": str(result)}


def with_structured_output(
    fn: Callable[..., Awaitable[str]],
) -> Callable[..., Awaitable[ToolResult]]:
    """Wrap a text tool so it also returns structuredContent.

    The text block is exactly what ``fn`` returned.  The wrapper keeps ``fn``'s
    name, docstring and signature, so the registered tool name and input
    schema are unchanged.
    """

    @wraps(fn)
    async def tool(*args: Any, **kwargs: Any) -> ToolResult:
        result = await fn(*args, **kwargs)
        return ToolResult(
            content=[TextContent(type="text", text=str(result))],
            structured_content=structured_content_for(result),
        )

    return tool
