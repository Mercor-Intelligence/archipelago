"""
HTTP caching middleware with ETag and Last-Modified validation.

Transparently caches GET responses and uses conditional requests to
revalidate them, ensuring data freshness while reducing bandwidth.
"""

from typing import Any

import httpx
from loguru import logger

from .cache_storage import CachedResponse, get_cache


class CachedHTTPClient:
    """
    HTTP client with transparent caching middleware.

    Intercepts GET requests, stores responses with validation headers,
    and uses conditional requests (If-None-Match, If-Modified-Since)
    to revalidate cached data.
    """

    def __init__(
        self,
        base_client: httpx.AsyncClient | None = None,
        enable_caching: bool = True,
        respect_cache_control: bool = True,
    ):
        """
        Initialize the cached HTTP client.

        Args:
            base_client: Optional httpx.AsyncClient to wrap
            enable_caching: Whether to enable caching (default: True)
            respect_cache_control: Whether to respect Cache-Control headers
        """
        self._client = base_client or httpx.AsyncClient()
        self._cache = get_cache()
        self._enable_caching = enable_caching
        self._respect_cache_control = respect_cache_control

    def _should_cache(self, method: str, response: httpx.Response) -> bool:
        """
        Determine if a response should be cached.

        Args:
            method: HTTP method
            response: Response object

        Returns:
            True if response should be cached
        """
        # Only cache GET requests
        if method.upper() != "GET":
            return False

        # Don't cache error responses (4xx, 5xx)
        if response.status_code >= 400:
            return False

        # Respect Cache-Control if enabled
        if self._respect_cache_control:
            cache_control = response.headers.get("cache-control", "").lower()

            # Don't cache if no-store or private
            if "no-store" in cache_control or "private" in cache_control:
                logger.debug(f"Not caching {response.request.url}: Cache-Control={cache_control}")
                return False

        return True

    def _add_conditional_headers(
        self, headers: dict[str, str], cached: CachedResponse
    ) -> dict[str, str]:
        """
        Add conditional request headers for validation.

        Args:
            headers: Existing request headers
            cached: Cached response with validators

        Returns:
            Updated headers with If-None-Match and/or If-Modified-Since
        """
        updated_headers = headers.copy()

        # Add If-None-Match if ETag available
        if cached.etag:
            updated_headers["if-none-match"] = cached.etag
            logger.debug(f"Added If-None-Match: {cached.etag}")

        # Add If-Modified-Since if Last-Modified available
        if cached.last_modified:
            updated_headers["if-modified-since"] = cached.last_modified
            logger.debug(f"Added If-Modified-Since: {cached.last_modified}")

        return updated_headers

    async def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """
        Perform a GET request with caching.

        Args:
            url: URL to request
            headers: Optional request headers
            **kwargs: Additional arguments for httpx.get

        Returns:
            httpx.Response object
        """
        if not self._enable_caching:
            # Caching disabled, pass through
            response = await self._client.get(url, headers=headers, **kwargs)
            # Mark as not from cache
            setattr(response, "_from_cache", False)
            return response

        method = "GET"
        headers = headers or {}

        # Check cache
        cached = self._cache.get(method, url, headers)

        if cached is None:
            # Cache miss - perform normal GET
            logger.debug(f"Cache MISS: {url}")
            response = await self._client.get(url, headers=headers, **kwargs)

            # Store in cache if appropriate
            if self._should_cache(method, response):
                cached_response = CachedResponse.from_response(response)
                self._cache.set(method, url, cached_response, headers)
                logger.debug(f"Cached response for {url}")

            # Mark as not from cache
            setattr(response, "_from_cache", False)
            return response

        # Cache hit - perform conditional GET if validators available
        if cached.has_validators():
            logger.debug(f"Cache HIT with validators: {url}")

            # Add conditional headers
            conditional_headers = self._add_conditional_headers(headers, cached)

            # Perform conditional GET
            response = await self._client.get(url, headers=conditional_headers, **kwargs)

            # Record revalidation only after successful request
            self._cache.record_revalidation()

            if response.status_code == 304:
                # Not Modified - use cached response
                logger.debug(f"304 Not Modified: {url} - using cached response")

                # Return a response object with cached content
                # We need to create a response-like object
                cached_response = httpx.Response(
                    status_code=200,  # Return 200 to caller (transparent)
                    headers=httpx.Headers(cached.headers),
                    content=cached.content,
                    request=response.request,
                )
                # Mark response as served from cache
                setattr(cached_response, "_from_cache", True)
                return cached_response

            elif response.status_code == 200:
                # Fresh data - update cache
                logger.debug(f"200 OK: {url} - updating cache")

                if self._should_cache(method, response):
                    cached_response = CachedResponse.from_response(response)
                    self._cache.set(method, url, cached_response, headers)

                # Mark as not from cache (fresh data)
                setattr(response, "_from_cache", False)
                return response

            else:
                # Other status code (error) - pass through without caching
                logger.debug(f"Status {response.status_code}: {url} - not caching error")
                # Mark as not from cache
                setattr(response, "_from_cache", False)
                return response

        else:
            # Cache hit but no validators - re-fetch
            # (Could add TTL-based serving here if desired)
            logger.debug(f"Cache HIT without validators: {url} - re-fetching")
            response = await self._client.get(url, headers=headers, **kwargs)

            if self._should_cache(method, response):
                cached_response = CachedResponse.from_response(response)
                self._cache.set(method, url, cached_response, headers)

            # Mark as not from cache (re-fetched)
            setattr(response, "_from_cache", False)
            return response

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """
        Perform an HTTP request with caching for GET methods.

        Args:
            method: HTTP method
            url: URL to request
            headers: Optional request headers
            **kwargs: Additional arguments for httpx.request

        Returns:
            httpx.Response object
        """
        if method.upper() == "GET":
            return await self.get(url, headers=headers, **kwargs)
        else:
            # Non-GET methods pass through without caching
            return await self._client.request(method, url, headers=headers, **kwargs)

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        return self._cache.get_stats()

    def clear_cache(self) -> None:
        """Clear all cached responses."""
        self._cache.clear()


# Convenience function to create a cached client
def create_cached_client(
    enable_caching: bool = True, respect_cache_control: bool = True
) -> CachedHTTPClient:
    """
    Create a cached HTTP client.

    Args:
        enable_caching: Whether to enable caching (default: True)
        respect_cache_control: Whether to respect Cache-Control headers

    Returns:
        CachedHTTPClient instance
    """
    return CachedHTTPClient(
        enable_caching=enable_caching,
        respect_cache_control=respect_cache_control,
    )
