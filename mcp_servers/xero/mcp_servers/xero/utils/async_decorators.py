"""
Async utility decorators for Xero MCP server.

This module provides three key decorators for async operations:
- make_async_background: Convert blocking functions to async by running in thread pool
- with_retry: Add exponential backoff retry logic to async functions
- with_concurrency_limit: Control concurrent execution of async operations

These utilities improve resilience, performance, and resource management across
both offline and online providers.
"""

import asyncio
import functools
import random
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

T = TypeVar("T")

# Shared thread pool executor for make_async_background
_executor = ThreadPoolExecutor(max_workers=10)


def make_async_background[T](func: Callable[..., T]) -> Callable[..., Awaitable[T]]:
    """
    Decorator to run blocking function in thread pool executor.

    Converts a synchronous blocking function into an async function by executing
    it in a thread pool. This is useful for I/O operations that would otherwise
    block the event loop.

    Useful for:
    - File I/O operations (JSON loading, file reads)
    - Synchronous HTTP calls
    - CPU-bound operations
    - Blocking library calls

    Args:
        func: Synchronous function to wrap

    Returns:
        Async function that executes in thread pool

    Example:
        >>> @make_async_background
        ... def load_large_json(path: str) -> dict:
        ...     with open(path) as f:
        ...         return json.load(f)
        ...
        >>> # Can now be awaited
        >>> data = await load_large_json("data.json")

    Example with class method:
        >>> class DataLoader:
        ...     @make_async_background
        ...     def _load_json_sync(self, path: str) -> dict:
        ...         with open(path) as f:
        ...             return json.load(f)
        ...
        ...     async def load_data(self):
        ...         self.data = await self._load_json_sync("data.json")

    Note:
        The wrapped function must be thread-safe. It will run in a thread pool
        with up to 10 worker threads by default.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, lambda: func(*args, **kwargs))

    return wrapper


def with_retry(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
    logger: Any | None = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """
    Decorator to add retry logic with exponential backoff to async functions.

    Automatically retries failed operations with exponentially increasing delays.
    Uses jitter to prevent thundering herd problems when many clients retry
    simultaneously.

    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        initial_delay: Initial delay in seconds (default: 1.0)
        max_delay: Maximum delay between retries in seconds (default: 60.0)
        exponential_base: Base for exponential backoff (default: 2.0)
        exceptions: Tuple of exception types to catch and retry (default: all)
        logger: Logger instance for logging retries (optional)

    Returns:
        Decorated async function with retry logic

    Example:
        >>> @with_retry(max_retries=3, initial_delay=1.0, exponential_base=2.0)
        ... async def fetch_from_api(url: str) -> dict:
        ...     response = await http_client.get(url)
        ...     return response.json()
        ...
        >>> # Will retry up to 3 times with delays: 1s, 2s, 4s
        >>> data = await fetch_from_api("https://api.xero.com/...")

    Example with specific exceptions:
        >>> import httpx
        >>> @with_retry(
        ...     max_retries=5,
        ...     initial_delay=1.0,
        ...     exponential_base=2.0,
        ...     exceptions=(httpx.HTTPError, httpx.TimeoutException)
        ... )
        ... async def fetch_invoice(invoice_id: str) -> dict:
        ...     async with httpx.AsyncClient() as client:
        ...         response = await client.get(f"https://api.xero.com/invoices/{invoice_id}")
        ...         response.raise_for_status()
        ...         return response.json()

    Example with logging:
        >>> from loguru import logger
        >>> @with_retry(max_retries=3, logger=logger)
        ... async def api_call():
        ...     # API call that may fail transiently
        ...     pass

    Backoff Formula:
        delay = min(initial_delay * (exponential_base ** attempt), max_delay)
        actual_delay = delay * (0.5 + random() * 0.5)  # Add jitter

    Note:
        - Jitter prevents thundering herd problem
        - Original exception is preserved on final failure
        - Retry attempts are logged if logger is provided
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_retries:
                        # Final failure - re-raise original exception
                        raise

                    # Calculate delay with exponential backoff and jitter
                    delay = min(initial_delay * (exponential_base**attempt), max_delay)
                    jitter = delay * (0.5 + random.random() * 0.5)

                    if logger:
                        logger.warning(
                            f"Retry {attempt + 1}/{max_retries} for {func.__name__} "
                            f"after {jitter:.2f}s due to {type(e).__name__}: {e}"
                        )

                    await asyncio.sleep(jitter)

            # Should never reach here, but satisfy type checker
            raise RuntimeError("Unexpected code path in with_retry")

        return wrapper

    return decorator


def with_concurrency_limit(
    limit: int = 10, semaphore: asyncio.Semaphore | None = None
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """
    Decorator to limit concurrent execution of async function.

    Uses asyncio.Semaphore to control how many instances of the decorated
    function can run concurrently. This prevents resource exhaustion and
    helps maintain system stability.

    Args:
        limit: Maximum number of concurrent executions (default: 10)
        semaphore: Optional existing semaphore to use (creates new if None)

    Returns:
        Decorated async function with concurrency limit

    Example:
        >>> @with_concurrency_limit(limit=5)
        ... async def process_invoice(invoice_id: str) -> dict:
        ...     data = await fetch_invoice(invoice_id)
        ...     return process(data)
        ...
        >>> # Only 5 invocations can run concurrently
        >>> results = await asyncio.gather(*[
        ...     process_invoice(id) for id in invoice_ids
        ... ])

    Example with shared semaphore:
        >>> shared_sem = asyncio.Semaphore(10)
        ...
        >>> @with_concurrency_limit(semaphore=shared_sem)
        ... async def func1():
        ...     pass
        ...
        >>> @with_concurrency_limit(semaphore=shared_sem)
        ... async def func2():
        ...     pass
        ...
        >>> # Total limit is 10 across both functions

    Example with batch processing:
        >>> @with_concurrency_limit(limit=20)
        ... async def process_item(item: dict) -> dict:
        ...     # Heavy processing
        ...     return result
        ...
        >>> async def process_batch(items: list[dict]):
        ...     # Only 20 items processed concurrently
        ...     return await asyncio.gather(*[process_item(i) for i in items])

    Note:
        - Semaphore is automatically released even if function raises exception
        - Using shared semaphore allows coordination across multiple functions
        - Default limit (10) is conservative; tune based on system resources
        - Semaphore is created lazily on first use to avoid event loop binding issues
    """
    # Store the provided semaphore or None (will create lazily)
    _provided_semaphore = semaphore
    _lazy_semaphore: asyncio.Semaphore | None = None

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            nonlocal _lazy_semaphore

            # Get or create semaphore within async context
            if _provided_semaphore is not None:
                sem = _provided_semaphore
            else:
                # Create semaphore lazily on first use to ensure it's bound to correct event loop
                if _lazy_semaphore is None:
                    _lazy_semaphore = asyncio.Semaphore(limit)
                sem = _lazy_semaphore

            async with sem:
                return await func(*args, **kwargs)

        return wrapper

    return decorator
