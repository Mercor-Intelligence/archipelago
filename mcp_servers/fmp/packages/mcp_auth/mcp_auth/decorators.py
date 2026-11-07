import asyncio
from collections.abc import Callable
from functools import wraps


def require_scopes(*scopes: str) -> Callable:
    """
    Decorator to require specific scopes for a tool.

    Usage:
        @mcp.tool()
        @require_scopes("bloomberg:refdata:read")
        async def reference_data_request(...):
            pass
    """

    def decorator(func: Callable) -> Callable:
        # Store scopes as function metadata
        if not hasattr(func, "_required_scopes"):
            func._required_scopes = set()
        func._required_scopes.update(scopes)

        # Create appropriate wrapper based on whether func is async or sync
        if asyncio.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                return await func(*args, **kwargs)

            async_wrapper._required_scopes = func._required_scopes
            return async_wrapper
        else:

            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                return func(*args, **kwargs)

            sync_wrapper._required_scopes = func._required_scopes
            return sync_wrapper

    return decorator


def require_roles(*roles: str) -> Callable:
    """
    Decorator to require specific roles for a tool.

    Usage:
        @mcp.tool()
        @require_roles("admin")
        async def admin_action(...):
            pass
    """

    def decorator(func: Callable) -> Callable:
        if not hasattr(func, "_required_roles"):
            func._required_roles = set()
        func._required_roles.update(roles)

        # Create appropriate wrapper based on whether func is async or sync
        if asyncio.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                return await func(*args, **kwargs)

            async_wrapper._required_roles = func._required_roles
            return async_wrapper
        else:

            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                return func(*args, **kwargs)

            sync_wrapper._required_roles = func._required_roles
            return sync_wrapper

    return decorator


def public_tool(func: Callable) -> Callable:
    """
    Mark a tool as public (no authentication required).

    Usage:
        @mcp.tool()
        @public_tool
        async def get_public_data(...):
            pass
    """
    func._public_tool = True
    return func
