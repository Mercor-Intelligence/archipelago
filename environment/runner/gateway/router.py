"""FastAPI router for MCP gateway endpoints.

This module defines the FastAPI router that handles the /apps endpoint
for configuring MCP servers.
"""

import time

from fastapi import APIRouter, FastAPI, HTTPException, Request
from loguru import logger

from .gateway import MCPReadinessError, swap_mcp_app
from .models import AppConfigRequest, AppConfigResult

router = APIRouter()


@router.post("/apps", response_model=AppConfigResult)
async def set_apps(request: AppConfigRequest, http_request: Request) -> AppConfigResult:
    """Set/update MCP servers configuration.

    This endpoint hot-swaps the MCP gateway with new configuration.
    Can be called multiple times to update the configuration.

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

    logger.debug(f"Apps configuration request received: {len(server_names)} server(s)")
    for server_name in server_names:
        server_config = request.mcpServers[server_name]
        transport = server_config.transport
        logger.debug(f"  Server '{server_name}': transport={transport}")

    start = time.perf_counter()
    try:
        await swap_mcp_app(request, app)
        duration_ms = (time.perf_counter() - start) * 1000

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
