"""ASGI middleware for the environment gateway."""

from starlette.types import ASGIApp, Receive, Scope, Send


class NormalizeMcpPathMiddleware:
    """Serve a bare ``/mcp`` directly instead of 307-redirecting to ``/mcp/``.

    The MCP gateway is mounted at ``/mcp`` (a Starlette ``Mount``), which only
    matches ``/mcp/...``. A request to a bare ``/mcp`` therefore misses the
    mount and Starlette's ``redirect_slashes`` returns an HTTP 307 to
    ``/mcp/``. Some MCP streamable-HTTP clients do not follow that redirect on
    the streaming POST and drop the connection before the MCP handshake
    completes.

    Rewrite the exact request path ``/mcp`` to ``/mcp/`` in-process, before
    routing, so the mounted gateway serves it directly (HTTP 200, no redirect).
    ``/mcp/`` requests are untouched.

    Implemented as pure ASGI (NOT ``BaseHTTPMiddleware``) so the MCP streaming
    request/response bodies pass straight through without being buffered.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            # Mutate path (used by Starlette routing) and raw_path together so
            # routing is consistent; query string lives in scope["query_string"]
            # and is untouched.
            scope["path"] = "/mcp/"
            if scope.get("raw_path"):
                scope["raw_path"] = b"/mcp/"
        await self.app(scope, receive, send)
