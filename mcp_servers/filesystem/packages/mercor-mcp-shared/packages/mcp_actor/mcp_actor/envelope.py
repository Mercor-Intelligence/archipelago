"""Accept both flat and nested (input/request) tool argument envelopes.

Document tools historically advertised a single nested object parameter
(``input`` / ``request``). Some deliveries and agents call them flat. Accepting
both at dispatch avoids train/eval mismatches without changing the Gemini-facing
input schema.
"""

from __future__ import annotations

from typing import Any, override

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from loguru import logger
from mcp.types import CallToolRequestParams

_ENVELOPE_KEYS = ("input", "request")
_INJECTED_PAGINATION_KEYS = frozenset({"page_number", "limit"})


def _unwrap_or_wrap_arguments(
    arguments: dict[str, Any] | None,
    properties: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not arguments or not properties:
        return arguments

    prop_keys = set(properties)
    real_prop_keys = prop_keys - _INJECTED_PAGINATION_KEYS
    # Already using the nested envelope the tool expects.
    for key in _ENVELOPE_KEYS:
        if key in arguments and key in prop_keys:
            return arguments

    # Tool expects exactly one envelope object param; caller sent flat fields.
    # ResponseLimiterMiddleware may have already injected page_number/limit into
    # the registry schema, so ignore those when detecting the original envelope.
    if len(real_prop_keys) == 1:
        only = next(iter(real_prop_keys))
        if only in _ENVELOPE_KEYS and only not in arguments:
            logger.debug("Wrapping flat tool arguments into '{}'", only)
            pagination_args = {
                key: value
                for key, value in arguments.items()
                if key in _INJECTED_PAGINATION_KEYS and key in prop_keys
            }
            envelope_args = {
                key: value for key, value in arguments.items() if key not in pagination_args
            }
            return {only: envelope_args, **pagination_args}

    return arguments


class EnvelopeCompatMiddleware(Middleware):
    """Wrap flat tool args into ``input``/``request`` when the schema expects it."""

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
        if isinstance(arguments, dict) and self._mcp is not None:
            try:
                tool = await self._mcp.get_tool(message.name)
            except Exception:
                tool = None
            params = getattr(tool, "parameters", None) if tool is not None else None
            properties = params.get("properties") if isinstance(params, dict) else None
            wrapped = _unwrap_or_wrap_arguments(arguments, properties)
            if wrapped is not arguments:
                message.arguments = wrapped
        return await call_next(context)
