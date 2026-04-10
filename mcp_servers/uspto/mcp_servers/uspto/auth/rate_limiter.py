"""Token bucket rate limiter for the USPTO MCP Server."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import ClassVar

# Context state key for storing rate limit info
RATE_LIMIT_STATE_KEY = "rate_limit_info"


@dataclass
class TokenBucket:
    """Token bucket state for rate limiting."""

    tokens: float
    last_refill: float
    capacity: int


@dataclass
class RateLimitResult:
    """Rate limit check result."""

    allowed: bool
    limit: int
    remaining: int
    reset_at: int
    retry_after: int = 0


@dataclass
class RateLimitInfo:
    """Rate limit information for client consumption.

    This can be included in tool responses to help clients
    proactively manage their request rate.
    """

    limit: int
    remaining: int
    reset_at: int
    endpoint_category: str

    def to_dict(self) -> dict[str, int | str]:
        """Convert to dictionary for inclusion in responses."""
        return {
            "X-RateLimit-Limit": self.limit,
            "X-RateLimit-Remaining": self.remaining,
            "X-RateLimit-Reset": self.reset_at,
            "X-RateLimit-Category": self.endpoint_category,
        }


@dataclass
class RateLimiter:
    """
    Token bucket rate limiter with per-endpoint limits.

    Session-scoped: rate limits are stored in-memory and cleared when
    the MCP session ends. This is acceptable because:
    1. Each MCP session is typically a single user interaction
    2. Rate limits protect the USPTO API, not our server
    3. Simpler than distributed rate limiting
    """

    DEFAULT_LIMITS: ClassVar[dict[str, int]] = {
        "search": 50,
        "retrieval": 100,
        "status_codes": 10,
        "status_normalize": 100,
        "documents": 50,
        "documents_download": 100,
        "foreign_priority": 50,
        "export": 20,
        "audit": 50,
        "default": 60,
    }

    storage: dict[str, TokenBucket] = field(default_factory=dict)
    limits: dict[str, int] = field(default_factory=lambda: dict(RateLimiter.DEFAULT_LIMITS))

    def _get_rate_limit_key(self, endpoint_category: str) -> str:
        """Generate rate limit key for endpoint category."""
        return f"rate_limit:{endpoint_category}"

    def get_limit(self, endpoint_category: str) -> int:
        """Get the rate limit for an endpoint category."""
        limit = self.limits.get(endpoint_category, self.limits["default"])
        # Guard against zero or negative limits to prevent division by zero
        return max(1, limit)

    def check_rate_limit(self, endpoint_category: str) -> RateLimitResult:
        """
        Check if request is within rate limit.

        Args:
            endpoint_category: Category of endpoint (e.g., "search", "retrieval")

        Returns:
            RateLimitResult with allowed=True/False and metadata

        Algorithm:
            1. Get current token count for endpoint_category
            2. Calculate tokens regenerated since last check
            3. Add regenerated tokens (capped at limit)
            4. If tokens >= 1, consume 1 token and allow request
            5. Otherwise, deny and return retry_after
        """
        key = self._get_rate_limit_key(endpoint_category)
        limit = self.get_limit(endpoint_category)
        now = time.time()

        bucket = self.storage.get(key)

        if not bucket:
            # First request for this endpoint - initialize bucket
            bucket = TokenBucket(
                tokens=limit - 1,  # Consume 1 token for this request
                last_refill=now,
                capacity=limit,
            )
            self.storage[key] = bucket

            return RateLimitResult(
                allowed=True,
                limit=limit,
                remaining=int(bucket.tokens),
                reset_at=int(now + 60),
            )

        # Calculate token refill
        # Guard against negative elapsed time (clock adjustments, NTP sync, VM restore)
        elapsed = max(0, now - bucket.last_refill)
        tokens_to_add = (elapsed / 60.0) * limit  # Refill rate: limit per minute

        # Update bucket with bounds checking
        bucket.tokens = max(0, min(bucket.capacity, bucket.tokens + tokens_to_add))
        bucket.last_refill = now

        # Check if request allowed
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return RateLimitResult(
                allowed=True,
                limit=limit,
                remaining=int(bucket.tokens),
                reset_at=int(now + 60),
            )
        else:
            # Calculate retry_after: time until 1 token is available
            tokens_needed = 1 - bucket.tokens
            # Guard against division by zero (limit is already guarded in get_limit,
            # but this is defensive programming)
            retry_after = int((tokens_needed / max(1, limit)) * 60) + 1
            return RateLimitResult(
                allowed=False,
                limit=limit,
                remaining=0,
                retry_after=retry_after,
                reset_at=int(now + retry_after),
            )

    def reset(self, endpoint_category: str | None = None) -> None:
        """
        Reset rate limit state.

        Args:
            endpoint_category: If provided, reset only this category.
                              If None, reset all categories.
        """
        if endpoint_category is not None:
            key = self._get_rate_limit_key(endpoint_category)
            self.storage.pop(key, None)
        else:
            self.storage.clear()


# Global rate limiter instance (session-scoped)
rate_limiter = RateLimiter()


__all__ = [
    "RATE_LIMIT_STATE_KEY",
    "RateLimitInfo",
    "RateLimitResult",
    "RateLimiter",
    "TokenBucket",
    "rate_limiter",
]
