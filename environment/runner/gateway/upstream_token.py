"""Per-boot secret start.sh mints and the gateway sends to each upstream MCP app.

It keeps a non-root model on the app's loopback off tools the gateway withholds. Never log it.
"""

import os
from pathlib import Path

MCP_GATEWAY_TOKEN_ENV = "MCP_GATEWAY_TOKEN"
MCP_GATEWAY_TOKEN_FILE = Path("/run/mcp_gateway/token")
# Lowercase so it replaces, rather than duplicates, the same header in the
# incoming request headers fastmcp forwards upstream.
MCP_GATEWAY_TOKEN_HEADER = "x-mcp-gateway-token"


def mcp_gateway_token() -> str | None:
    """The token from the environment, else the root-only file; None if absent."""
    token = os.environ.get(MCP_GATEWAY_TOKEN_ENV, "").strip()
    if token:
        return token
    try:
        token = MCP_GATEWAY_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def with_gateway_token(
    headers: dict[str, str] | None, token: str | None
) -> dict[str, str] | None:
    """``headers`` plus the token header, replacing any of that name in any case."""
    if not token:
        return headers
    merged = {
        k: v
        for k, v in (headers or {}).items()
        if k.lower() != MCP_GATEWAY_TOKEN_HEADER
    }
    merged[MCP_GATEWAY_TOKEN_HEADER] = token
    return merged


def inject_gateway_token(server: dict[str, object], token: str | None) -> None:
    """Add the token header in place to one URL-bearing server config dict."""
    if not token or not server.get("url"):
        return
    existing = server.get("headers")
    headers = with_gateway_token(
        {str(k): str(v) for k, v in existing.items()}
        if isinstance(existing, dict)
        else None,
        token,
    )
    server["headers"] = headers
