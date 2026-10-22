"""Middleware that sanitizes Pydantic validation errors for LLM agents.

Intercepts verbose ``ValidationError`` messages (which include type metadata
and ``https://errors.pydantic.dev/`` URLs) and re-raises them as concise,
URL-free ``ValueError`` instances so that upstream ``ErrorHandlingMiddleware``
still classifies them as *invalid-params* (``-32602``) rather than
*internal-error* (``-32603``).
"""

from typing import override

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from loguru import logger
from mcp.types import CallToolRequestParams
from pydantic import ValidationError as PydanticValidationError


def format_validation_error(exc: PydanticValidationError) -> str:
    """Format a Pydantic ValidationError into a concise, URL-free string."""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(segment) for segment in err["loc"])
        msg = err["msg"]
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "Validation error: " + "; ".join(parts)


class ValidationErrorSanitizerMiddleware(Middleware):
    """Catches Pydantic ``ValidationError`` and re-raises a concise ``ValueError``."""

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
            logger.debug(
                f"Sanitized validation error for {context.message.name}: {clean}"
            )
            raise ValueError(clean) from None
