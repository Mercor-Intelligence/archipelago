"""Logging configuration for the environment."""

import asyncio
import sys

from loguru import logger

from .settings import Environment, get_settings

settings = get_settings()


def setup_logger() -> None:
    """Configure logging with optional Datadog sink."""
    logger.remove()

    if settings.DATADOG_LOGGING:
        # Datadog logger
        from .datadog_logger import datadog_sink  # import-check-ignore

        datadog_sink.start()
        logger.add(datadog_sink, level="DEBUG")

    if settings.ENV == Environment.LOCAL:
        # Local logger
        logger.add(
            sys.stdout,
            level="DEBUG",
            enqueue=True,
            backtrace=True,
            diagnose=True,
            colorize=True,
        )
    else:
        # Structured logger
        logger.add(
            sys.stdout,
            level="DEBUG",
            enqueue=True,
            backtrace=True,
            diagnose=True,
            serialize=True,
        )


async def teardown_logger() -> None:
    """Flush pending logs before shutdown."""
    await logger.complete()

    if settings.DATADOG_LOGGING:
        from .datadog_logger import api_client, datadog_sink  # import-check-ignore

        await asyncio.to_thread(datadog_sink.close)
        api_client.close()


def pending_datadog_logs() -> int | None:
    """Lines queued for Datadog and not yet sent; None when Datadog logging is off."""
    if not settings.DATADOG_LOGGING:
        return None
    from .datadog_logger import datadog_sink  # import-check-ignore

    return datadog_sink.pending()
