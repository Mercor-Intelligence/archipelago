"""Install the no-proactive-bind lifecycle onto a server (S3).

:func:`register_db_lifecycle` is the one call a server (or
:func:`mcp_middleware.run_server`) makes to wire lazy binding. It does NOT
bind anything — it registers the recipe and the trigger arms, and returns.
The DB is bound later, by whichever trigger fires first.

The two request-side trigger arms
---------------------------------

* **MCP / stdio tool arm** (installed here) — a FastMCP ``on_call_tool``
  middleware. On the stdio transport there is no HTTP status to hand back and
  no Starlette gate in the pipeline, so the first tool call must *block* until
  the DB is bound: this arm runs :func:`ensure_db_ready` (ruling D1, bounded
  synchronous ensure) off the event loop before delegating to the tool. Once
  READY it is a cheap no-op on every subsequent call. On the HTTP transport
  this same arm still fires, but the DB gate (default-CLOSED at boot, S4) sits
  *outside* it and 503s the request before it ever reaches tool dispatch while
  unbound — so the synchronous ensure only ever does real work on stdio (or on
  HTTP after the gate already opened, where it no-ops).

* **HTTP request arm** (primitive provided here, wired by S4/S6) — the
  operator LOCKED that the first HTTP request before any bind returns a 503 +
  ``Retry-After`` rather than holding the connection open through a bind. So
  the HTTP side does not ensure synchronously; instead the gate-closed path
  fires :func:`background_ensure`, a non-blocking one-shot that binds in a
  daemon thread. The current request 503s immediately; the client's
  ``Retry-After`` retry finds the gate open. This satisfies "the first REST
  request triggers a bind" without a synchronous hold.

The populate signal arm (Trigger 1) — the initial bind performed by
``adopt_late_delivery`` when a world is delivered — is S5; it reads the spec
back off the holder (:attr:`EngineHolder.spec`, stashed here).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Collection
from typing import Any

from .ensure import DEFAULT_ENSURE_TIMEOUT, ensure_db_ready
from .ready import DbLifecycleSpec, DbNotReadyError, DbReadyState, EngineHolder, engine_holder

logger = logging.getLogger(__name__)

__all__ = [
    "background_ensure",
    "register_db_lifecycle",
]


def register_db_lifecycle(
    mcp: Any,
    spec: DbLifecycleSpec,
    *,
    holder: EngineHolder | None = None,
    ensure_timeout: float = DEFAULT_ENSURE_TIMEOUT,
    bypass_tools: Collection[str] | None = None,
) -> EngineHolder:
    """Wire lazy binding onto ``mcp``. Binds nothing; returns the holder.

    Args:
        mcp: A FastMCP instance. Its ``add_middleware`` receives the MCP tool
            arm that performs the first-call synchronous ensure.
        spec: The declarative bind recipe. Stashed on the holder so the
            populate signal arm (S5) can bind from it too.
        holder: The :class:`EngineHolder` to bind against; defaults to the
            process-global :func:`engine_holder` singleton.
        ensure_timeout: Bound (seconds) the tool arm waits for a concurrent
            binder before raising. Forwarded to :func:`ensure_db_ready`.
        bypass_tools: Tool names that must NOT trigger a bind — DB-independent
            tools such as ``server_info`` / a login tool that the UI calls
            before any world exists. A call to a bypassed tool passes straight
            through without ensuring. ``None`` (default) ensures on every tool.

    Returns:
        The :class:`EngineHolder` the lifecycle is bound to.
    """
    h = holder if holder is not None else engine_holder()
    h.set_spec(spec)

    bypass = frozenset(bypass_tools or ())
    tool_arm = _make_ensure_tool_arm(spec, h, ensure_timeout, bypass)
    mcp.add_middleware(tool_arm)
    return h


def background_ensure(
    spec: DbLifecycleSpec,
    *,
    holder: EngineHolder | None = None,
    ensure_timeout: float = DEFAULT_ENSURE_TIMEOUT,
) -> bool:
    """Kick a one-shot, non-blocking bind if the holder is still UNBOUND.

    The HTTP-arm primitive (ruling: first HTTP request 503s, does not hold):
    the gate-closed path calls this so the DB starts binding in a daemon
    thread while the request returns 503 immediately. Idempotent-ish — returns
    ``False`` without starting a thread if the holder is already BINDING or
    READY. A few threads may start in the narrow UNBOUND window before
    :func:`ensure_db_ready`'s ``begin_binding`` claim flips the state; that is
    harmless (the losers just wait on ``wait_ready`` and exit — only one binds).

    Returns:
        ``True`` if a background bind thread was started, ``False`` if the
        holder was already binding / ready (nothing to do).
    """
    h = holder if holder is not None else engine_holder()
    if h.state is not DbReadyState.UNBOUND:
        return False

    def _run() -> None:
        try:
            ensure_db_ready(spec, holder=h, timeout=ensure_timeout)
        except DbNotReadyError as exc:
            # Lost the race and the winner is slow, or the winner rolled back.
            # A later request retries; nothing to do here but note it.
            logger.debug("background_ensure: not ready yet: %s", exc)
        except Exception:  # noqa: BLE001 - a background bind failure must not crash the thread
            logger.warning("background_ensure: bind failed", exc_info=True)

    thread = threading.Thread(target=_run, name="db-lifecycle-ensure", daemon=True)
    thread.start()
    return True


def _make_ensure_tool_arm(
    spec: DbLifecycleSpec,
    holder: EngineHolder,
    ensure_timeout: float,
    bypass: frozenset[str],
) -> Any:
    """Build the FastMCP ``on_call_tool`` arm that ensures on first tool call.

    FastMCP's ``Middleware`` base is imported lazily and used as a mixin (same
    shape as :mod:`.default_user_gate`) so importing this module never
    hard-requires a specific FastMCP version.
    """
    from fastmcp.server.middleware import Middleware as _FastMCPMiddleware

    class _EnsureToolArm(_FastMCPMiddleware):  # type: ignore[misc, valid-type]
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            # Fast path: already bound — skip straight to the tool.
            if holder.is_ready():
                return await call_next(context)

            tool_name = getattr(getattr(context, "message", None), "name", "")
            if tool_name in bypass:
                # DB-independent tool (server_info / login) — must not bind.
                return await call_next(context)

            # Bounded synchronous ensure (D1), off the event loop so a single
            # first-call bind (which opens an engine + may run cold_init) does
            # not stall the whole async runtime while it works.
            import anyio

            try:
                await anyio.to_thread.run_sync(
                    lambda: ensure_db_ready(spec, holder=holder, timeout=ensure_timeout)
                )
            except DbNotReadyError as exc:
                from fastmcp.exceptions import ToolError

                raise ToolError(
                    "runtime DB is not ready yet; retry shortly "
                    "(a request-triggered bind is non-terminal)"
                ) from exc
            return await call_next(context)

    return _EnsureToolArm()
