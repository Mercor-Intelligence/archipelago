"""Logging configuration for the environment."""

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
