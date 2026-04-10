"""Factory helpers for choosing between online and offline USPTO clients."""

from __future__ import annotations

from mcp_servers.uspto.api.client import USPTOAPIClient
from mcp_servers.uspto.api.contracts import USPTOClient
from mcp_servers.uspto.api.offline_client import OfflineUSPTOClient
from mcp_servers.uspto.config import is_online_mode


def get_uspto_client(api_key: str | None = None) -> USPTOClient:
    """Return the appropriate USPTO client based on the current mode.

    In online mode, retrieves API key from context if not provided.
    In offline mode, no API key is needed.

    Args:
        api_key: Optional API key. If None in online mode, will be retrieved from context.

    Returns:
        USPTOClient instance (online or offline implementation)

    Raises:
        AuthenticationError: If in online mode and no API key is available
    """

    if is_online_mode():
        # Only retrieve API key if we're in online mode and it wasn't provided
        if api_key is None:
            from mcp_servers.uspto.auth.keys import APIKeyManager

            api_key = APIKeyManager.get_api_key_from_context()
        return USPTOAPIClient(api_key=api_key, offline_mode=False)

    # Offline mode: no API key needed
    return OfflineUSPTOClient()


__all__ = ["OfflineUSPTOClient", "get_uspto_client"]
