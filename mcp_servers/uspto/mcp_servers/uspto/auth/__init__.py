"""Authentication and rate limiting for the USPTO MCP Server."""

from mcp_servers.uspto.auth.decorators import (
    clear_rate_limit_info,
    get_rate_limit_info,
    rate_limit,
    require_api_key,
)
from mcp_servers.uspto.auth.keys import API_KEY_HEADER, MIN_API_KEY_LENGTH, APIKeyManager
from mcp_servers.uspto.auth.rate_limiter import (
    RATE_LIMIT_STATE_KEY,
    RateLimiter,
    RateLimitInfo,
    RateLimitResult,
    TokenBucket,
    rate_limiter,
)

__all__ = [
    "API_KEY_HEADER",
    "APIKeyManager",
    "MIN_API_KEY_LENGTH",
    "RATE_LIMIT_STATE_KEY",
    "RateLimitInfo",
    "RateLimitResult",
    "RateLimiter",
    "TokenBucket",
    "clear_rate_limit_info",
    "get_rate_limit_info",
    "rate_limit",
    "rate_limiter",
    "require_api_key",
]
