"""Global state for MCP gateway hot-swapping.

This module manages the global state needed for hot-swapping the MCP gateway,
including the mount reference, lifespan manager, and concurrency lock.
"""

import asyncio

from asgi_lifespan import LifespanManager
from starlette.routing import Mount

# Global state for MCP mount and lifespan manager
_mcp_mount: Mount | None = None
_mcp_lifespan_manager: LifespanManager | None = None
_mcp_lock: asyncio.Lock = asyncio.Lock()


def get_mcp_mount() -> Mount | None:
    """Get the current MCP mount reference."""
    return _mcp_mount


def set_mcp_mount(mount: Mount | None) -> None:
    """Set the MCP mount reference."""
    global _mcp_mount
    _mcp_mount = mount


def get_mcp_lifespan_manager() -> LifespanManager | None:
    """Get the current MCP lifespan manager."""
    return _mcp_lifespan_manager


def set_mcp_lifespan_manager(manager: LifespanManager | None) -> None:
    """Set the MCP lifespan manager."""
    global _mcp_lifespan_manager
    _mcp_lifespan_manager = manager


def get_mcp_lock() -> asyncio.Lock:
    """Get the MCP swap lock."""
    return _mcp_lock
