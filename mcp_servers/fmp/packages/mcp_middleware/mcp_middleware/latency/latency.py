"""
Latency Middleware for simulating variable response times and timeouts.

This middleware helps mock interactions with slow or unreliable data sources by:
1. Adding random delays to requests (simulating network latency)
2. Optionally timing out requests to test retry logic
3. Providing configurable latency ranges to mimic real-world API behavior
"""

import asyncio
import random

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from loguru import logger


class LatencyMiddleware(Middleware):
    """
    Middleware that simulates variable response times and occasional timeouts.

    This middleware helps test how agents handle:
    - Slow API responses with variable latency
    - Request timeouts requiring retry logic
    - Unpredictable response times typical of real-world APIs

    Important: When using with RetryMiddleware, RetryMiddleware MUST be added first.
    Middleware can only catch exceptions from what they wrap via call_next().
    Wrong order will cause retry logic to fail completely.
    See shared/middleware/latency/USAGE_EXAMPLE.md for details.

    Args:
        min_latency: Minimum delay in seconds (default: 0.1)
        max_latency: Maximum delay in seconds (default: 2.0)
        timeout_probability: Probability (0.0-1.0) of timing out a request (default: 0.0)
        timeout_duration: Duration to sleep before raising timeout error. This enforces a
            MINIMUM runtime for timed-out requests, not a maximum. Lower values (0.5-2.0s)
            recommended for faster test execution (default: 5.0)
        enabled: Whether the middleware is active (default: True)
        seed: Optional random seed for reproducible behavior (default: None)

    Example:
        ```python
        # Simulate moderate latency with occasional timeouts
        mcp.add_middleware(LatencyMiddleware(
            min_latency=0.5,
            max_latency=3.0,
            timeout_probability=0.1,  # 10% of requests timeout
            timeout_duration=2.0  # Each timeout sleeps 2s before raising error
        ))
        # Note: timeout_duration sets MINIMUM sleep time, not maximum.
        # High values will significantly slow down testing.

        # Simulate consistently slow responses without timeouts
        mcp.add_middleware(LatencyMiddleware(
            min_latency=2.0,
            max_latency=5.0
        ))

        # For reproducible testing
        mcp.add_middleware(LatencyMiddleware(
            min_latency=0.5,
            max_latency=2.0,
            seed=42  # Reproducible random behavior
        ))
        ```
    """

    def __init__(
        self,
        min_latency: float = 0.1,
        max_latency: float = 2.0,
        timeout_probability: float = 0.0,
        timeout_duration: float = 5.0,
        enabled: bool = True,
        seed: int | None = None,
    ):
        """
        Initialize the LatencyMiddleware.

        Raises:
            ValueError: If parameters are invalid
        """
        if min_latency < 0:
            raise ValueError(f"min_latency must be >= 0, got {min_latency}")
        if max_latency < min_latency:
            raise ValueError(f"max_latency ({max_latency}) must be >= min_latency ({min_latency})")
        if not 0.0 <= timeout_probability <= 1.0:
            raise ValueError(
                f"timeout_probability must be between 0.0 and 1.0, got {timeout_probability}"
            )
        if timeout_duration <= 0:
            raise ValueError(f"timeout_duration must be > 0, got {timeout_duration}")

        self.min_latency = min_latency
        self.max_latency = max_latency
        self.timeout_probability = timeout_probability
        self.timeout_duration = timeout_duration
        self.enabled = enabled

        # Create a Random instance for reproducible behavior
        self._random = random.Random(seed)

    async def on_request(self, context: MiddlewareContext, call_next: CallNext):
        """
        Process the request with simulated latency and potential timeout.

        Args:
            context: The middleware context containing request information
            call_next: Callable to invoke the next middleware or handler

        Returns:
            The response from the downstream handler

        Raises:
            TimeoutError: If the request is simulated to timeout
        """
        if not self.enabled:
            return await call_next(context)

        # Determine if this request should timeout
        will_timeout = self._random.random() < self.timeout_probability

        if will_timeout:
            # Simulate a timeout
            logger.warning(
                f"LatencyMiddleware: Simulating timeout for {context.method} "
                f"(timeout_duration={self.timeout_duration}s)"
            )
            await asyncio.sleep(self.timeout_duration)
            raise TimeoutError(
                f"Request timed out after {self.timeout_duration} seconds (simulated)"
            )

        # Add random latency
        latency = self._random.uniform(self.min_latency, self.max_latency)
        logger.debug(f"LatencyMiddleware: Adding {latency:.3f}s latency to {context.method}")

        # Wait for the latency period
        await asyncio.sleep(latency)

        # Continue to the next middleware/handler
        response = await call_next(context)

        logger.debug(f"LatencyMiddleware: {context.method} completed after {latency:.3f}s delay")

        return response

    def disable(self):
        """Disable the middleware (useful for testing)."""
        self.enabled = False

    def enable(self):
        """Enable the middleware."""
        self.enabled = True

    def set_latency_range(self, min_latency: float, max_latency: float):
        """
        Update the latency range.

        Args:
            min_latency: New minimum latency in seconds
            max_latency: New maximum latency in seconds

        Raises:
            ValueError: If parameters are invalid
        """
        if min_latency < 0:
            raise ValueError(f"min_latency must be >= 0, got {min_latency}")
        if max_latency < min_latency:
            raise ValueError(f"max_latency ({max_latency}) must be >= min_latency ({min_latency})")

        self.min_latency = min_latency
        self.max_latency = max_latency

    def set_timeout_probability(self, timeout_probability: float):
        """
        Update the timeout probability.

        Args:
            timeout_probability: New probability (0.0-1.0) of timing out

        Raises:
            ValueError: If probability is invalid
        """
        if not 0.0 <= timeout_probability <= 1.0:
            raise ValueError(
                f"timeout_probability must be between 0.0 and 1.0, got {timeout_probability}"
            )

        self.timeout_probability = timeout_probability
