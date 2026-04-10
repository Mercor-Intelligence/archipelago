"""
HTTP client with built-in caching support.

Provides a simple interface for making HTTP requests with automatic
ETag/Last-Modified caching.
"""

from loguru import logger

from .cache_middleware import CachedHTTPClient, create_cached_client

__all__ = ["CachedHTTPClient", "create_cached_client", "get_http_client"]


# Global client instance and its configuration
_global_client: CachedHTTPClient | None = None
_global_config: dict | None = None


def get_http_client(
    enable_caching: bool = True, respect_cache_control: bool = True
) -> CachedHTTPClient:
    """
    Get or create the global HTTP client with caching.

    NOTE: This function uses a singleton pattern. The configuration parameters
    (enable_caching, respect_cache_control) are only applied on the FIRST call.
    Subsequent calls return the existing global client regardless of parameters.

    If you need a client with different configuration, use create_cached_client()
    directly to create a new instance.

    Args:
        enable_caching: Whether to enable caching (default: True)
        respect_cache_control: Whether to respect Cache-Control headers

    Returns:
        CachedHTTPClient instance
    """
    global _global_client, _global_config

    current_config = {
        "enable_caching": enable_caching,
        "respect_cache_control": respect_cache_control,
    }

    if _global_client is None:
        _global_client = create_cached_client(
            enable_caching=enable_caching,
            respect_cache_control=respect_cache_control,
        )
        _global_config = current_config
    elif _global_config != current_config:
        logger.warning(
            f"get_http_client() called with config {current_config} but global "
            f"client already exists with config {_global_config}. "
            "Returning existing client. Use create_cached_client() for different configs."
        )

    return _global_client
