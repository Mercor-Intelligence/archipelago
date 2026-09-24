# pyright: reportPrivateUsage=false
"""Actor middleware honoring the EXPOSE_PHYSICAL_PATHS runtime flag."""

from fastmcp.server.middleware import CallNext, MiddlewareContext
from mcp_actor import paths as actor_paths
from utils.path_utils import physical_paths_enabled


class PathModeActorMiddleware(actor_paths.ActorMiddleware):
    """ActorMiddleware that skips output redaction in physical-path mode.

    Mirrors ``ActorMiddleware.on_call_tool`` (keep in sync with
    mercor-mcp-shared): binds the actor identity for one tool call, then
    redacts physical roots from outputs unless EXPOSE_PHYSICAL_PATHS is
    enabled for the bound actor. ``physical_paths_enabled`` is only ever true
    for the target agent / coordinator, so VCA outputs are always redacted.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        actor_id = actor_paths.validate_actor_id(
            actor_paths.extract_bearer_actor_id(actor_paths._request_headers())
            or actor_paths.TARGET_AGENT_ACTOR_ID
        )
        token = actor_paths._current_actor_id.set(actor_id)
        try:
            result = await call_next(context)
            if physical_paths_enabled():
                return result
            return actor_paths._redact_tool_result(result)
        except Exception as exc:
            if physical_paths_enabled():
                raise
            raise actor_paths._redact_exception(exc) from None
        finally:
            actor_paths._current_actor_id.reset(token)
