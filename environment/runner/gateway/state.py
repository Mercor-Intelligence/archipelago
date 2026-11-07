"""Global state for MCP gateway hot-swapping.

This module manages the global state needed for hot-swapping the MCP gateway,
including the mount reference, lifespan manager, and concurrency lock.
"""

import asyncio
from typing import NamedTuple

from asgi_lifespan import LifespanManager
from starlette.routing import Mount

from .models import AppConfigRequest


class StatefulProxyHandle(NamedTuple):
    """Handle for the active session-affine backend client's owner task.

    Setting ``stop`` and awaiting ``owner_task`` disconnects the client in the
    same task that connected it.
    """

    stop: asyncio.Event
    owner_task: "asyncio.Task[None]"


# Global state for MCP mount and lifespan manager
_mcp_mount: Mount | None = None
_mcp_lifespan_manager: LifespanManager | None = None
_mcp_lock: asyncio.Lock = asyncio.Lock()
_mcp_config: AppConfigRequest | None = None
# Active session-affine proxy handle; None when the gateway is stateless.
_current_stateful: StatefulProxyHandle | None = None


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


def get_current_stateful() -> StatefulProxyHandle | None:
    """Get the active session-affine proxy handle (None in stateless mode)."""
    return _current_stateful


def set_current_stateful(handle: StatefulProxyHandle | None) -> None:
    """Set the active session-affine proxy handle."""
    global _current_stateful
    _current_stateful = handle


def get_mcp_config() -> AppConfigRequest | None:
    """Get the config most recently applied to the mounted MCP gateway.

    Used by the /apps endpoint to short-circuit when an incoming request would
    swap the gateway to a byte-for-byte identical configuration.
    """
    return _mcp_config


def set_mcp_config(config: AppConfigRequest | None) -> None:
    """Set the config most recently applied to the mounted MCP gateway."""
    global _mcp_config
    _mcp_config = config
