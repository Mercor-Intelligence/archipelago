"""Accept both flat and nested (input/request) tool argument envelopes."""

from __future__ import annotations

from typing import Any, override

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from loguru import logger
from mcp.types import CallToolRequestParams

_ENVELOPE_KEYS = ("input", "request")


def _wrap_flat_arguments(
    arguments: dict[str, Any] | None,
    properties: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if properties is None:
        return arguments

    prop_keys = set(properties)
    if isinstance(arguments, dict):
        for key in _ENVELOPE_KEYS:
            if key in arguments and key in prop_keys:
                return arguments

    if len(prop_keys) == 1:
        only = next(iter(prop_keys))
        if only in _ENVELOPE_KEYS and (arguments is None or only not in arguments):
            logger.debug("Wrapping flat tool arguments into {!r}", only)
            return {only: arguments or {}}

    return arguments


class EnvelopeCompatMiddleware(Middleware):
    """Wrap flat tool args into input/request when the schema expects a nested object."""

    def __init__(self, mcp: Any | None = None) -> None:
        super().__init__()
        self._mcp = mcp

    @override
    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        message = context.message
        arguments = getattr(message, "arguments", None)
        if (arguments is None or isinstance(arguments, dict)) and self._mcp is not None:
            try:
                tool = await self._mcp.get_tool(message.name)
            except Exception:
                tool = None
            params = getattr(tool, "parameters", None) if tool is not None else None
            properties = params.get("properties") if isinstance(params, dict) else None
            wrapped = _wrap_flat_arguments(arguments, properties)
            if wrapped is not arguments:
                message.arguments = wrapped
        return await call_next(context)
