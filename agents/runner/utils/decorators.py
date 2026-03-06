"""
Utility decorators for the agent runner.
"""

import asyncio
import functools
import random
from collections.abc import Callable

from loguru import logger


def with_retry(
    max_retries=3,
    base_backoff=1.5,
    jitter: float = 1.0,
    retry_on: tuple[type[Exception], ...] | None = None,
    skip_on: tuple[type[Exception], ...] | None = None,
    skip_if: Callable[[Exception], bool] | None = None,
):
    """
    This decorator is used to retry a function if it fails.
    It will retry the function up to the specified number of times, with a backoff between attempts.

    Args:
        max_retries: Maximum number of retry attempts
        base_backoff: Base backoff time in seconds
        jitter: Random jitter to add to backoff time
        retry_on: Tuple of exception types to retry on. If None, retries on all exceptions.
        skip_on: Tuple of exception types to never retry on, even if they match retry_on.
        skip_if: Predicate function that returns True if the exception should NOT be retried.
                 Useful for checking error messages (e.g., context window errors).
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    # Check type-based skip
                    if skip_on is not None and isinstance(e, skip_on):
                        raise

                    # Check predicate-based skip (for content-based detection)
                    if skip_if is not None and skip_if(e):
                        raise

                    # If retry_on is specified, only retry on those exception types
                    if retry_on is not None and not isinstance(e, retry_on):
                        raise

                    is_last_attempt = attempt >= max_retries
                    if is_last_attempt:
                        logger.error(
                            f"Error in {func.__name__}: {repr(e)}, after {max_retries} attempts"
                        )
                        raise

                    backoff = base_backoff * (2 ** (attempt - 1))
                    jitter_delay = random.uniform(0, jitter) if jitter > 0 else 0
                    delay = backoff + jitter_delay
                    logger.warning(f"Error in {func.__name__}: {repr(e)}")
                    await asyncio.sleep(delay)

        return wrapper

    return decorator
