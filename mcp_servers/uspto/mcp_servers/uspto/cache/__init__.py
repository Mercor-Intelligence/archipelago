"""Session-scoped cache helpers for the USPTO MCP server."""

from mcp_servers.uspto.cache.search_cache import (
    cache_search_results,
    generate_cache_key,
    get_cached_search,
    reset_search_cache_metrics,
)
from mcp_servers.uspto.cache.status_codes_cache import (
    fetch_and_cache_status_codes,
    get_cached_status_codes,
    get_status_code_description,
    is_status_codes_cached,
    purge_expired_status_codes,
    status_codes_cache_cutoff,
)

__all__ = [
    "cache_search_results",
    "fetch_and_cache_status_codes",
    "generate_cache_key",
    "get_cached_search",
    "reset_search_cache_metrics",
    "get_cached_status_codes",
    "get_status_code_description",
    "is_status_codes_cached",
    "purge_expired_status_codes",
    "status_codes_cache_cutoff",
]
