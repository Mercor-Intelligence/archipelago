"""Middleware to sanitize Pydantic validation errors returned to LLM agents.

When an LLM sends malformed tool arguments, Pydantic raises a ValidationError
with verbose messages including type metadata and documentation URLs like:

    4 validation errors for call[extract_html_table]
    request
      Missing required argument [type=missing_argument, input_value=...]
        For further information visit https://errors.pydantic.dev/2.12/v/missing_argument

This middleware intercepts those errors and reformats them as concise messages:

    Validation error: request: Missing required argument; ticker: Unexpected keyword argument
"""

from typing import override

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from loguru import logger
from mcp.types import CallToolRequestParams
from pydantic import ValidationError as PydanticValidationError


def format_validation_error(exc: PydanticValidationError) -> str:
    """Format a Pydantic ValidationError into a concise, URL-free string.

    Extracts each error's location and message from ``e.errors()``,
    skipping the type metadata, input values, and documentation URLs
    that Pydantic includes by default.
    """
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(segment) for segment in err["loc"])
        msg = err["msg"]
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "Validation error: " + "; ".join(parts)


class ValidationErrorSanitizerMiddleware(Middleware):
    """Catches Pydantic ``ValidationError`` from tool calls and re-raises
    a plain ``Exception`` with a concise, URL-free message.

    The MCP SDK's generic ``except Exception`` handler then wraps it with
    ``isError=True``, preserving error semantics while keeping the message
    short for LLM context windows.
    """

    @override
    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except PydanticValidationError as exc:
            clean = format_validation_error(exc)
            logger.debug(f"Sanitized validation error for {context.message.name}: {clean}")
            raise Exception(clean) from None  # noqa: TRY002
