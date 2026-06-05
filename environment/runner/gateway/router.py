"""FastAPI router for MCP gateway endpoints.

This module defines the FastAPI router that handles the /apps endpoint
for configuring MCP servers.
"""

import time

from fastapi import APIRouter, FastAPI, HTTPException, Request
from loguru import logger

from runner.coordinator.config.models import CoordinatorConfig
from runner.coordinator.runtime import get_coordinator

from .gateway import MCPReadinessError, swap_mcp_app
from .models import AppConfigRequest, AppConfigResult
from .state import get_mcp_config, set_mcp_config

router = APIRouter()


@router.post("/apps", response_model=AppConfigResult)
async def set_apps(request: AppConfigRequest, http_request: Request) -> AppConfigResult:
    """Set/update MCP servers configuration.

    This endpoint hot-swaps the MCP gateway with new configuration.
    Can be called multiple times to update the configuration.

    If the incoming configuration is byte-for-byte identical to the
    currently-mounted one, the swap is skipped entirely.

    Args:
        request: AppConfigRequest containing mcpServers configuration
        http_request: FastAPI Request object to access the app instance

    Returns:
        AppConfigResult with list of server names

    Raises:
        HTTPException: If configuration is invalid or swap fails
    """
    app: FastAPI = http_request.app
    server_names = list(request.mcpServers.keys())

    current_config = get_mcp_config()
    if current_config is not None and current_config == request:
        logger.info(
            f"MCP gateway config unchanged ({len(server_names)} server(s)), skipping swap"
        )
        return AppConfigResult(servers=server_names, duration_ms=0.0)

    logger.debug(f"Apps configuration request received: {len(server_names)} server(s)")
    for server_name in server_names:
        server_config = request.mcpServers[server_name]
        transport = server_config.transport
        logger.debug(f"  Server '{server_name}': transport={transport}")

    # Invalidate the cache *before* attempting the swap. swap_mcp_app
    # mutates the FastAPI mount (and rotates the lifespan manager)
    # before running the readiness check, so any post-mutation failure
    # — MCPReadinessError, or any exception from inside swap_mcp_app's
    # generic except block — leaves the gateway in a partial state
    # where the cached config no longer matches what is actually
    # mounted. Without this invalidation, a subsequent /apps request
    # matching the *prior* cached config would short-circuit and
    # report success even though the mounted gateway is in a broken
    # state. Worst case if swap_mcp_app raises *before* the mount
    # mutation (e.g. a ValueError from _build_mcp_app_with_proxy on
    # invalid config), this just costs one redundant full swap on the
    # next request — harmless.
    set_mcp_config(None)

    start = time.perf_counter()
    try:
        mcp_proxy = await swap_mcp_app(request, app)
        await get_coordinator().start(
            mcp_proxy=mcp_proxy,
            config=request.coordinator_config or CoordinatorConfig(enabled=False),
        )
        duration_ms = (time.perf_counter() - start) * 1000

        set_mcp_config(request)

        logger.info(
            f"Configured MCP gateway with {len(server_names)} server(s): {', '.join(server_names)}"
        )

        return AppConfigResult(servers=server_names, duration_ms=duration_ms)
    except MCPReadinessError as e:
        logger.error(f"MCP servers not ready: {e.message}")
        error_detail = {
            "error": e.message,
            "failed_servers": list(e.failed_servers.keys()),
            "details": {
                name: details.model_dump() for name, details in e.failed_servers.items()
            },
        }
        raise HTTPException(status_code=503, detail=error_detail) from e
    except ValueError as e:
        logger.error(f"Invalid MCP configuration: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        logger.error(f"Failed to swap MCP gateway: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Unexpected error setting MCP servers: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
