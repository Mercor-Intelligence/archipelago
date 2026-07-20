import httpx
from utils.config import FMP_API_BASE_URL, FMP_API_KEY


def get_api_client(base_url: str | None = None) -> httpx.Client:
    """Get an authenticated HTTP client for FMP API.

    Args:
        base_url: Optional base URL override (e.g., for v4 endpoints)
    """
    url = base_url or FMP_API_BASE_URL
    return httpx.Client(
        base_url=url,
        params={"apikey": FMP_API_KEY},
        timeout=30.0,
    )
