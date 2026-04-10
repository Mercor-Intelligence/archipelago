"""Per-tenant rate limiter with thread-safe asyncio.Lock support."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from loguru import logger


@dataclass
class TenantRateLimitState:
    """Rate limit state for a single tenant."""

    # Minute-based rate limiting (60 requests/minute)
    minute_count: int = 0
    minute_reset_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Day-based rate limiting (5000 requests/day)
    day_count: int = 0
    day_reset_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Thread-safety lock
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class PerTenantRateLimiter:
    """
    Thread-safe per-tenant rate limiter for Xero API.

    Enforces:
    - 60 requests per minute per tenant
    - 5000 requests per day per tenant
    - Thread-safe operations using asyncio.Lock
    """

    def __init__(self, requests_per_minute: int = 60, requests_per_day: int = 5000):
        """
        Initialize per-tenant rate limiter.

        Args:
            requests_per_minute: Maximum requests per minute per tenant (default: 60)
            requests_per_day: Maximum requests per day per tenant (default: 5000)
        """
        self.requests_per_minute = requests_per_minute
        self.requests_per_day = requests_per_day
        self._tenant_states: dict[str, TenantRateLimitState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._creation_lock = asyncio.Lock()  # Protects lock creation

        logger.info(
            f"Initialized PerTenantRateLimiter: {requests_per_minute}/min, "
            f"{requests_per_day}/day per tenant"
        )

    async def _get_or_create_lock(self, tenant_id: str) -> asyncio.Lock:
        """
        Get or create lock for tenant (thread-safe with atomic creation).

        Args:
            tenant_id: Xero tenant ID

        Returns:
            asyncio.Lock for the tenant
        """
        if tenant_id not in self._locks:
            async with self._creation_lock:
                # Double-check after acquiring lock
                if tenant_id not in self._locks:
                    self._locks[tenant_id] = asyncio.Lock()
        return self._locks[tenant_id]

    def _get_or_create_state(self, tenant_id: str) -> TenantRateLimitState:
        """
        Get or create rate limit state for tenant.

        Args:
            tenant_id: Xero tenant ID

        Returns:
            Rate limit state for the tenant
        """
        if tenant_id not in self._tenant_states:
            now = datetime.now(UTC)
            # Set minute reset to next minute
            minute_reset = now + timedelta(seconds=60)
            # Set day reset to next midnight UTC
            day_reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

            self._tenant_states[tenant_id] = TenantRateLimitState(
                minute_count=0,
                minute_reset_at=minute_reset,
                day_count=0,
                day_reset_at=day_reset,
            )
            logger.debug(f"Created new rate limit state for tenant: {tenant_id}")

        return self._tenant_states[tenant_id]

    def _should_reset_minute(self, state: TenantRateLimitState) -> bool:
        """
        Check if minute counter should be reset.

        Args:
            state: Tenant rate limit state

        Returns:
            True if minute window has passed
        """
        return datetime.now(UTC) >= state.minute_reset_at

    def _should_reset_day(self, state: TenantRateLimitState) -> bool:
        """
        Check if day counter should be reset.

        Args:
            state: Tenant rate limit state

        Returns:
            True if day window has passed
        """
        return datetime.now(UTC) >= state.day_reset_at

    def _reset_minute_counter(self, state: TenantRateLimitState) -> None:
        """
        Reset minute counter and update reset time.

        Args:
            state: Tenant rate limit state
        """
        now = datetime.now(UTC)
        state.minute_count = 0
        state.minute_reset_at = now + timedelta(seconds=60)
        logger.debug(f"Reset minute counter. Next reset at: {state.minute_reset_at.isoformat()}")

    def _reset_day_counter(self, state: TenantRateLimitState) -> None:
        """
        Reset day counter and update reset time.

        Args:
            state: Tenant rate limit state
        """
        now = datetime.now(UTC)
        state.day_count = 0
        state.day_reset_at = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        logger.debug(f"Reset day counter. Next reset at: {state.day_reset_at.isoformat()}")

    async def check_and_wait(self, tenant_id: str) -> None:
        """
        Check rate limits and wait if necessary (thread-safe).

        This method:
        1. Acquires tenant-specific lock
        2. Resets counters if windows have passed
        3. Waits if limits are exceeded
        4. Does NOT increment counters (call record_request after successful API call)

        Args:
            tenant_id: Xero tenant ID

        Raises:
            ValueError: If tenant_id is None or empty
        """
        if not tenant_id:
            raise ValueError("tenant_id cannot be None or empty")

        # Acquire tenant-specific lock for thread-safe operations
        lock = await self._get_or_create_lock(tenant_id)
        async with lock:
            state = self._get_or_create_state(tenant_id)

            # Reset counters if windows have passed
            if self._should_reset_minute(state):
                self._reset_minute_counter(state)

            if self._should_reset_day(state):
                self._reset_day_counter(state)

            # Check minute limit
            if state.minute_count >= self.requests_per_minute:
                wait_seconds = (state.minute_reset_at - datetime.now(UTC)).total_seconds()
                if wait_seconds > 0:
                    logger.warning(
                        f"Minute rate limit reached for tenant {tenant_id}. "
                        f"Waiting {wait_seconds:.1f}s until reset."
                    )
                    await asyncio.sleep(wait_seconds)
                    # Reset counter after waiting
                    self._reset_minute_counter(state)

            # Check day limit
            if state.day_count >= self.requests_per_day:
                wait_seconds = (state.day_reset_at - datetime.now(UTC)).total_seconds()
                if wait_seconds > 0:
                    logger.warning(
                        f"Day rate limit reached for tenant {tenant_id}. "
                        f"Waiting {wait_seconds:.1f}s until reset."
                    )
                    await asyncio.sleep(wait_seconds)
                    # Reset counter after waiting
                    self._reset_day_counter(state)

    async def record_request(self, tenant_id: str) -> None:
        """
        Record a successful request (thread-safe).

        Call this AFTER a successful API request to increment counters.

        Args:
            tenant_id: Xero tenant ID

        Raises:
            ValueError: If tenant_id is None or empty
        """
        if not tenant_id:
            raise ValueError("tenant_id cannot be None or empty")

        # Acquire tenant-specific lock for thread-safe counter increment
        lock = await self._get_or_create_lock(tenant_id)
        async with lock:
            state = self._get_or_create_state(tenant_id)
            state.minute_count += 1
            state.day_count += 1

            logger.debug(
                f"Recorded request for tenant {tenant_id}: "
                f"minute={state.minute_count}/{self.requests_per_minute}, "
                f"day={state.day_count}/{self.requests_per_day}"
            )

    def get_state(self, tenant_id: str) -> dict[str, any]:
        """
        Get current rate limit state for a tenant (for debugging/monitoring).

        Args:
            tenant_id: Xero tenant ID

        Returns:
            Dictionary with current state information
        """
        if tenant_id not in self._tenant_states:
            return {
                "tenant_id": tenant_id,
                "minute_count": 0,
                "minute_limit": self.requests_per_minute,
                "day_count": 0,
                "day_limit": self.requests_per_day,
                "status": "no_requests_yet",
            }

        state = self._tenant_states[tenant_id]
        now = datetime.now(UTC)

        return {
            "tenant_id": tenant_id,
            "minute_count": state.minute_count,
            "minute_limit": self.requests_per_minute,
            "minute_reset_in_seconds": max(0, (state.minute_reset_at - now).total_seconds()),
            "day_count": state.day_count,
            "day_limit": self.requests_per_day,
            "day_reset_in_seconds": max(0, (state.day_reset_at - now).total_seconds()),
            "status": "active",
        }
