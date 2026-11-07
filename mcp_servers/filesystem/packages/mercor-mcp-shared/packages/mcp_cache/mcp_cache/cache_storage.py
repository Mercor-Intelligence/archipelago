"""
HTTP response cache storage with ETag and Last-Modified support.

Stores HTTP responses along with their validation headers for conditional requests.
"""

import hashlib
import threading
from dataclasses import dataclass
from typing import Any


@dataclass
class CachedResponse:
    """Represents a cached HTTP response with validation headers."""

    status_code: int
    headers: dict[str, str]
    content: bytes
    etag: str | None = None
    last_modified: str | None = None

    @classmethod
    def from_response(cls, response: Any) -> "CachedResponse":
        """
        Create a CachedResponse from an httpx.Response object.

        Args:
            response: httpx.Response object

        Returns:
            CachedResponse instance
        """
        # Extract validation headers
        etag = response.headers.get("etag")
        last_modified = response.headers.get("last-modified")

        return cls(
            status_code=response.status_code,
            headers=dict(response.headers),
            content=response.content,
            etag=etag,
            last_modified=last_modified,
        )

    def has_validators(self) -> bool:
        """Check if this response has validation headers."""
        return self.etag is not None or self.last_modified is not None


class HTTPCacheStorage:
    """
    Thread-safe LRU cache for HTTP responses.

    Stores responses with their ETag and Last-Modified headers for
    conditional request validation.
    """

    def __init__(self, max_size: int = 1000):
        """
        Initialize the cache storage.

        Args:
            max_size: Maximum number of cached responses (LRU eviction)
        """
        self._cache: dict[str, CachedResponse] = {}
        self._access_order: list[str] = []  # Track access for LRU
        self._lock = threading.RLock()
        self._max_size = max_size
        self._hits = 0
        self._misses = 0
        self._revalidations = 0

    def _generate_cache_key(
        self, method: str, url: str, headers: dict[str, str] | None = None
    ) -> str:
        """
        Generate a cache key from request parameters.

        Includes auth headers to prevent cross-user bleed.

        Args:
            method: HTTP method (should be GET)
            url: Request URL
            headers: Request headers (especially auth headers)

        Returns:
            Cache key string
        """
        # Extract relevant headers for cache key (especially auth)
        key_headers = {}
        if headers:
            # Include authorization headers in cache key
            # HTTP headers are case-insensitive, so normalize to lowercase
            headers_lower = {k.lower(): v for k, v in headers.items()}
            for header in ["authorization", "x-api-key", "cookie"]:
                if header in headers_lower:
                    key_headers[header] = headers_lower[header]

        # Create stable key
        key_parts = [
            method.upper(),
            url,
            str(sorted(key_headers.items())),
        ]
        key_string = "|".join(key_parts)

        # Hash for compact key
        return hashlib.sha256(key_string.encode()).hexdigest()

    def get(
        self, method: str, url: str, headers: dict[str, str] | None = None
    ) -> CachedResponse | None:
        """
        Retrieve a cached response.

        Args:
            method: HTTP method
            url: Request URL
            headers: Request headers

        Returns:
            CachedResponse if found, None otherwise
        """
        cache_key = self._generate_cache_key(method, url, headers)

        with self._lock:
            cached = self._cache.get(cache_key)

            if cached is None:
                self._misses += 1
                return None

            # Update LRU access order
            if cache_key in self._access_order:
                self._access_order.remove(cache_key)
            self._access_order.append(cache_key)

            self._hits += 1
            return cached

    def set(
        self,
        method: str,
        url: str,
        response: CachedResponse,
        headers: dict[str, str] | None = None,
    ) -> None:
        """
        Store a response in the cache.

        Args:
            method: HTTP method
            url: Request URL
            response: CachedResponse to store
            headers: Request headers
        """
        cache_key = self._generate_cache_key(method, url, headers)

        with self._lock:
            # Implement LRU eviction
            if len(self._cache) >= self._max_size and cache_key not in self._cache:
                # Remove least recently used entry
                if self._access_order:
                    lru_key = self._access_order.pop(0)
                    self._cache.pop(lru_key, None)

            self._cache[cache_key] = response

            # Update access order
            if cache_key in self._access_order:
                self._access_order.remove(cache_key)
            self._access_order.append(cache_key)

    def delete(self, method: str, url: str, headers: dict[str, str] | None = None) -> None:
        """
        Remove an entry from the cache.

        Args:
            method: HTTP method
            url: Request URL
            headers: Request headers
        """
        cache_key = self._generate_cache_key(method, url, headers)

        with self._lock:
            self._cache.pop(cache_key, None)
            if cache_key in self._access_order:
                self._access_order.remove(cache_key)

    def clear(self) -> None:
        """Clear all cached responses."""
        with self._lock:
            self._cache.clear()
            self._access_order.clear()
            self._hits = 0
            self._misses = 0
            self._revalidations = 0

    def record_revalidation(self) -> None:
        """Record that a conditional request was made."""
        with self._lock:
            self._revalidations += 1

    def get_stats(self) -> dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dictionary with hits, misses, size, revalidations, and hit rate
        """
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total * 100) if total > 0 else 0.0

            return {
                "hits": self._hits,
                "misses": self._misses,
                "revalidations": self._revalidations,
                "size": len(self._cache),
                "hit_rate": round(hit_rate, 2),
            }


# Global cache instance
_global_cache = HTTPCacheStorage()


def get_cache() -> HTTPCacheStorage:
    """Get the global HTTP cache instance."""
    return _global_cache
