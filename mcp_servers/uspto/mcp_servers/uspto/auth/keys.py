"""API key passthrough authentication for the USPTO MCP Server."""

from __future__ import annotations

from mcp_servers.uspto.api_key import get_api_key
from mcp_servers.uspto.utils.errors import AuthenticationError

MIN_API_KEY_LENGTH = 20

# Header name for per-request API key
API_KEY_HEADER = "X-API-KEY"
AUTHORIZATION_HEADER = "Authorization"


class APIKeyManager:
    """Manage API key passthrough authentication."""

    @staticmethod
    def get_api_key_from_context() -> str:
        """
        Extract API key from MCP request context.

        Priority order:
        1. X-API-KEY header (per-request, preferred for true passthrough)
        2. Authorization: Bearer <token> header (UI-friendly passthrough)
        3. CLI --api-key flag (fallback, set at server startup)

        Returns:
            Raw API key for USPTO API passthrough

        Raises:
            AuthenticationError: If API key is missing from both sources
        """
        # Try per-request header first
        api_key = APIKeyManager._get_api_key_from_header()

        # Fall back to CLI-provided key
        if not api_key:
            api_key = get_api_key()

        if not api_key:
            raise AuthenticationError(
                code="MISSING_API_KEY",
                message=(
                    "API key required. Provide X-API-KEY or Authorization: Bearer <token> "
                    "header, or use the --api-key flag."
                ),
                details={
                    "hint": "Include X-API-KEY or Authorization header, or use --api-key flag"
                },
            )

        return api_key

    @staticmethod
    def _get_api_key_from_header() -> str | None:
        """
        Extract API key from X-API-KEY request header.

        HTTP headers are case-insensitive per RFC 7230, so this method
        performs a case-insensitive lookup to handle all variants
        (e.g., X-API-KEY, x-api-key, X-Api-Key).

        Returns:
            API key from header, or None if not present or not in HTTP context
        """
        try:
            from fastmcp.server.dependencies import get_http_headers

            headers = get_http_headers()
            # HTTP headers are case-insensitive per RFC 7230
            # Perform case-insensitive lookup to handle all variants
            api_key_header = API_KEY_HEADER.lower()
            authorization_header = AUTHORIZATION_HEADER.lower()
            api_key_value = None
            authorization_value = None
            for key, value in headers.items():
                lowered = key.lower()
                if lowered == api_key_header:
                    api_key_value = value
                elif lowered == authorization_header:
                    authorization_value = value

            if api_key_value:
                return api_key_value
            if authorization_value:
                token = _parse_bearer_token(authorization_value)
                if token:
                    return token
            return None
        except (RuntimeError, ImportError):
            # No HTTP context available (e.g., stdio transport) or FastMCP not available
            return None

    @staticmethod
    def validate_api_key_format(api_key: str) -> bool:
        """
        Validate API key format (basic sanity check).

        Args:
            api_key: Raw API key

        Returns:
            True if format is valid, False otherwise

        Note:
            Does NOT verify key with USPTO - just checks format.
            Adjust validation based on actual USPTO key format requirements.
        """
        if not api_key:
            return False

        if len(api_key) < MIN_API_KEY_LENGTH:
            return False

        return True


def _parse_bearer_token(value: str | None) -> str | None:
    """Extract a token from an Authorization: Bearer header value."""
    if not value:
        return None
    parts = value.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


__all__ = ["APIKeyManager", "MIN_API_KEY_LENGTH"]
