import asyncio
import functools
import random

from loguru import logger


def with_retry(
    max_retries=3,
    base_backoff=1.5,
    jitter: float = 1.0,
    retry_on: tuple[type[Exception], ...] | None = None,
    skip_on: tuple[type[Exception], ...] | None = None,
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
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if skip_on is not None and isinstance(e, skip_on):
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


def with_concurrency_limit(max_concurrency: int):
    """
    This decorator is used to limit the concurrency of a function.
    It will limit concurrent calls to the function to the specified number within the same event loop.
    """

    _semaphores: dict[int, asyncio.Semaphore] = {}

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            loop = asyncio.get_running_loop()
            loop_id = id(loop)

            sem = _semaphores.get(loop_id)
            if sem is None:
                sem = asyncio.Semaphore(max_concurrency)
                _semaphores[loop_id] = sem

            async with sem:
                return await func(*args, **kwargs)

        return wrapper

    return decorator
