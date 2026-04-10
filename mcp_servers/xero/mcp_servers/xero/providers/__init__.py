"""Data providers for Xero MCP server."""

from __future__ import annotations

from .base import BaseProvider
from .offline import OfflineProvider

try:
    from .online import OnlineProvider  # type: ignore[misc]
except Exception as import_error:
    _online_provider_import_error = import_error

    class OnlineProvider:
        """Sentinel OnlineProvider used when dependencies are unavailable.

        Attempting to instantiate the online provider without the PKCE module
        configured raises a descriptive error while keeping offline tests importable.
        """

        def __init__(self, *args, **kwargs):
            message = (
                "OnlineProvider is unavailable because the OAuth PKCE dependencies "
                "failed to import. Please set OAUTH_PKCE_MODULE_PATH and install "
                "required shared infrastructure before using online mode."
            )
            raise RuntimeError(message) from _online_provider_import_error

        def __getattr__(self, item):
            raise RuntimeError(
                "OnlineProvider cannot be used until OAuth PKCE dependencies are available"
            ) from _online_provider_import_error


__all__ = ["BaseProvider", "OfflineProvider", "OnlineProvider"]
