"""MCP gateway module for hot-swapping MCP server configurations.

This module provides endpoints and logic for dynamically configuring
and hot-swapping MCP servers without restarting the FastAPI application.
"""

from .gateway import MCPReadinessError, swap_mcp_app
from .models import AppConfigRequest, AppConfigResult, MCPSchema, MCPServerConfig
from .router import router

__all__ = [
    "AppConfigRequest",
    "AppConfigResult",
    "MCPReadinessError",
    "MCPServerConfig",
    "MCPSchema",
    "router",
    "swap_mcp_app",
]
