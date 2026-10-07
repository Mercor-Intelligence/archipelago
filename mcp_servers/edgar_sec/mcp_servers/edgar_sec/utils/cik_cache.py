"""CIK company tickers cache."""

import os
from pathlib import Path

from config import EDGAR_OFFLINE_MODE
from diskcache import Cache
from repositories import get_repository

# Cache directory - use app's provisioned data dir (APP_FS_ROOT) for containerized/sandboxed
# environments where /tmp may be read-only or have restricted permissions
_default_cache_dir = os.path.join(
    os.environ.get("APP_FS_ROOT", "/filesystem"), ".cache", "edgar_sec"
)
CACHE_DIR = Path(os.environ.get("CACHE_DIR", _default_cache_dir))
CACHE_KEY = "company_tickers"
CACHE_TTL = 86400  # 24 hours

# Lazy initialization of disk cache
_cache: Cache | None = None


def _get_cache() -> Cache:
    """Lazily initialize the disk cache."""
    global _cache
    if _cache is None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache = Cache(str(CACHE_DIR))
    return _cache


async def get_company_tickers() -> dict:
    """Get company tickers data (from repository - online or offline).

    Returns:
        Dictionary with company data in the format:
        {
            "0": {"cik_str": int, "ticker": str, "title": str},
            "1": {"cik_str": int, "ticker": str, "title": str},
            ...
        }
    """
    # In offline mode, always read from repository (local files)
    if EDGAR_OFFLINE_MODE:
        repo = get_repository()
        return await repo.get_company_tickers()

    # In online mode, check cache first
    cache = _get_cache()
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached

    # Fetch from repository (online API)
    repo = get_repository()
    data = await repo.get_company_tickers()

    # Cache the result
    cache.set(CACHE_KEY, data, expire=CACHE_TTL)

    return data
