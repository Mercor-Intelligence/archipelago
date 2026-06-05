import sys

from loguru import logger

from runner.utils.settings import Environment, get_settings

settings = get_settings()


def setup_logger() -> None:
    logger.remove()

    if settings.DATADOG_LOGGING:
        # Datadog logger
        from .datadog_logger import datadog_sink  # import-check-ignore

        logger.debug("Adding Datadog logger")
        logger.add(datadog_sink, level="DEBUG", enqueue=True)

    if settings.ENV == Environment.LOCAL:
        logger.add(
            sys.stdout,
            level="DEBUG",
            enqueue=True,
            backtrace=True,
            diagnose=True,
            colorize=True,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        )
    else:
        logger.add(
            sys.stdout,
            level="DEBUG",
            enqueue=True,
            backtrace=True,
            diagnose=True,
            serialize=True,
        )
