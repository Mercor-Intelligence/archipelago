import inspect
from collections.abc import Callable
from functools import wraps

# Module-level session storage for stdio clients
# Key: session_id (or "default" for single session), Value: user dict
_session_auth_store: dict[str, dict] = {}


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
        if inspect.iscoroutinefunction(func):

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


def require_any_scopes(*scopes: str) -> Callable:
    """
    Decorator to require ANY of the specified scopes (OR-logic).

    Use this for tools where the user needs at least ONE of the listed scopes,
    not all of them.

    The decorator:
    1. Stores scopes in _required_any_scopes for AuthGuard to enforce OR-logic
    2. AuthGuard checks if user has AT LEAST ONE of these scopes

    Usage:
        @mcp.tool()
        @require_any_scopes("requisitions:read", "requisitions:read:own")
        async def get_requisition(...):
            # User passes if they have EITHER scope
            pass
    """

    def decorator(func: Callable) -> Callable:
        # Store scopes for OR-logic enforcement
        if not hasattr(func, "_required_any_scopes"):
            func._required_any_scopes = set()
        func._required_any_scopes.update(scopes)

        # Create appropriate wrapper based on whether func is async or sync
        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                return await func(*args, **kwargs)

            async_wrapper._required_any_scopes = func._required_any_scopes
            return async_wrapper
        else:

            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                return func(*args, **kwargs)

            sync_wrapper._required_any_scopes = func._required_any_scopes
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
        if inspect.iscoroutinefunction(func):

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


def session_login(func: Callable) -> Callable:
    """
    Decorator for login tools that stores auth state in session.

    This decorator:
    1. Marks the tool as public (no authentication required)
    2. Automatically stores the authenticated user in session state
       after a successful login

    The wrapped function should return a dict with "user" and "token"
    keys on successful login, or an "error" key on failure.

    The decorated function MUST have a `ctx: Context = None` parameter
    for FastMCP to inject the context.

    Usage:
        from fastmcp import Context

        @mcp.tool()
        @session_login
        async def login(username: str, password: str, ctx: Context = None) -> dict:
            # Validate credentials and return user/token
            return {"user": {...}, "token": "..."}
    """
    from loguru import logger

    # Mark as public
    func._public_tool = True

    # Create appropriate wrapper based on whether func is async or sync
    if inspect.iscoroutinefunction(func):

        @wraps(func)
        async def async_wrapper(**kwargs):
            # Extract ctx for session storage
            ctx = kwargs.get("ctx")

            # Call the underlying login function
            result = await func(**kwargs)

            # If login succeeded, store in module-level session store
            if isinstance(result, dict) and "user" in result and "token" in result:
                username = result["user"].get("username", "unknown")
                # Use getattr to handle ctx objects that may not have session_id
                session_id = getattr(ctx, "session_id", None) or "default"
                _session_auth_store[session_id] = result["user"]
                logger.debug(f"[mcp-auth] Stored session for {username} (key={session_id})")

            return result

        # Preserve the original function's signature for FastMCP
        async_wrapper.__signature__ = inspect.signature(func)

        # Remove __wrapped__ to prevent FastMCP from unwrapping
        if hasattr(async_wrapper, "__wrapped__"):
            delattr(async_wrapper, "__wrapped__")

        async_wrapper._public_tool = True
        return async_wrapper
    else:

        @wraps(func)
        def sync_wrapper(**kwargs):
            # Extract ctx for session storage
            ctx = kwargs.get("ctx")

            # Call the underlying login function
            result = func(**kwargs)

            # If login succeeded, store in module-level session store
            if isinstance(result, dict) and "user" in result and "token" in result:
                username = result["user"].get("username", "unknown")
                # Use getattr to handle ctx objects that may not have session_id
                session_id = getattr(ctx, "session_id", None) or "default"
                _session_auth_store[session_id] = result["user"]
                logger.debug(f"[mcp-auth] Stored session for {username} (key={session_id})")

            return result

        # Preserve the original function's signature for FastMCP
        sync_wrapper.__signature__ = inspect.signature(func)

        # Remove __wrapped__ to prevent FastMCP from unwrapping
        if hasattr(sync_wrapper, "__wrapped__"):
            delattr(sync_wrapper, "__wrapped__")

        sync_wrapper._public_tool = True
        return sync_wrapper
