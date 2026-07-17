"""REST bridge middleware for MCP servers.

Provides middleware to handle REST bridge integration by passing
HTTP headers from the REST bridge to downstream middleware via ContextVar.
"""

from typing import Any
from weakref import WeakSet

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from .context import set_http_headers
from .logging import get_logger


class RestBridgeMiddleware(Middleware):
    """Middleware to synthesize HTTP request context from REST bridge headers.

    The mcp_rest_bridge.py script injects a _headers parameter containing
    HTTP headers into every tool call. This middleware:
    1. Extracts the _headers parameter
    2. Creates a synthetic Starlette Request object
    3. Sets it in the FastMCP HTTP request context
    4. Removes _headers from arguments to prevent validation errors

    This allows auth middleware and other HTTP-aware middleware to work
    seamlessly with REST bridge requests.

    This middleware is not registered automatically. Other middleware that
    depends on it (like AuthGuard) will call ensure_registered() to add it
    when needed.

    Usage:
        from mcp_middleware import RestBridgeMiddleware

        # Manual registration
        mcp.add_middleware(RestBridgeMiddleware())

        # Or let dependent middleware register it automatically
        # (e.g., AuthGuard will call RestBridgeMiddleware.ensure_registered(mcp))
    """

    # Track which MCP instances have this middleware registered
    _registered_instances: WeakSet = WeakSet()

    @classmethod
    def ensure_registered(cls, mcp_instance: Any) -> None:
        """Ensure RestBridgeMiddleware is registered for the given MCP instance.

        This is called by other middleware (like AuthGuard) that depend on
        RestBridgeMiddleware to synthesize HTTP context from REST bridge headers.

        Args:
            mcp_instance: FastMCP instance to register middleware on
        """
        log = get_logger()
        if mcp_instance in cls._registered_instances:
            log.debug("[REST-BRIDGE] Already registered")
            return

        log.info("[REST-BRIDGE] Auto-registering RestBridgeMiddleware")
        mcp_instance.add_middleware(cls())
        cls._registered_instances.add(mcp_instance)

    @classmethod
    def is_registered(cls, mcp_instance: Any) -> bool:
        """Check if RestBridgeMiddleware is registered for the given MCP instance.

        Args:
            mcp_instance: FastMCP instance to check

        Returns:
            True if middleware is registered, False otherwise
        """
        return mcp_instance in cls._registered_instances

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        """Extract _headers and synthesize HTTP request context.

        Only synthesizes an HTTP request if:
        1. _headers parameter is present (REST bridge mode)
        2. No real HTTP request context exists (not HTTP/SSE transport)

        This ensures real HTTP requests take precedence over synthetic ones.
        """
        message = context.message

        # Check if there's already an HTTP request context (from HTTP/SSE transport)
        # If so, extract headers from it
        headers_dict = None
        try:
            existing_request = context.fastmcp_context.get_http_request()
            if existing_request and existing_request.headers:
                headers_dict = dict(existing_request.headers)
        except Exception:
            pass

        # If no HTTP request, extract _headers from params._meta field
        # The bridge puts _headers in params._meta to pass through Pydantic extra fields
        if headers_dict is None:
            # Check request_context.meta first (per FastMCP documentation)
            if hasattr(context.fastmcp_context, "request_context"):
                req_ctx = context.fastmcp_context.request_context
                if hasattr(req_ctx, "meta") and req_ctx.meta is not None:
                    # Check if meta has _headers as an attribute (Pydantic model)
                    if hasattr(req_ctx.meta, "_headers"):
                        headers_dict = getattr(req_ctx.meta, "_headers")
                    # Fallback: check if meta is a dict
                    elif isinstance(req_ctx.meta, dict):
                        headers_dict = req_ctx.meta.get("_headers")

            # Fallback: check arguments for backward compatibility (old bridge behavior)
            if headers_dict is None:
                args = getattr(message, "arguments", None)
                if args is not None and isinstance(args, dict) and "_headers" in args:
                    headers_dict = args.get("_headers")
                    # Remove _headers to prevent unexpected keyword arguments
                    filtered_args = {k: v for k, v in args.items() if k != "_headers"}
                    message.arguments = filtered_args

        # Set headers in ContextVar for downstream middleware (like AuthGuard)
        if headers_dict:
            set_http_headers(headers_dict)

        # Proceed with the request
        return await call_next(context)
