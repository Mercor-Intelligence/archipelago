# pyright: reportPrivateUsage=false, reportMissingImports=false
"""Actor middleware honoring the EXPOSE_PHYSICAL_PATHS runtime flag."""

from collections.abc import Sequence
from contextvars import Token

from fastmcp.server.middleware import CallNext, MiddlewareContext

# Resolvable at runtime but not statically under the slim fastmcp dist — same
# import and same suppression as mercor-mcp-shared's mcp_actor.paths.
from fastmcp.tools.tool import Tool  # type: ignore[reportMissingImports]
from mcp_actor import paths as actor_paths
from utils.path_utils import physical_paths_enabled

# Appended to every served tool description so the calling agent is told which
# path form this server emits. The static parameter descriptions are kept
# mode-neutral; this is the single authoritative statement of the mode.
PHYSICAL_MODE_NOTE_TEMPLATE = (
    "Path mode: this server reports physical filesystem paths under the shared "
    "{root} mount, which is the same working directory the code-execution app "
    "uses. Pass paths returned by these tools to that app verbatim — do not "
    "rewrite or strip them. Paths rooted at '/' are also accepted as input."
)

VIRTUAL_MODE_NOTE = (
    "Path mode: this server reports virtualized paths rooted at '/', where '/' "
    "is the sandbox root. These are not host paths and are not meaningful "
    "outside this server."
)


def _mode_note() -> str:
    """The path-mode note for the currently bound actor.

    Resolved per listing rather than at import so it names the root actually in
    use. Only reachable with the physical root for the target agent /
    coordinator, since ``physical_paths_enabled`` is false for every VCA.
    """
    if not physical_paths_enabled():
        return VIRTUAL_MODE_NOTE
    return PHYSICAL_MODE_NOTE_TEMPLATE.format(root=actor_paths.active_filesystem_root())


class PathModeActorMiddleware(actor_paths.ActorMiddleware):
    """ActorMiddleware that skips output redaction in physical-path mode.

    Mirrors ``ActorMiddleware.on_call_tool`` (keep in sync with
    mercor-mcp-shared): binds the actor identity for one tool call, then
    redacts physical roots from outputs unless EXPOSE_PHYSICAL_PATHS is
    enabled for the bound actor. ``physical_paths_enabled`` is only ever true
    for the target agent / coordinator, so VCA outputs are always redacted.

    ``on_list_tools`` binds the same identity so served tool descriptions state
    the path form that actor will actually get. A VCA is always told paths are
    virtual, even while the env var is true, so descriptions never advertise
    the shared target-agent root to an actor that cannot see it.
    """

    @staticmethod
    def _bind_actor() -> Token[str | None]:
        actor_id = actor_paths.validate_actor_id(
            actor_paths.extract_bearer_actor_id(actor_paths._request_headers())
            or actor_paths.TARGET_AGENT_ACTOR_ID
        )
        return actor_paths._current_actor_id.set(actor_id)

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        token = self._bind_actor()
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

    async def on_list_tools(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Sequence[Tool]:
        token = self._bind_actor()
        try:
            tools = await call_next(context)
            note = _mode_note()
            # fastmcp 3.x hands out fresh Tool copies per listing, so updating
            # the copies here is both safe and the only thing that is served.
            return [
                tool.model_copy(
                    update={
                        "description": f"{tool.description.rstrip()}\n\n{note}"
                        if tool.description
                        else note
                    }
                )
                for tool in tools
            ]
        finally:
            actor_paths._current_actor_id.reset(token)
