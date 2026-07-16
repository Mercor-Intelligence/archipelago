import asyncio
import functools
import json
import random
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

import asyncer
from loguru import logger
from pydantic import BaseModel

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


def with_retry(max_retries=3, base_backoff=1.5, jitter: float = 1.0):
    """
    This decorator is used to retry a function if it fails.
    It will retry the function up to the specified number of times, with a backoff between attempts.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
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


def truncate_response(data: Any, max_size_bytes: int = 32768) -> dict:
    """Truncate a response if it exceeds the max size.

    This is a safety net to prevent context window overflow.
    It serializes to JSON, checks size, and truncates if needed.

    Args:
        data: Response data (dict or Pydantic model)
        max_size_bytes: Maximum response size in bytes (default: 32KB)

    Returns:
        Original data (as dict) if under limit, or truncated version with metadata
    """
    # Convert to dict if Pydantic model
    if isinstance(data, BaseModel):
        data = data.model_dump()

    # Serialize to check size
    try:
        json_str = json.dumps(data, default=str)
    except (TypeError, ValueError) as e:
        logger.warning(f"Failed to serialize response: {e}")
        return {"error": "Response could not be serialized", "truncated": True}

    size_bytes = len(json_str.encode("utf-8"))

    if size_bytes <= max_size_bytes:
        return data

    # Response is too large - truncate intelligently
    logger.warning(f"Response size {size_bytes} bytes exceeds limit {max_size_bytes}, truncating")

    # Create truncation notice
    truncation_notice = {
        "_truncated": True,
        "_original_size_bytes": size_bytes,
        "_max_size_bytes": max_size_bytes,
        "_message": (
            f"Response was {size_bytes:,} bytes, exceeding {max_size_bytes:,} byte limit. "
            "Use filtering or pagination to get smaller responses."
        ),
    }

    # Try to preserve structure while reducing size
    if isinstance(data, dict):
        # Keep metadata, truncate large list fields
        truncated = dict(truncation_notice)
        for key, value in data.items():
            if key.startswith("_"):
                continue  # Skip internal keys
            if isinstance(value, list) and len(value) > 5:
                # Keep first 5 items of large lists
                truncated[key] = value[:5]
                truncated[f"_{key}_truncated_from"] = len(value)
            elif isinstance(value, dict):
                # Include dict keys but note it's summarized
                truncated[key] = {"_keys": list(value.keys())[:20]}
            else:
                truncated[key] = value
        return truncated

    return truncation_notice


def with_response_size_limit(max_size_bytes: int | None = None):
    """Decorator to enforce response size limits on tool functions.

    Args:
        max_size_bytes: Max response size in bytes. If None, uses config default.

    Usage:
        @with_response_size_limit(32768)
        async def my_tool(request: MyInput) -> MyOutput:
            ...
    """

    def decorator(func: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[dict]]:
        @functools.wraps(func)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> dict:
            result = await func(*args, **kwargs)

            # Get size limit from config if not specified
            limit = max_size_bytes
            if limit is None:
                try:
                    from config import MAX_RESPONSE_SIZE_BYTES

                    limit = MAX_RESPONSE_SIZE_BYTES
                except ImportError:
                    limit = 32768

            return truncate_response(result, limit)

        return wrapper

    return decorator
