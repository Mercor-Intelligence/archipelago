"""Typed structured output for the text-returning tools.

Each text tool keeps producing exactly the text it always has: agents read the
text content block, and changing it would change every trajectory. On top of
that text a tool attaches a Pydantic model holding the same facts, built from
the same values the text was built from, which the server publishes as the
tool's ``outputSchema`` and returns as ``structuredContent``.

The text travels as :class:`StructuredText`, a ``str`` subclass. Calling a tool
function directly therefore still returns a string equal to the old one, so
direct callers keep working, while :func:`structured_tool` (the form registered
with the server) splits it into the text block and the structured payload.

Payloads are dumped with ``exclude_none``: ``mcp_schema.flatten_schema``
(applied to every served schema in ``main.py``) turns ``X | None`` into a bare
``X`` plus the non-standard ``nullable`` flag, so an emitted ``null`` would
fail client-side validation. Omitting the key instead always validates, which
is why any field a payload can lack (including every success field on the
error path) is optional.
"""

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp.tools import ToolResult
from mcp.types import TextContent
from pydantic import BaseModel


class StructuredText[M: BaseModel](str):
    """The exact text a tool returns, carrying its structured equivalent."""

    data: M

    def __new__(cls, text: str, data: M) -> "StructuredText[M]":
        obj = super().__new__(cls, text)
        obj.data = data
        return obj


def output_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The JSON schema advertised as a tool's ``outputSchema``."""
    return model.model_json_schema()


def to_tool_result(result: StructuredText[Any]) -> ToolResult:
    """Split a tool's StructuredText into its text block and structured content."""
    if not isinstance(result, StructuredText):
        # Every return path of a structured tool must attach its data; a bare
        # string here is a programming error, not something to paper over with
        # a payload that would not match the advertised schema.
        raise TypeError(
            f"structured tool returned {type(result).__name__}, expected StructuredText"
        )
    return ToolResult(
        content=[TextContent(type="text", text=str(result))],
        structured_content=result.data.model_dump(mode="json", exclude_none=True),
    )


def structured_tool[**P](
    fn: Callable[P, Awaitable[StructuredText[Any]]],
) -> Callable[P, Awaitable[ToolResult]]:
    """Wrap a tool so the server returns its text plus structured content.

    Name, docstring and parameter signature are carried over unchanged, so the
    served tool name, description and input schema are exactly the wrapped
    function's.
    """

    @functools.wraps(fn)
    async def tool(*args: P.args, **kwargs: P.kwargs) -> ToolResult:
        return to_tool_result(await fn(*args, **kwargs))

    setattr(  # noqa: B010 - __signature__ is not a declared attribute
        tool,
        "__signature__",
        inspect.signature(fn).replace(return_annotation=ToolResult),
    )
    return tool
