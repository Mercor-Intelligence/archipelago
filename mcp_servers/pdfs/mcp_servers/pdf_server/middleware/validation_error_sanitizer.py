"""Middleware that sanitizes Pydantic validation errors for LLM agents.

Intercepts verbose ``ValidationError`` messages (which include type metadata
and ``https://errors.pydantic.dev/`` URLs) and re-raises them as concise,
URL-free ``ValueError`` instances that the MCP SDK wraps with ``isError=True``.
"""

from typing import override

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from loguru import logger
from mcp.types import CallToolRequestParams
from pydantic import ValidationError as PydanticValidationError

# Pydantic renders ``model_type`` as "Input should be a valid dictionary or
# instance of <class>", naming the model the tool signature happens to use. The
# caller sends JSON and cannot act on a Python class name, so the whole message
# is replaced rather than having the name cut out of it -- removing the name
# leaves a dangling "or instance of".
_OBJECT_EXPECTED = "Input should be a valid JSON object"


def format_validation_error(exc: PydanticValidationError) -> str:
    """Format a Pydantic ValidationError into a concise, URL-free string."""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(segment) for segment in err["loc"])
        msg = _OBJECT_EXPECTED if err["type"] == "model_type" else err["msg"]
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
            # ValueError, not a bare Exception: ErrorHandlingMiddleware maps
            # ValueError/TypeError to -32602 "Invalid params" and everything
            # else to -32603 "Internal error", so a rejected payload was being
            # reported to the agent as a server fault. ``from None`` is
            # load-bearing for the same reason -- that middleware reads
            # ``type(error.__cause__)`` in preference to the raised type.
            raise ValueError(clean) from None
