import asyncio
import functools
import random
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import asyncer
from loguru import logger
from utils.exceptions import NonRetryableError

_P = ParamSpec("_P")
_R = TypeVar("_R")


def make_async_background[**P, R](fn: Callable[P, R]) -> Callable[P, Awaitable[R]]:
    """
    Make a function run in the background (thread) and return an awaitable.
    """

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return await asyncer.asyncify(fn)(*args, **kwargs)

    return wrapper


_NO_RETRY_EXCEPTIONS: tuple[type[Exception], ...] = (NonRetryableError,)


def with_retry(max_retries=3, base_backoff=1.5, jitter: float = 1.0):
    """
    This decorator is used to retry a function if it fails.
    It will retry the function up to the specified number of times, with a backoff between attempts.

    Deterministic errors (e.g. ValueError from validation) are raised immediately
    without retrying since they will never succeed on subsequent attempts.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except _NO_RETRY_EXCEPTIONS:
                    raise
                except Exception as e:
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
