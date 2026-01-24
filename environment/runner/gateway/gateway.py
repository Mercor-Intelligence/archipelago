"""Core MCP gateway logic for building and hot-swapping MCP apps.

This module handles creating FastMCP proxy ASGI apps and hot-swapping them
in the FastAPI application without restarting the server.
"""

import asyncio
import time

from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from fastmcp import Client as FastMCPClient
from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan
from loguru import logger
from starlette.routing import Mount

from .models import (
    MCPSchema,
    ServerReadinessDetails,
)
from .state import (
    get_mcp_lifespan_manager,
    get_mcp_lock,
    get_mcp_mount,
    set_mcp_lifespan_manager,
    set_mcp_mount,
)


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


def _build_mcp_app_with_proxy(
    config: MCPSchema,
) -> tuple[StarletteWithLifespan, FastMCP | None]:
    """Build a FastMCP proxy ASGI app from MCP configuration.

    Internal function that returns both the HTTP app and the proxy instance.

    Args:
        config: MCP configuration schema with "mcpServers" key

    Returns:
        Tuple of (ASGI app, FastMCP proxy or None if no servers)
    """
    if not config.mcpServers:
        mcp_server = FastMCP(name="Gateway")
        mcp_app = mcp_server.http_app(path="/")
        return mcp_app, None

    # FastMCP's config parser is sensitive to keys being present with null values
    # (e.g., http servers should not also include {"command": null, ...}).
    # Only emit explicitly-set fields.
    config_dict = config.model_dump(exclude_none=True)
    mcp_proxy = FastMCP.as_proxy(config_dict, name="Gateway")

    # Create HTTP ASGI app, root at "/" so final URLs are under /mcp
    mcp_app = mcp_proxy.http_app(path="/")

    return mcp_app, mcp_proxy


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

    # FastMCP only prefixes tools when there are multiple servers
    single_server = len(expected_servers) == 1

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

                servers_with_tools = {}
                if single_server:
                    server = expected_servers[0]
                    if tool_names:
                        servers_with_tools[server] = len(tool_names)
                else:
                    # Sort by name length (longest first) to handle prefix collisions
                    # e.g., "api_v2" before "api" so "api_v2_tool" isn't claimed by "api"
                    sorted_servers = sorted(expected_servers, key=len, reverse=True)
                    claimed_tools: set[str] = set()

                    for server in sorted_servers:
                        prefix = f"{server}_"
                        matching = [
                            name
                            for name in tool_names
                            if name.startswith(prefix) and name not in claimed_tools
                        ]
                        if matching:
                            servers_with_tools[server] = len(matching)
                            claimed_tools.update(matching)

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


async def swap_mcp_app(config: MCPSchema, app: FastAPI) -> None:
    """Hot-swap the mounted MCP app with a new configuration.

    This function:
    1. Builds a new MCP app from config
    2. Starts its lifespan
    3. Atomically replaces the Mount.app reference
    4. Shuts down the old app's lifespan
    5. Warms up gateway connections and verifies all servers are ready

    Args:
        config: New MCP configuration schema (MCPSchema instance)
        app: The FastAPI application instance

    Raises:
        ValueError: If config is invalid
        RuntimeError: If swap fails
        MCPReadinessError: If any server fails readiness check
    """
    async with get_mcp_lock():  # Prevent concurrent swaps
        new_app, mcp_proxy = _build_mcp_app_with_proxy(config)

        new_lm = LifespanManager(new_app)
        _ = await new_lm.__aenter__()

        try:
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
                _ = await old_lm.__aexit__(None, None, None)

            set_mcp_lifespan_manager(new_lm)

            server_count = len(config.mcpServers)
            logger.info(
                f"Successfully swapped MCP gateway with {server_count} server(s)"
            )

            if not config.mcpServers or mcp_proxy is None:
                logger.debug("No MCP servers configured, skipping readiness check")
                return

            logger.debug("Waiting 1.0 seconds before starting readiness checks...")
            await asyncio.sleep(1.0)

            server_names = list(config.mcpServers.keys())
            _ = await warm_and_check_gateway(mcp_proxy, server_names)

        except MCPReadinessError:
            raise
        except Exception as e:
            _ = await new_lm.__aexit__(None, None, None)
            logger.error(f"Failed to swap MCP gateway: {e}")
            raise RuntimeError(f"Failed to swap MCP gateway: {e}") from e
