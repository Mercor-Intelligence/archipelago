"""Provider factory for FMP MCP Server.

This module provides:
- get_provider(): Factory function to get the appropriate provider based on mode
- BaseProvider: Re-exported abstract base class
- ResponseMeta: Re-exported TypedDict for metadata typing

Usage:
    from providers import get_provider

    provider = get_provider()
    result = await provider.get_stock_quote("AAPL")
"""

import sys
from functools import lru_cache
from pathlib import Path

# Support both module and direct execution
try:
    from ..config import Mode, get_config
    from .base import BaseProvider, ResponseMeta
except ImportError:
    # Direct execution - add parent to path for local imports
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config import Mode, get_config
    from providers.base import BaseProvider, ResponseMeta

__all__ = ["get_provider", "BaseProvider", "ResponseMeta"]


@lru_cache
def get_provider() -> BaseProvider:
    """Get the appropriate provider instance based on current mode.

    This factory function returns either an OnlineProvider or OfflineProvider
    based on the server's configuration mode. The result is cached to ensure
    a singleton provider instance.

    Returns:
        BaseProvider: The appropriate provider for the current mode

    Raises:
        ValueError: If an unknown mode is configured

    Example:
        >>> provider = get_provider()
        >>> result = await provider.get_stock_quote("AAPL")
        >>> print(result["_meta"]["mode"])  # "online" or "offline"
    """
    config = get_config()

    if config.mode == Mode.OFFLINE:
        from .offline import OfflineProvider

        return OfflineProvider()

    if config.mode == Mode.ONLINE:
        from .online import OnlineProvider

        return OnlineProvider()

    # This should never happen, but handle it gracefully
    raise ValueError(f"Unknown mode: {config.mode}")


def clear_provider_cache() -> None:
    """Clear the cached provider instance.

    This is useful for testing or when configuration changes.
    After clearing, the next call to get_provider() will create
    a new provider instance based on current configuration.
    """
    get_provider.cache_clear()
