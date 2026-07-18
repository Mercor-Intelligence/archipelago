"""Core MCP gateway logic for building and hot-swapping MCP apps.

This module handles creating FastMCP proxy ASGI apps and hot-swapping them
in the FastAPI application without restarting the server.
"""

import asyncio
import contextlib
import os
import time
from collections.abc import Sequence
from typing import Any, override

import mcp.types as mt
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from fastmcp import Client as FastMCPClient
from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.providers.proxy import ProxyClient, StatefulProxyClient
from fastmcp.server.server import create_proxy
from fastmcp.tools import Tool, ToolResult
from loguru import logger
from starlette.routing import Mount

from runner.coordinator.middleware import CoordinatorToolCallMiddleware
from runner.utils.tool_names import tool_counts_by_server, tool_name_matches

from .models import (
    MCPSchema,
    ServerReadinessDetails,
)
from .state import (
    StatefulProxyHandle,
    get_current_stateful,
    get_mcp_lifespan_manager,
    get_mcp_lock,
    get_mcp_mount,
    set_current_stateful,
    set_mcp_lifespan_manager,
    set_mcp_mount,
)


class _AllowedToolsMiddleware(Middleware):
    """Hide and reject MCP tools not in an allowlist.

    Used for per-task tool filtering: list_tools returns only allowed tools,
    and call_tool raises if a disallowed tool is invoked.
    """

    def __init__(self, allowed_tool_names: Sequence[str]) -> None:
        self._allowed: set[str] = set(allowed_tool_names)

    @override
    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        return [
            tool
            for tool in tools
            if any(
                tool_name_matches(
                    configured_tool_name=allowed,
                    observed_tool_name=tool.name,
                )
                for allowed in self._allowed
            )
        ]

    @override
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        if not any(
            tool_name_matches(
                configured_tool_name=allowed,
                observed_tool_name=context.message.name,
            )
            for allowed in self._allowed
        ):
            allowed_count = len(self._allowed)
            raise ValueError(
                f"Tool {context.message.name!r} is not in the allowlist ({allowed_count} tools allowed)"
            )
        return await call_next(context)


# Mirror of hosted_envs._strip_nonstring_enums (PR #13032); kept separate because
# this module is vendored into delivered worlds and can't import across packages.
def _strip_nonstring_enums(node: Any) -> Any:
    """Return a copy of a JSON schema with every non-string ``enum`` dropped.

    External Go agent runners (GDM's "Xbox Go" sandbox) unmarshal a tool's
    schema with ``enum`` typed as ``[]string``; a single non-string member (an
    int id, a bool) makes the Go side reject the WHOLE tool list, so the agent
    never starts. Dropping the enum keeps the field's ``type`` (and every other
    key), so the param stays well-typed and the value the model emits is
    unchanged — only the (usually spurious) enumerated hint is removed.
    All-string enums are left untouched.
    """
    if isinstance(node, dict):
        return {
            k: _strip_nonstring_enums(v)
            for k, v in node.items()
            if not (
                k == "enum"
                and isinstance(v, list)
                and not all(isinstance(m, str) for m in v)
            )
        }
    if isinstance(node, list):
        return [_strip_nonstring_enums(v) for v in node]
    return node


class _StripNonStringEnumsMiddleware(Middleware):
    """Strip non-string ``enum`` members from served tool schemas.

    Mirrors FastMCP's own ``DereferenceRefsMiddleware``: rewrites each tool's
    ``parameters`` in ``list_tools`` so downstream agent runners that type
    ``enum`` as ``[]string`` can parse the tool list. Tool-call arguments are
    proxied through untouched, so no value ever changes type.
    """

    @override
    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        stripped: list[Tool] = []
        for tool in tools:
            params = _strip_nonstring_enums(tool.parameters)
            stripped.append(
                tool.model_copy(update={"parameters": params})
                if params != tool.parameters
                else tool
            )
        return stripped


class MCPReadinessError(Exception):
    """Exception raised when MCP servers fail readiness check.

    Attributes:
        failed_servers: Dict mapping server names to readiness details
        message: Human-readable error message
    """

    failed_servers: dict[str, ServerReadinessDetails]
    message: str

    def __init__(
        self,
        failed_servers: dict[str, ServerReadinessDetails],
        message: str | None = None,
    ):
        """Initialize MCP readiness error.

        Args:
            failed_servers: Dict mapping server names to ServerReadinessDetails
            message: Optional custom error message
        """
        self.failed_servers = failed_servers
        server_list = ", ".join(failed_servers.keys())
        self.message = message or f"MCP servers not ready after 5 min: {server_list}"
        super().__init__(self.message)


# Env-gated upstream read-timeout (seconds) for the gateway's proxy client.
# Unset = FastMCP default (~5 min read), which aborts a long-running tool's
# result delivery with "Upstream request timed out" even while the upstream is
# healthy and emitting keepalive progress. When set, every proxied tool call
# may wait this long for an upstream response — size it above the longest
# expected single-tool runtime (e.g. a full antigravity_run) to decouple result
# delivery from that ceiling. (FastMCP 3.x ignores per-server `sse_read_timeout`;
# the live knob is the client's `timeout` → ClientSession `read_timeout_seconds`.)
_READ_TIMEOUT_ENV = "MCP_GATEWAY_SSE_READ_TIMEOUT_SECONDS"


def _proxy_read_timeout_seconds() -> float | None:
    raw = os.getenv(_READ_TIMEOUT_ENV)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _build_proxy(config_dict: dict[str, Any]) -> FastMCP:
    """Build the gateway proxy, honoring the env read-timeout when set.

    Default (env unset) is byte-identical to `FastMCP.as_proxy(config_dict)`.
    When the env timeout is set we build the same proxy explicitly so the
    upstream `ProxyClient` carries a `timeout` (→ `read_timeout_seconds`),
    which is the only knob FastMCP 3.x actually applies to the upstream read.
    """
    middleware = [CoordinatorToolCallMiddleware()]
    timeout = _proxy_read_timeout_seconds()
    if timeout is None:
        return FastMCP.as_proxy(config_dict, name="Gateway", middleware=middleware)

    from fastmcp.server.providers.proxy import FastMCPProxy

    base_client = ProxyClient(config_dict, timeout=timeout)
    return FastMCPProxy(
        client_factory=lambda: base_client.new(),
        name="Gateway",
        middleware=middleware,
    )


# Per-server config fields consumed by the gateway itself; not part of the
# FastMCP MCP-server config schema, so strip them before handing the dict to
# FastMCP's proxy builder.
_GATEWAY_ONLY_KEYS = {
    "serve_mcp_tools",
    "openapi_mcp_filter",
    "exposes_mcp",
    "session_affinity",
}


def _serving_config_dict(config: MCPSchema) -> dict[str, Any] | None:
    """Return the FastMCP-ready config dict for serving servers, or None if none serve.

    serve_mcp_tools=False servers are routable via /rest but excluded from the
    aggregated MCP tool list (transport "rest" only).
    """
    serving = {n: s for n, s in config.mcpServers.items() if s.serve_mcp_tools}
    if not serving:
        return None

    # FastMCP's config parser is sensitive to keys being present with null values
    # (e.g., http servers should not also include {"command": null, ...}).
    # Only emit explicitly-set fields. allowed_tool_names is gateway-specific
    # (not part of the FastMCP MCP server config schema), so strip it before
    # passing to FastMCP's proxy builder.
    config_dict = config.model_dump(exclude_none=True)
    config_dict.pop("allowed_tool_names", None)
    config_dict["mcpServers"] = {
        n: {k: v for k, v in s.items() if k not in _GATEWAY_ONLY_KEYS}
        for n, s in config_dict["mcpServers"].items()
        if n in serving
    }
    return config_dict


def _session_affinity_requested(config: MCPSchema) -> bool:
    """True if any serving server needs a reused backend MCP session (e.g. browser)."""
    return any(
        s.session_affinity for s in config.mcpServers.values() if s.serve_mcp_tools
    )


# --- Stateful (session-affine) gateway proxy ----------------------------------
# A serving server can opt into session affinity (default off). When set, the
# gateway connects ONE StatefulProxyClient for THAT server and reuses its session
# for every tool call, so the browser's page/refs/cookies survive across calls
# instead of resetting to about:blank. The connect must run in a long-lived owner
# task, never an inbound request task, or its streamable-HTTP cancel scope is
# orphaned when the request ends and crashes the next reuse. Affinity is scoped
# per server, never world-wide: a session connected outside a request never
# carries the per-call `Authorization: Bearer <actor_id>` rewrite, so sweeping a
# tenancy-enforcing backend (e.g. the email app) into the affine path breaks it
# with "Missing Authorization: Bearer <user_id> header." on every call.
#
# WARNING — this session-affine path depends on UNDOCUMENTED fastmcp internals. Do
# not upgrade fastmcp without re-validating ALL of the following against the new
# version:
#   1. StatefulProxyClient.__aexit__ is a no-op, so fastmcp's per-call nesting_counter
#      accumulates and never decrements (the reconnect logic below exists solely to
#      recover from the error this causes when a backend session later drops).
#   2. Client._connect raises a RuntimeError containing `_NESTING_COUNTER_ERROR` when a
#      session is (re)started with the counter != 0 — the exact reconnect trigger.
#   3. create_proxy() reuses a *connected* client's session for every call because
#      `type(client) is ProxyClient` is False for this subclass. This is what actually
#      defeats about:blank, and a regression here is SILENT (no error, just a broken
#      browser), so it is the most dangerous of the three to miss.
# fastmcp moves these between minor releases (3.4.2 already split the distribution into
# fastmcp + fastmcp-slim). The env pins 3.2.0 but the specifier is unbounded
# (`fastmcp>=3.2.0`) and the same monorepo already runs 3.4.2 in hosted-envs/uv.lock.
# On any bump, re-run the session-reuse + drop-recovery tests in test_mcp_gateway.py:
# they fail-closed on (1) and (2), but NOT reliably on (3) — verify (3) by hand.
_NESTING_COUNTER_ERROR = "nesting counter should be 0"


class _ReconnectingStatefulProxyClient(StatefulProxyClient[Any]):
    """StatefulProxyClient that survives a backend session drop.

    StatefulProxyClient's __aexit__ is a no-op, so fastmcp's nesting_counter
    accumulates per tool call; if the backend session then drops, the next
    connect raises the nesting-counter error and would fail the run. Catch it,
    force-disconnect (resetting the counter) and reconnect once — the browser is
    already gone for that drop, but the call succeeds on a fresh session.
    """

    @override
    async def __aenter__(self) -> "FastMCPClient[Any]":
        try:
            return await self._connect()
        except RuntimeError as e:
            if _NESTING_COUNTER_ERROR not in str(e):
                raise
            logger.warning("Session-affine backend dropped; reconnecting fresh.")
            # _disconnect resets the counter BEFORE it awaits the dead session task,
            # then that await re-raises a *dirty* drop's stored error. We only need the
            # reset here, so suppress and reconnect — otherwise a dirty drop fails this
            # call and recovers only on the next one.
            with contextlib.suppress(Exception):
                await self._disconnect(force=True)
            return await self._connect()


async def _shutdown_stateful(handle: StatefulProxyHandle | None) -> None:
    """Signal an owner task to disconnect its backend client and wait for it."""
    if handle is None:
        return
    stop, task = handle
    stop.set()
    try:
        await task
    except Exception as e:  # noqa: BLE001
        # A failed disconnect leaks a backend browser; log it for monitoring.
        logger.warning(f"Stateful proxy owner task errored on shutdown: {e}")


async def shutdown_stateful_proxy() -> None:
    """Disconnect the active session-affine backend client (e.g. on app shutdown)."""
    # Take the swap lock so this can't race a concurrent swap mutating the state.
    async with get_mcp_lock():
        handle = get_current_stateful()
        set_current_stateful(None)
        await _shutdown_stateful(handle)


async def _build_stateful_mcp_app_with_proxy(
    config_dict: dict[str, Any],
    affine_names: set[str],
) -> tuple[StarletteWithLifespan, FastMCP, StatefulProxyHandle]:
    """Build a session-affine proxy app: one connected, reused backend session.

    For 2+ serving servers, dispatches to _build_multi_stateful_mcp_app_with_proxy
    (only servers in affine_names get a reconnecting stateful client; the rest stay
    per-call stateless); the body below is the single-server path, where the sole
    server is the affine one by construction.

    Returns (app, proxy, handle); the caller disconnects the handle via
    _shutdown_stateful once the app is unmounted. Raises (leaving no owner task) if
    the connect or app build fails, so the caller can keep the old gateway.
    """
    if len(config_dict["mcpServers"]) >= 2:
        return await _build_multi_stateful_mcp_app_with_proxy(config_dict, affine_names)

    timeout = _proxy_read_timeout_seconds()
    client = _ReconnectingStatefulProxyClient(config_dict, timeout=timeout)
    ready: asyncio.Event = asyncio.Event()
    stop: asyncio.Event = asyncio.Event()
    connect_error: dict[str, BaseException] = {}

    async def _owner() -> None:
        # Connect + disconnect both run in THIS task so the session's cancel scope
        # is never orphaned across request tasks. Catch Exception (not BaseException)
        # so a CancelledError propagates; `ready` is always set so the builder never
        # hangs on `await ready.wait()`.
        try:
            _ = await client.__aenter__()
        except Exception as e:  # noqa: BLE001
            connect_error["error"] = e
            return
        finally:
            ready.set()
        try:
            await stop.wait()
        finally:
            try:
                # Same protected force-disconnect MCPConfigTransport uses internally.
                await client._disconnect(force=True)  # pyright: ignore[reportPrivateUsage]
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Stateful proxy client disconnect error: {e}")

    task = asyncio.create_task(_owner())
    try:
        await ready.wait()
    except BaseException:
        # Swap cancelled/errored before the connect settled: cancel and reap the
        # owner task so it can't park forever holding a live browser session.
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        raise
    if "error" in connect_error:
        await task  # owner already returned; reap it so it is not left pending
        raise RuntimeError(
            f"Failed to connect session-affine gateway proxy: {connect_error['error']!r}"
        )

    logger.info("Gateway proxy built in session-affine (shared-session) mode")
    handle = StatefulProxyHandle(stop, task)

    try:
        # create_proxy() over a *connected* client reuses its session per request.
        proxy = create_proxy(
            client, name="Gateway", middleware=[CoordinatorToolCallMiddleware()]
        )
        mcp_app = proxy.http_app(path="/")
    except BaseException:
        # Connected but failed to build the app: disconnect so the owner task and
        # backend browser don't leak.
        await _shutdown_stateful(handle)
        raise
    return mcp_app, proxy, handle


async def _build_multi_stateful_mcp_app_with_proxy(
    config_dict: dict[str, Any],
    affine_names: set[str],
) -> tuple[StarletteWithLifespan, FastMCP, StatefulProxyHandle]:
    """Build a mixed proxy for 2+ serving servers, session-affine per server.

    ONLY servers in affine_names get a connected, reused backend session; every
    other server is mounted as a per-call stateless proxy. Session affinity must
    stay per-server scoped: a stateful client connects from the owner task with no
    active HTTP request, so fastmcp's connect_session never sees the per-call
    `Authorization: Bearer <actor_id>` rewrite (CoordinatorToolCallMiddleware) and
    tenancy-enforcing backends reject every call with "Missing Authorization".
    Stateless mounts open a fresh backend session inside the request, which is
    what forwards that header.

    Each affine backend gets its OWN single-server reconnecting client (a
    single-server MCPConfig takes fastmcp's direct-transport path with no inner
    client, so the reconnect override is in the path for every backend) and is
    mounted into one composite gateway. Without this, fastmcp's multi-server
    transport interposes a plain per-backend client our reconnect never reaches,
    so a backend drop fails the call. Tool naming matches fastmcp's native
    multi-server composition by construction (mount each backend with
    namespace=<server name>).
    """
    timeout = _proxy_read_timeout_seconds()
    clients: dict[str, _ReconnectingStatefulProxyClient] = {
        name: _ReconnectingStatefulProxyClient(
            {"mcpServers": {name: server_cfg}}, timeout=timeout
        )
        for name, server_cfg in config_dict["mcpServers"].items()
        if name in affine_names
    }
    ready: asyncio.Event = asyncio.Event()
    stop: asyncio.Event = asyncio.Event()
    connect_error: dict[str, BaseException] = {}

    async def _owner() -> None:
        # Connect AND disconnect every backend in THIS one task so no session's
        # cancel scope is orphaned across request tasks. The outer `finally` drains
        # whatever actually connected on EVERY exit — normal stop, partial-connect
        # failure, AND CancelledError (a /apps request cancelled mid-connect) — so an
        # already-connected backend session is never silently orphaned.
        connected: list[_ReconnectingStatefulProxyClient] = []
        try:
            try:
                for client in clients.values():
                    _ = await client.__aenter__()
                    connected.append(client)
            except Exception as e:  # noqa: BLE001
                connect_error["error"] = e
            finally:
                ready.set()
            if not connect_error:
                await stop.wait()
        finally:
            for client in connected:
                try:
                    await client._disconnect(force=True)  # pyright: ignore[reportPrivateUsage]
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Stateful proxy client disconnect error: {e}")

    task = asyncio.create_task(_owner())
    try:
        await ready.wait()
    except BaseException:
        # Swap cancelled/errored before the connects settled: cancel and reap the
        # owner task so it can't park forever holding live backend sessions.
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        raise
    if "error" in connect_error:
        await task  # owner already returned; reap it so it is not left pending
        raise RuntimeError(
            f"Failed to connect session-affine gateway proxy: {connect_error['error']!r}"
        )

    logger.info("Gateway proxy built in session-affine (multi-server) mode")
    handle = StatefulProxyHandle(stop, task)

    try:
        composite = FastMCP(
            name="Gateway", middleware=[CoordinatorToolCallMiddleware()]
        )
        for name, server_cfg in config_dict["mcpServers"].items():
            if name in clients:
                # create_proxy() over a *connected* client reuses its session per
                # request; mount with namespace=<name> to match fastmcp's native
                # multi-server {name}_{tool} naming.
                proxy = create_proxy(clients[name], name=f"Proxy-{name}")
            else:
                # Non-affine backend: a *disconnected* ProxyClient makes
                # create_proxy() open a fresh session per request, so the per-call
                # Authorization rewrite is forwarded (pre-affinity behavior).
                proxy = create_proxy(
                    ProxyClient({"mcpServers": {name: server_cfg}}, timeout=timeout),
                    name=f"Proxy-{name}",
                )
            composite.mount(proxy, namespace=name)
        mcp_app = composite.http_app(path="/")
    except BaseException:
        # Connected but failed to build the app: disconnect so the owner task and
        # backend sessions don't leak.
        await _shutdown_stateful(handle)
        raise
    return mcp_app, composite, handle


def _build_mcp_app_with_proxy(
    config_dict: dict[str, Any] | None,
) -> tuple[StarletteWithLifespan, FastMCP]:
    """Build a (stateless) FastMCP proxy ASGI app from a serving config dict.

    config_dict is the output of _serving_config_dict(); None means no serving
    servers, so a bare gateway (no aggregated tools) is returned. Returns
    (ASGI app, FastMCP gateway).
    """
    if config_dict is None:
        mcp_server = FastMCP(
            name="Gateway",
            middleware=[CoordinatorToolCallMiddleware()],
        )
        return mcp_server.http_app(path="/"), mcp_server

    mcp_proxy = _build_proxy(config_dict)
    # Root at "/" so final URLs are under /mcp.
    return mcp_proxy.http_app(path="/"), mcp_proxy


async def warm_and_check_gateway(
    mcp_proxy: FastMCP,
    expected_servers: list[str],
    max_wait_seconds: float = 300.0,
    retry_interval: float = 1.0,
) -> int:
    """Warm up gateway connections and verify all servers are ready.

    Connects to the gateway and calls list_tools(). This forces the proxy to
    connect to all backend servers (warming the connections). Then verifies
    that every expected server contributed at least one tool.

    Args:
        mcp_proxy: The FastMCP proxy instance to warm up
        expected_servers: List of server names that must provide tools
        max_wait_seconds: Maximum time to wait for all servers (default 5 min)
        retry_interval: Time between retry attempts (default 1s)

    Returns:
        Total number of tools loaded

    Raises:
        MCPReadinessError: If any server doesn't provide tools within timeout
    """
    start_time = time.perf_counter()
    deadline = start_time + max_wait_seconds
    attempts = 0
    last_error: str = ""
    missing_servers: set[str] = set(expected_servers)
    servers_with_tools: dict[str, int] = {}

    while True:
        attempts += 1
        remaining = deadline - time.perf_counter()

        if remaining <= 0:
            last_error = "Timeout"
            break

        try:
            async with asyncio.timeout(remaining):
                async with FastMCPClient(mcp_proxy) as client:
                    tools = await client.list_tools()
                    tool_names = [t.name for t in tools]

                servers_with_tools = tool_counts_by_server(tool_names, expected_servers)

                missing_servers = set(expected_servers) - set(servers_with_tools.keys())

                if not missing_servers:
                    elapsed = time.perf_counter() - start_time
                    total_tools = len(tools)
                    logger.info(
                        f"Gateway ready after {elapsed:.1f}s: {total_tools} tools from {len(expected_servers)} servers"
                    )
                    for server, count in sorted(servers_with_tools.items()):
                        logger.info(f"  - {server}: {count} tools")
                    return total_tools

                elapsed = time.perf_counter() - start_time
                ready_list = ", ".join(
                    f"{s} ({c} tools)" for s, c in servers_with_tools.items()
                )
                missing_list = ", ".join(missing_servers)
                if ready_list:
                    logger.debug(
                        f"Attempt {attempts} ({elapsed:.1f}s): Ready: [{ready_list}], Waiting: [{missing_list}]"
                    )
                else:
                    logger.debug(
                        f"Attempt {attempts} ({elapsed:.1f}s): Waiting for all servers"
                    )

        except TimeoutError:
            last_error = "Timeout"
            break

        except Exception as e:
            elapsed = time.perf_counter() - start_time
            last_error = str(e)
            logger.debug(
                f"Attempt {attempts} ({elapsed:.1f}s): Gateway connection failed: {e}"
            )

        await asyncio.sleep(retry_interval)

    # Failure path - report results
    elapsed = time.perf_counter() - start_time
    failed_servers: dict[str, ServerReadinessDetails] = {}

    for server in missing_servers:
        error_msg = f"No tools found after {elapsed:.1f}s"
        if last_error:
            error_msg += f" (last error: {last_error})"
        failed_servers[server] = ServerReadinessDetails(
            error=error_msg,
            attempts=attempts,
        )
        logger.warning(
            f"Server '{server}' FAILED after {attempts} attempt(s) ({elapsed:.1f}s): {error_msg}"
        )

    for server, count in servers_with_tools.items():
        logger.info(
            f"Server '{server}' ready after {attempts} attempt(s) ({elapsed:.1f}s): {count} tools"
        )

    failed_count = len(failed_servers)
    ready_count = len(servers_with_tools)
    logger.error(
        f"MCP readiness check failed: {failed_count} server(s) not ready ({ready_count} server(s) ready)"
    )
    raise MCPReadinessError(failed_servers)


async def warm_and_check_servers(
    server_urls: dict[str, str],
    max_wait_seconds: float = 300.0,
    retry_interval: float = 1.0,
) -> None:
    """Wait for registered backends that are excluded from the aggregated proxy.

    rest-only servers (serve_mcp_tools=False) still expose an /mcp endpoint; we
    probe each directly so readiness covers them without adding their tools to
    the aggregated /mcp.
    """
    if not server_urls:
        return

    start_time = time.perf_counter()
    deadline = start_time + max_wait_seconds
    attempts = 0
    pending: dict[str, str] = dict(server_urls)
    last_error: dict[str, str] = {}

    while pending:
        attempts += 1
        if time.perf_counter() >= deadline:
            break
        for name, url in list(pending.items()):
            try:
                async with FastMCPClient(url) as client:
                    _ = await client.list_tools()
                del pending[name]
            except Exception as e:  # noqa: BLE001
                last_error[name] = str(e)
        if pending:
            await asyncio.sleep(retry_interval)

    if pending:
        elapsed = time.perf_counter() - start_time
        failed_servers = {
            name: ServerReadinessDetails(
                error=f"Not ready after {elapsed:.1f}s (last error: {last_error.get(name, 'unknown')})",
                attempts=attempts,
            )
            for name in pending
        }
        raise MCPReadinessError(failed_servers)

    elapsed = time.perf_counter() - start_time
    logger.info(
        f"REST-only backends ready after {elapsed:.1f}s: {', '.join(server_urls)}"
    )


async def swap_mcp_app(config: MCPSchema, app: FastAPI) -> FastMCP:
    """Hot-swap the mounted MCP app with a new configuration.

    This function:
    1. Builds a new MCP app from config
    2. Starts its lifespan
    3. Atomically replaces the Mount.app reference
    4. Shuts down the old app's lifespan
    5. Warms up gateway connections and verifies configured servers are ready

    Args:
        config: New MCP configuration schema (MCPSchema instance)
        app: The FastAPI application instance

    Raises:
        ValueError: If config is invalid
        RuntimeError: If swap fails
        MCPReadinessError: If any server fails readiness check
    """
    async with get_mcp_lock():  # Prevent concurrent swaps
        # Build the new app first. A session-affine server (e.g. browser) needs ONE
        # reused backend session; everything else uses the stateless per-call proxy.
        # The active session-affine client keeps serving the outgoing app and is
        # disconnected only after the swap succeeds, so a failed build leaves the
        # old gateway intact.
        new_stateful: StatefulProxyHandle | None = None
        config_dict = _serving_config_dict(config)
        if config_dict is not None and _session_affinity_requested(config):
            # Affinity is scoped per server: only these servers get a reused
            # backend session; the rest stay per-call stateless.
            affine_names = {
                n
                for n, s in config.mcpServers.items()
                if s.serve_mcp_tools and s.session_affinity
            }
            (
                new_app,
                mcp_proxy,
                new_stateful,
            ) = await _build_stateful_mcp_app_with_proxy(config_dict, affine_names)
        else:
            new_app, mcp_proxy = _build_mcp_app_with_proxy(config_dict)

        new_lm = LifespanManager(new_app)
        published = False
        lm_entered = False

        try:
            _ = await new_lm.__aenter__()
            lm_entered = True

            current_mount = get_mcp_mount()
            if current_mount is None:
                app.mount("/mcp", new_app)

                mount = next(
                    (
                        r
                        for r in app.router.routes
                        if isinstance(r, Mount) and r.path == "/mcp"
                    ),
                    None,
                )
                if mount is None:
                    msg = (
                        "Failed to find mounted MCP gateway after mounting. "
                        "This should not happen and indicates a bug."
                    )
                    raise RuntimeError(msg)
                set_mcp_mount(mount)
            else:
                current_mount.app = new_app

            old_lm = get_mcp_lifespan_manager()
            if old_lm is not None:
                # The new app is already mounted and live; the old app is being
                # discarded, so a teardown failure here must NOT abort the swap and
                # roll back onto the now-live gateway. Swallow and log.
                try:
                    _ = await old_lm.__aexit__(None, None, None)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"Old gateway lifespan teardown errored (ignored): {e}"
                    )

            set_mcp_lifespan_manager(new_lm)

            # Old app is fully torn down; publish the new session-affine client as
            # active, then disconnect the previous one (no-op when the previous
            # gateway was stateless).
            prev_stateful = get_current_stateful()
            set_current_stateful(new_stateful)
            published = True
            await _shutdown_stateful(prev_stateful)

            server_count = len(config.mcpServers)
            logger.info(
                f"Successfully swapped MCP gateway with {server_count} server(s)"
            )

            # Only serve_mcp_tools servers contribute to the aggregated /mcp list.
            server_names = [
                n for n, s in config.mcpServers.items() if s.serve_mcp_tools
            ]
            # rest-only servers are routable via /rest but excluded from /mcp;
            # still wait on them so /apps doesn't report ready before they serve.
            rest_only_urls = {
                n: s.url
                for n, s in config.mcpServers.items()
                if not s.serve_mcp_tools and s.url
            }
            if not server_names and not rest_only_urls:
                logger.debug("No MCP tool servers configured, skipping readiness check")
                return mcp_proxy

            logger.debug("Waiting 1.0 seconds before starting readiness checks...")
            await asyncio.sleep(1.0)
            if server_names:
                _ = await warm_and_check_gateway(mcp_proxy, server_names)
            await warm_and_check_servers(rest_only_urls)

            # Install the allowlist filter only after readiness, otherwise
            # warm_and_check_gateway would see only the allowed subset and
            # report missing servers for any whose tools are all excluded.
            # Truthy check: an empty list is treated the same as None (no
            # filter). Otherwise an accidentally-empty allowlist would silently
            # block every tool.
            if config.allowed_tool_names:
                mcp_proxy.add_middleware(
                    _AllowedToolsMiddleware(config.allowed_tool_names)
                )
                logger.info(
                    f"Tool allowlist active: {len(config.allowed_tool_names)} tools allowed"
                )
            # Always strip non-string enum hints: external Go agent runners
            # (GDM's "Xbox Go" sandbox) type JSON-Schema `enum` as []string and
            # reject the whole tool list on one non-string member. Dropping the
            # enum preserves each field's type, so emitted values are unchanged.
            mcp_proxy.add_middleware(_StripNonStringEnumsMiddleware())
            return mcp_proxy

        except MCPReadinessError:
            raise
        except Exception as e:
            # Pre-publish failure (including new_lm.__aenter__): tear down the
            # just-built session-affine client AND the new app's lifespan so
            # neither leaks; the previous gateway stays active. Post-publish the
            # new app IS the live gateway — leave it mounted (do NOT tear it down).
            if not published:
                await _shutdown_stateful(new_stateful)
                if lm_entered:
                    _ = await new_lm.__aexit__(None, None, None)
            logger.error(f"Failed to swap MCP gateway: {e}")
            raise RuntimeError(f"Failed to swap MCP gateway: {e}") from e
