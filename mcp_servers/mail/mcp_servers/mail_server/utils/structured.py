"""Structured output for the individual mail tools.

Every tool returns text that agents already read and that the meta-tools and
tests parse, so that text must not change. A tool therefore returns a
``StructuredText``: a ``str`` holding exactly the text it returned before,
carrying the response model the text was rendered from (or, where the text was
written by hand, a model built from the same values).

``structured_tool`` adapts such a tool for MCP registration: the text goes out
as the content block, unchanged, and the model goes out as
``structuredContent`` matching the tool's declared ``outputSchema``. Callers
that use the tool function directly still get a plain string.
"""

import functools
from collections.abc import Awaitable, Callable
from typing import Any, Self

from fastmcp.tools import ToolResult
from mcp.types import TextContent
from pydantic import BaseModel


class StructuredText(str):
    """Tool text that also carries the structured payload behind it."""

    structured: BaseModel

    def __new__(cls, text: str, structured: BaseModel) -> Self:
        obj = super().__new__(cls, text)
        obj.structured = structured
        return obj


def structured(model: BaseModel) -> StructuredText:
    """The model's display text, carrying the model itself."""
    return StructuredText(str(model), model)


def output_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The outputSchema to declare for a tool whose payload is ``model``.

    Validation-mode schema, so a field with a default is not required: the
    payload omits ``None`` values rather than sending ``null``, which keeps it
    valid after ``flatten_schema`` collapses ``X | None`` to plain ``X``.
    """
    return model.model_json_schema(by_alias=True)


def structured_content(model: BaseModel) -> dict[str, Any]:
    """Serialize a payload the way ``output_schema`` describes it."""
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def structured_tool(
    fn: Callable[..., Awaitable[str]],
) -> Callable[..., Awaitable[ToolResult]]:
    """Wrap a ``StructuredText``-returning tool for registration with FastMCP.

    The wrapper keeps the tool's name, docstring and signature (FastMCP follows
    ``__wrapped__``), so its input schema is unchanged.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> ToolResult:
        result = await fn(*args, **kwargs)
        if not isinstance(result, StructuredText):
            raise TypeError(f"{fn.__name__} returned text without a structured payload")
        return ToolResult(
            content=[TextContent(type="text", text=str(result))],
            structured_content=structured_content(result.structured),
        )

    return wrapper
