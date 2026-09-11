from typing import cast

import mcp.types as mt
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from loguru import logger
from pydantic import JsonValue

from runner.utils.metrics import increment

from .agents.models import (
    COORDINATOR_ACTOR_ID_VALUE,
    COORDINATOR_DISPATCH_ORIGIN,
    TARGET_AGENT_ACTOR_ID_VALUE,
    TOOL_CALL_ACTOR_KEY,
    TOOL_CALL_ORIGIN_KEY,
)
from .runtime import get_coordinator

AUTHORIZATION_HEADER = b"authorization"
BEARER_PREFIX = "bearer "

# Attribution headers stamped by the runner's `build_mcp_gateway_schema`, mapping the
# wire header name (ASGI lowercases them) to the field name used in the audit log. Read
# only for attribution — they carry non-secret opaque ids + int versions and never gate
# access, so a missing or garbled header degrades to "no attribution", never an error.
_AGENT_IDENTITY_HEADERS: dict[bytes, str] = {
    b"x-agent-id": "agent_id",
    b"x-agent-version": "agent_version",
    b"x-orchestrator-id": "orchestrator_id",
    b"x-orchestrator-version": "orchestrator_version",
}


def _get_agent_identity_from_request() -> dict[str, str]:
    try:
        request = get_http_request()
    except RuntimeError:
        return {}
    identity: dict[str, str] = {}
    for name, value in request.scope.get("headers", []):
        field = _AGENT_IDENTITY_HEADERS.get(name.lower())
        if field is None:
            continue
        decoded = value.decode(errors="replace").strip()
        if decoded:
            identity[field] = decoded
    return identity


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


def _get_tool_call_origin(
    context: MiddlewareContext[mt.CallToolRequestParams],
) -> str | None:
    """Origin marker for the call (mirrors ``_get_actor_id_from_context``).

    Set by the Coordinator on its own direct tool-call dispatches so shadow-mode
    authz can exclude them from per-persona attribution.
    """
    request_metadata = (
        context.fastmcp_context.request_context.meta
        if context.fastmcp_context is not None
        and context.fastmcp_context.request_context is not None
        else context.message.meta
    )
    origin = getattr(request_metadata, TOOL_CALL_ORIGIN_KEY, None)
    return origin if isinstance(origin, str) and origin else None


def _shadow_check_persona_tool_authz(
    actor_id: str, tool_name: str, agent_identity: dict[str, str], outcome: str
) -> None:
    """Shadow-mode per-agent tool authorization (SECPRJ-1646). Observe only.

    When a persona (VCA) calls a tool outside its declared ``allowed_tool_names``,
    emit a "would-deny" signal so we can measure per-agent least-privilege
    violations before ever enforcing them (the first "control" rung after the
    attribution work, SECPRJ-1338/1599). ``outcome`` records whether the task
    allowlist admitted the call ("admitted") or it failed / was rejected ("error"),
    so PR 2's baseline can weight them — a persona reaching for a tool that 401s or
    is task-blocked is exactly a case worth capturing.

    Deliberately non-enforcing and fail-open: the whole body is wrapped so any error
    here can never affect the tool call. Non-VCA actors (TA / coordinator), a
    disabled coordinator, and personas with no declared allowlist (the default) all
    produce no signal — byte-for-byte prior behavior.
    """
    # Cheap discriminator before touching the config: the TA and coordinator are
    # never persona-scoped, and they account for most tool-call volume.
    if actor_id in (TARGET_AGENT_ACTOR_ID_VALUE, COORDINATOR_ACTOR_ID_VALUE):
        return
    # Phase 1 — decide. Fail-open, but surface errors at warning (not debug) rather
    # than silently dropping the signal. Never propagates into the tool call.
    try:
        coordinator = get_coordinator()
        # Mirror record_tool_call's gate: don't emit during the start/stop windows
        # where the enabled config is on disk but the coordinator isn't live.
        if not coordinator.is_started:
            return
        config = coordinator.store.config.read()
        if not config.enabled:
            return
        vca = config.agents.get(actor_id)
        # Empty/absent allowlist == "unscoped" (no signal), NOT "deny all" — same
        # convention as the per-task allowlist (gateway.py), so an accidentally empty
        # list can't flag every call and bury the baseline.
        if vca is None or not vca.allowed_tool_names:
            return
        # Exact match on the fully-namespaced observed tool name. Suffix matching
        # (used to hide tools) would over-grant: a grant of "read" must not admit
        # "secrets_read" from a different mounted server.
        if tool_name in vca.allowed_tool_names:
            return
        allowed_count = len(vca.allowed_tool_names)
    except Exception as e:
        logger.warning(f"Shadow tool-authz check errored (skipped): {e!r}")
        return
    # Phase 2 — emit the would-deny. Its own fail-open block so a broken emitter is
    # visible (at warning) instead of silently swallowed, and still never breaks the
    # tool call. Emit only on a would-deny (rare) to keep per-call emits off the hot
    # path (the metrics client is fire-and-forget but built for low-volume use); a
    # rate denominator can be recovered from record_tool_call volume if needed.
    try:
        # tool_name is agent-controlled and unbounded (esp. on the error path, where
        # the tool may not even exist), so it is NOT a metric tag — only bounded dims
        # are. High-cardinality values (tool_name, agent_id/version) go to the log.
        increment(
            "studio.mcp.tool_authz.shadow_violation",
            tags=["actor_kind:vca", f"outcome:{outcome}"],
        )
        logger.bind(
            message_type="tool_authz_shadow_violation",
            vca_id=actor_id,
            tool_name=tool_name,
            outcome=outcome,
            allowed_count=allowed_count,
            **agent_identity,
        ).warning(
            "Per-agent tool authorization (shadow) would deny "
            + f"actor={actor_id} tool={tool_name!r} outcome={outcome} "
            + f"(persona allows {allowed_count} tools)"
        )
    except Exception as e:
        logger.warning(f"Shadow tool-authz emit failed: {e!r}")


class CoordinatorToolCallMiddleware(Middleware):
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        tool_name = context.message.name
        arguments = cast(dict[str, JsonValue], context.message.arguments or {})
        actor_id = _get_actor_id_from_context(context)
        agent_identity = _get_agent_identity_from_request()
        # Coordinator-dispatched direct tool actions aren't the persona's own tool
        # choice, so exclude them from shadow attribution (SECPRJ-1646).
        shadow_eligible = _get_tool_call_origin(context) != COORDINATOR_DISPATCH_ORIGIN

        _set_authorization_header(actor_id)

        try:
            result = await call_next(context)
        except Exception as e:
            # A would-deny that also 401s / doesn't exist / is task-blocked is exactly
            # a case worth capturing in the baseline. Observe-only; never suppresses
            # the raise below.
            if shadow_eligible:
                _shadow_check_persona_tool_authz(
                    actor_id, tool_name, agent_identity, outcome="error"
                )
            try:
                await get_coordinator().record_tool_call(
                    tool_name=tool_name,
                    arguments=arguments,
                    actor_id=actor_id,
                    agent_identity=agent_identity,
                    error=repr(e),
                    error_type=type(e).__name__,
                )
            except Exception as record_error:
                logger.error(
                    f"Environment Coordinator failed to record MCP call: {repr(record_error)}"
                )
            raise
        if shadow_eligible:
            _shadow_check_persona_tool_authz(
                actor_id, tool_name, agent_identity, outcome="admitted"
            )
        try:
            await get_coordinator().record_tool_call(
                tool_name=tool_name,
                arguments=arguments,
                actor_id=actor_id,
                agent_identity=agent_identity,
                result=result,
            )
        except Exception as e:
            logger.error(
                f"Environment Coordinator failed to record MCP call: {repr(e)}"
            )
        return result
