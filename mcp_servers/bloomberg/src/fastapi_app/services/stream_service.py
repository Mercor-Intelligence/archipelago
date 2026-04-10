"""Streaming service for generating random data."""

import asyncio
import random
from collections.abc import AsyncGenerator


async def generate_random_numbers(interval: float = 1.0) -> AsyncGenerator[float]:
    """Generate random numbers at specified interval.

    Args:
        interval: Time in seconds between numbers

    Yields:
        Random float between 0 and 100
    """
    while True:
        yield random.uniform(0, 100)
        await asyncio.sleep(interval)
