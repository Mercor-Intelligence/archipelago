"""
Middleware to simulate rate-limited responses

This middleware mocks interactions with rate-limited data sources by
providing rate-limited responses to mimic real-world API behavior
"""

import asyncio
import time
from enum import Enum, auto

from fastmcp.exceptions import McpError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp.types import ErrorData


class Algorithm(Enum):
    """The rate limiting algorithm to use."""

    TokenBucket = auto()
    FixedWindow = auto()
    SlidingWindow = auto()
    SlidingLog = auto()
    LeakyBucket = auto()


class RateLimitMiddleware(Middleware):
    """
    Middleware that imposes rate limiting.

    Args:
        max_calls (int): Maximum number of requests (required)
        period_sec (float): Number of seconds during which max_calls are allowed (default 1.0)
        algorithm (unused -- always token bucket)
        enabled (bool): Whether middleware is active (default: True)
    """

    def __init__(
        self,
        max_calls: int,
        period_sec: float = 1.0,
        algorithm: Algorithm = Algorithm.TokenBucket,
        enabled: bool = True,
    ):
        """
        Initialize the RateLimitMiddleware.

        Raise:
            ValueError if parameters are invalid
            NotImplementedError if algorithm is not TokenBucket
        """
        if period_sec < 0:
            raise ValueError("period_sec must be >= 0")
        if max_calls < 0:
            raise ValueError("max_calls must be >= 0")
        if algorithm is not Algorithm.TokenBucket:
            raise NotImplementedError

        self.max_calls = max_calls
        self.period_sec = period_sec
        self.algorithm = algorithm
        self.enabled = enabled
        self.refill_bucket()
        self._lock = asyncio.Lock()

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        """
        Process the request with rate limiting

        Args:
            context: The middleware context containing request information
            call_next: Callable to invoke the next middleware or handler

        Return:
            The response from the downstream handler
        """
        if not self.enabled:
            return await call_next(context)

        async with self._lock:
            if time.time() > self.refill_time + self.period_sec:
                self.refill_bucket()

            if self.token_count <= 0:
                raise McpError(ErrorData(code=429, message="Rate limit exceeded"))

            self.token_count -= 1

        try:
            result = await call_next(context)
        except:
            async with self._lock:
                self.token_count += 1
            raise

        return result

    def refill_bucket(self):
        self.token_count = self.max_calls
        self.refill_time = time.time()

    def disable(self):
        """Disable the middleware (useful for testing)."""
        self.enabled = False

    def enable(self):
        """Enable the middleware."""
        self.refill_bucket()
        self.enabled = True
