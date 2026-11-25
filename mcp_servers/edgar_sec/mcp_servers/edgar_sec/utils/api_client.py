"""HTTP client for SEC EDGAR API with rate limiting."""

import httpx
from aiolimiter import AsyncLimiter
from config import EDGAR_API_BASE_URL, EDGAR_OFFLINE_MODE, EDGAR_USER_AGENT

# Rate limiter: 10 requests per second as per SEC guidelines
_rate_limiter: AsyncLimiter | None = None


def _get_rate_limiter() -> AsyncLimiter:
    """Get or create the rate limiter (lazy initialization)."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = AsyncLimiter(10, 1.0)
    return _rate_limiter


def reset_rate_limiter() -> None:
    """Reset the rate limiter singleton (for testing)."""
    global _rate_limiter
    _rate_limiter = None


async def rate_limited_get(url: str, **kwargs) -> httpx.Response:
    """Make a rate-limited GET request to SEC EDGAR API.

    Creates a fresh HTTP client per request to avoid stale TCP DNS
    connections when resolv.conf uses 'options use-vc' (DNS over TCP).

    Enforces 10 requests per second limit.

    Args:
        url: URL path (relative to base URL)
        **kwargs: Additional arguments for httpx.get()

    Returns:
        httpx.Response object

    Raises:
        RuntimeError: If called while EDGAR_OFFLINE_MODE is enabled
    """
    if EDGAR_OFFLINE_MODE:
        raise RuntimeError(f"Cannot make live SEC API requests in offline mode. Requested: {url}")
    async with _get_rate_limiter():
        async with httpx.AsyncClient(
            base_url=EDGAR_API_BASE_URL,
            headers={"User-Agent": EDGAR_USER_AGENT},
            timeout=300.0,
            follow_redirects=True,
        ) as client:
            return await client.get(url, **kwargs)
