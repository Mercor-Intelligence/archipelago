"""
HTTP caching middleware and utilities.

Provides transparent caching for HTTP requests with ETag and Last-Modified validation.
"""

from .cache_middleware import CachedHTTPClient, create_cached_client
from .cache_storage import CachedResponse, HTTPCacheStorage, get_cache
from .http_client import get_http_client
from .version import __version__

__all__ = [
    "__version__",
    "CachedHTTPClient",
    "create_cached_client",
    "CachedResponse",
    "HTTPCacheStorage",
    "get_cache",
    "get_http_client",
]
