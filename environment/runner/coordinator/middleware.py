from typing import cast

import mcp.types as mt
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from loguru import logger
from pydantic import JsonValue

from .agents.models import (
    COORDINATOR_ACTOR_ID_VALUE,
    TARGET_AGENT_ACTOR_ID_VALUE,
    TOOL_CALL_ACTOR_KEY,
)
from .runtime import get_coordinator

AUTHORIZATION_HEADER = b"authorization"
BEARER_PREFIX = "bearer "


def _get_bearer_token_from_request() -> str | None:
    try:
        request = get_http_request()
    except RuntimeError:
        return None
    for name, value in request.scope.get("headers", []):
        if name.lower() != AUTHORIZATION_HEADER:
            continue
        raw = value.decode(errors="replace").strip()
        if raw.lower().startswith(BEARER_PREFIX):
            return raw[len(BEARER_PREFIX) :].strip() or None
    return None


def _get_known_bearer_actor_id() -> str | None:
    actor_id = _get_bearer_token_from_request()
    if actor_id is None:
        return None
    if actor_id in {TARGET_AGENT_ACTOR_ID_VALUE, COORDINATOR_ACTOR_ID_VALUE}:
        return actor_id
    try:
        if actor_id in get_coordinator().store.config.read().agents:
            return actor_id
    except Exception:
        return None
    return None


def _get_actor_id_from_context(
    context: MiddlewareContext[mt.CallToolRequestParams],
) -> str:
    """
    Determine if the TA, VCAs, or the Coordinator made the tool call.

    Checks FastMCP request context if there's a VCA or Coordinator id,
    otherwise falls back to the TA.
    """
    request_metadata = (
        context.fastmcp_context.request_context.meta
        if context.fastmcp_context is not None
        and context.fastmcp_context.request_context is not None
        else context.message.meta
    )
    actor_id = getattr(request_metadata, TOOL_CALL_ACTOR_KEY, None)
    if isinstance(actor_id, str) and actor_id:
        return actor_id
    actor_id = _get_known_bearer_actor_id()
    if actor_id is not None:
        return actor_id
    return TARGET_AGENT_ACTOR_ID_VALUE


def _set_authorization_header(actor_id: str) -> None:
    """
    We use `Authorization: Bearer <token>` two different ways:

    - Internal TAs: For securing the Modal sandbox with
    `Sandbox.create_connect_token()`. External TAs do not do this.
    - VCAs: For user ID tenancy

    This wipes the Authorization header if it were a Modal sandbox token,
    and sets the actor_id.

    For example:
    - Internal TA <> MCP Gateway -- `Authorization: Bearer <Sandbox.create_connect_token()>`
    - MCP Gateway <> Foundry MCP -- `Authorization: Bearer <actor_id>`
    """

    try:
        request = get_http_request()
    except RuntimeError:
        return
    headers = [
        (name, value)
        for name, value in request.scope.get("headers", [])
        if name.lower() != AUTHORIZATION_HEADER
    ]
    headers.append((AUTHORIZATION_HEADER, f"Bearer {actor_id}".encode()))
    request.scope["headers"] = headers
    if hasattr(request, "_headers"):
        delattr(request, "_headers")


class CoordinatorToolCallMiddleware(Middleware):
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        tool_name = context.message.name
        arguments = cast(dict[str, JsonValue], context.message.arguments or {})
        actor_id = _get_actor_id_from_context(context)

        _set_authorization_header(actor_id)

        try:
            result = await call_next(context)
        except Exception as e:
            try:
                await get_coordinator().record_tool_call(
                    tool_name=tool_name,
                    arguments=arguments,
                    actor_id=actor_id,
                    error=repr(e),
                )
            except Exception as record_error:
                logger.error(
                    f"Environment Coordinator failed to record MCP call: {repr(record_error)}"
                )
            raise
        try:
            await get_coordinator().record_tool_call(
                tool_name=tool_name,
                arguments=arguments,
                actor_id=actor_id,
                result=result,
            )
        except Exception as e:
            logger.error(
                f"Environment Coordinator failed to record MCP call: {repr(e)}"
            )
        return result
