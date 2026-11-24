import pprint
from collections.abc import Callable
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from loguru import logger

from ..context import current_user, get_http_headers
from ..decorators import _session_auth_store


def _extract_bearer_token(headers: Any) -> str | None:
    """Extract Bearer token from Authorization header."""
    if not hasattr(headers, "get"):
        return None

    auth = headers.get("Authorization", "") or headers.get("authorization", "")
    if isinstance(auth, str) and auth.startswith("Bearer "):
        return auth.split(" ", 1)[1].strip()

    return None


class AuthGuard(Middleware):
    """
    Enhanced authentication and authorization middleware.

    Features:
    - Auto-discovers permissions from @require_scopes and @require_roles decorators
    - Robust tool discovery across different FastMCP versions
    - Clear 401 (authentication) vs 403 (authorization) errors
    """

    # Discovery configuration - attributes to search for tools
    _TOOL_CONTAINER_ATTRS = (
        "_tools",
        "tools",
        "_tool_manager",
        "tool_manager",
        "_tool_registry",
        "tool_registry",
        "_resource_manager",
        "_mcp_server",
        "_tool_store",
    )

    _TOOL_MAP_ATTRS = (
        "_tools",
        "tools",
        "registered",
        "registered_tools",
        "registry",
        "_registry",
    )

    _TOOL_MAP_METHODS = ("get_tools", "list_tools", "iter_tools", "all_tools")

    _CALLABLE_ATTRS = ("fn", "function", "callable", "handler", "callback")

    def __init__(
        self,
        auth_service,
        mcp_instance: Any = None,
        public_tools: list[str] = None,
        default_deny: bool = True,
    ):
        """
        Initialize AuthGuard with auto-discovery.

        Args:
            auth_service: AuthService instance
            mcp_instance: FastMCP instance (for auto-discovering permissions)
            public_tools: Additional public tools (beyond @public_tool decorated)
            default_deny: If True, deny tools without explicit permissions
        """
        self.auth_service = auth_service
        self.public_tools: set[str] = set(public_tools or [])
        self.public_tools.add("tools/list")
        self.default_deny = default_deny

        # Discovered permissions: tool_name -> {"scopes": set, "roles": set}
        self.tool_permissions: dict[str, dict[str, set[str]]] = {}

        # Ensure RestBridgeMiddleware is registered (for REST bridge support)
        # This must happen before we're added to the middleware chain
        if mcp_instance:
            try:
                from mcp_middleware import RestBridgeMiddleware

                RestBridgeMiddleware.ensure_registered(mcp_instance)
            except ImportError:
                logger.warning(
                    "[mcp-auth] mcp_middleware not available, REST bridge support disabled"
                )

        # Discover permissions from decorated tools
        if mcp_instance:
            self._discover_permissions(mcp_instance)

        # Log configuration
        logger.info("[mcp-auth] AuthGuard initialized")
        logger.info(f"[mcp-auth] default_deny={default_deny}")
        self._log_summary()

    def _discover_permissions(self, mcp_instance: Any) -> None:
        """Discover permissions from decorated tools."""
        logger.info(f"[mcp-auth] Discovering tools from {type(mcp_instance).__name__}")

        # Debug: log MCP instance state
        if logger._core.min_level <= 10:  # DEBUG level
            logger.debug(
                f"[mcp-auth] MCP instance state:\n"
                f"{pprint.pformat(mcp_instance.__dict__, indent=2, width=120)}"
            )

        # Find tools mapping
        tools_map, source_name = self._find_tools_map(mcp_instance)

        if not tools_map:
            return

        logger.info(f"[mcp-auth] Using tools source: {source_name}")

        # Process each tool
        tools_items = tools_map.items() if hasattr(tools_map, "items") else tools_map
        for tool_name, tool_info in tools_items:
            self._process_tool(tool_name, tool_info)

        # Log summary
        self._log_discovery_complete()

    def _find_tools_map(self, mcp_instance: Any) -> tuple[dict | None, str | None]:
        """
        Find the tools mapping in the MCP instance.

        Returns:
            (tools_map, source_name) or (None, None)
        """
        candidates = self._get_tool_candidates(mcp_instance)

        if not candidates:
            logger.warning(
                "[mcp-auth] Cannot discover tools - no candidate attributes found on MCP instance"
            )
            return None, None

        # Try to find tools in each candidate
        for name, obj in candidates:
            logger.debug(f"[mcp-auth] Inspecting: {name} ({type(obj).__name__})")

            # Try dict-like attributes
            tools_map, source = self._try_dict_attributes(obj, name)
            if tools_map:
                return tools_map, source

            # Try callable methods
            tools_map, source = self._try_callable_methods(obj, name)
            if tools_map:
                return tools_map, source

        logger.warning("[mcp-auth] Cannot discover tools - no tools mapping found in candidates")
        return None, None

    def _get_tool_candidates(self, mcp_instance: Any) -> list[tuple[str, Any]]:
        """Get list of (name, object) candidates that might contain tools."""
        candidates = []
        for attr in self._TOOL_CONTAINER_ATTRS:
            if hasattr(mcp_instance, attr):
                obj = self._safe_getattr(mcp_instance, attr)
                if obj is not None:
                    candidates.append((f"mcp_instance.{attr}", obj))
        return candidates

    def _try_dict_attributes(self, obj: Any, prefix: str) -> tuple[dict | None, str | None]:
        """Try to find tools in dict-like attributes."""
        # First check if obj itself is dict-like (for direct dict containers)
        if isinstance(obj, dict) or (hasattr(obj, "items") and not hasattr(obj, "__dict__")):
            tools_map = dict(obj) if not isinstance(obj, dict) else obj
            if tools_map:  # Only return if non-empty
                logger.debug(f"[mcp-auth] Found tools dict directly at {prefix}")
                return tools_map, prefix

        # Then check attributes within obj
        for attr in self._TOOL_MAP_ATTRS:
            candidate = self._safe_getattr(obj, attr)

            if candidate is None:
                continue

            # Check if it's dict-like
            if isinstance(candidate, dict) or hasattr(candidate, "items"):
                tools_map = dict(candidate) if not isinstance(candidate, dict) else candidate
                return tools_map, f"{prefix}.{attr}"

        return None, None

    def _try_callable_methods(self, obj: Any, prefix: str) -> tuple[dict | None, str | None]:
        """Try to find tools by calling methods."""
        for method_name in self._TOOL_MAP_METHODS:
            method = self._safe_getattr(obj, method_name)

            if not callable(method):
                continue

            try:
                result = method()

                # Try as dict
                if isinstance(result, dict):
                    return result, f"{prefix}.{method_name}()"

                # Try as iterable of (key, value) pairs
                try:
                    tools_map = dict(result)
                    return tools_map, f"{prefix}.{method_name}()"
                except (TypeError, ValueError):
                    continue

            except Exception as e:
                logger.debug(f"[mcp-auth] {prefix}.{method_name}() failed: {e}")

        return None, None

    def _process_tool(self, tool_name: str, tool_info: Any) -> None:
        """Process a single tool to extract permissions."""
        func = self._extract_callable(tool_info)

        if func is None:
            logger.debug(f"[mcp-auth] Could not extract callable for tool: {tool_name}")
            return

        # Check if public tool
        if getattr(func, "_public_tool", False):
            self.public_tools.add(tool_name)
            logger.debug(f"[mcp-auth] Discovered public tool: {tool_name}")
            return

        # Extract scopes and roles
        scopes = set(getattr(func, "_required_scopes", set()) or set())
        roles = set(getattr(func, "_required_roles", set()) or set())
        any_scopes = set(getattr(func, "_required_any_scopes", set()) or set())

        if scopes or roles or any_scopes:
            self.tool_permissions[tool_name] = {
                "scopes": scopes,
                "roles": roles,
                "any_scopes": any_scopes,
            }
            logger.debug(
                f"[mcp-auth] Discovered permissions for {tool_name}: "
                f"scopes={scopes}, roles={roles}, any_scopes={any_scopes}"
            )

    def _safe_getattr(self, obj: Any, attr: str, default: Any = None) -> Any:
        """Safely get attribute, returning default if not found or error."""
        try:
            return getattr(obj, attr, default)
        except Exception:
            return default

    def _extract_callable(self, tool_info: Any) -> Callable | None:
        """Extract callable from tool_info object."""
        # Try common attribute names
        for attr in self._CALLABLE_ATTRS:
            func = self._safe_getattr(tool_info, attr)
            if callable(func):
                return func

        # If tool_info itself is callable
        if callable(tool_info):
            return tool_info

        # If it's a dict
        if isinstance(tool_info, dict):
            for key in self._CALLABLE_ATTRS:
                if key in tool_info and callable(tool_info[key]):
                    return tool_info[key]

        return None

    def _log_summary(self) -> None:
        """Log configuration summary."""
        logger.info(
            f"[mcp-auth] Configuration: "
            f"Public tools: {len(self.public_tools)}, "
            f"Protected tools: {len(self.tool_permissions)}"
        )

    def _log_discovery_complete(self) -> None:
        """Log discovery completion summary."""
        logger.info(
            f"[mcp-auth] Discovery complete. "
            f"Public: {len(self.public_tools)}, "
            f"Protected: {len(self.tool_permissions)}"
        )

        if self.public_tools:
            public_sorted = sorted(self.public_tools)
            logger.info(f"[mcp-auth] Public tools: {public_sorted}")

        if self.tool_permissions:
            logger.info("[mcp-auth] Protected tools with permissions:")
            for tool_name in sorted(self.tool_permissions.keys()):
                perms = self.tool_permissions[tool_name]
                logger.info(
                    f"[mcp-auth]   - {tool_name}: scopes={perms['scopes']}, "
                    f"any_scopes={perms['any_scopes']}, roles={perms['roles']}"
                )

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        """Authenticate and authorize tool calls."""
        message = context.message
        tool_name = getattr(message, "name", None)

        logger.debug(f"[mcp-auth] Checking access for: {tool_name}")

        # 1. Check if public tool
        if tool_name in self.public_tools:
            logger.debug(f"[mcp-auth] Public tool: {tool_name}")
            return await call_next(context)

        # 2. Authenticate (401)
        user = await self._authenticate(context, tool_name)

        # 3. Authorize (403)
        await self._authorize(user, tool_name)

        # 4. Attach user to message and set in context
        setattr(message, "user", user)
        current_user.set(user)

        logger.info(f"[mcp-auth] Access granted: {user['username']} → {tool_name}")

        # Note: We intentionally don't reset current_user after call_next() because:
        # 1. ContextVars are already request-scoped in async frameworks
        # 2. Downstream middleware (like LoggingMiddleware) needs to read the persona
        #    after the tool execution completes
        return await call_next(context)

    async def _authenticate(self, context: MiddlewareContext, tool_name: str) -> dict:
        """Authenticate and return user info (401 on failure).

        Authentication sources (checked in order):
        1. HTTP Authorization header (from ContextVar set by RestBridgeMiddleware)
        2. Session state (for stdio clients that called login_tool - only in pure stdio mode)
        """
        # Get headers from ContextVar (set by RestBridgeMiddleware for all transports)
        http_headers = get_http_headers()

        # Try extracting token from HTTP headers
        token = _extract_bearer_token(http_headers) if http_headers else None

        # Only try session auth in pure stdio mode (no HTTP headers)
        if not token and not http_headers:
            logger.debug("[mcp-auth] Attempting session auth (no token, no HTTP context)")
            user = await self._get_user_from_session(context)
            if user:
                logger.debug(
                    f"[mcp-auth] Authenticated via session: {user['username']}, "
                    f"roles={user.get('roles', [])}, scopes={user.get('scopes', [])}"
                )
                return user

        if not token:
            logger.warning(f"[mcp-auth] 401: Missing token for {tool_name}")
            raise PermissionError("Authentication required: Missing Authorization header")

        if len(token) > 500:
            raise PermissionError("Authentication required: Invalid token format")

        user = self.auth_service.validate_token(token)

        if not user:
            logger.warning(f"[mcp-auth] 401: Invalid token for {tool_name}")
            raise PermissionError("Authentication required: Invalid or expired token")

        logger.debug(
            f"[mcp-auth] Authenticated: {user['username']}, "
            f"roles={user.get('roles', [])}, scopes={user.get('scopes', [])}"
        )

        return user

    async def _get_user_from_session(self, context: MiddlewareContext) -> dict | None:
        """Try to get authenticated user from module-level session store.

        This supports stdio clients that authenticated via login_tool,
        which stores the user in a module-level dict keyed by session_id.
        """
        try:
            session_id = context.fastmcp_context.session_id or "default"
            user = _session_auth_store.get(session_id)
            if user and isinstance(user, dict) and "username" in user:
                logger.debug(f"[mcp-auth] Found user in session store: {user['username']}")
                return user
        except Exception as e:
            logger.debug(f"[mcp-auth] Could not get from session store: {e}")
        return None

    async def _authorize(self, user: dict, tool_name: str) -> None:
        """Check permissions (403 on failure)."""
        # Check if tool has defined permissions
        if tool_name not in self.tool_permissions:
            if self.default_deny:
                logger.warning(
                    f"[mcp-auth] 403: Tool '{tool_name}' has no defined permissions "
                    f"(default_deny=True)"
                )
                raise PermissionError(
                    f"Access denied: Tool '{tool_name}' requires explicit permissions"
                )
            logger.debug(
                f"[mcp-auth] No permissions for '{tool_name}', allowing (default_deny=False)"
            )
            return

        perms = self.tool_permissions[tool_name]
        user_roles = set(user.get("roles", []))
        user_scopes = set(user.get("scopes", []))

        # Check required roles
        required_roles = perms.get("roles", set())
        if required_roles and not required_roles.intersection(user_roles):
            logger.warning(
                f"[mcp-auth] 403: {user['username']} missing roles. "
                f"Required: {required_roles}, Has: {user_roles}"
            )
            raise PermissionError(f"Access denied: Requires role(s): {', '.join(required_roles)}")

        # Check required scopes (AND-logic: all must be present)
        required_scopes = perms.get("scopes", set())
        if required_scopes and not required_scopes.issubset(user_scopes):
            missing = required_scopes - user_scopes
            logger.warning(
                f"[mcp-auth] 403: {user['username']} missing scopes. "
                f"Required: {required_scopes}, Missing: {missing}"
            )
            raise PermissionError(f"Access denied: Missing scope(s): {', '.join(missing)}")

        # Check any_scopes (OR-logic: at least one must be present)
        required_any_scopes = perms.get("any_scopes", set())
        if required_any_scopes and not required_any_scopes.intersection(user_scopes):
            logger.warning(
                f"[mcp-auth] 403: {user['username']} missing scopes. "
                f"Required one of: {required_any_scopes}, Has: {user_scopes}"
            )
            raise PermissionError(
                f"Access denied: Requires one of: {', '.join(required_any_scopes)}"
            )

        logger.debug("[mcp-auth] Authorization passed")
