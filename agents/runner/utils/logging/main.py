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

    if settings.REDIS_LOGGING:
        # Redis logger
        from .redis_logger import redis_sink  # import-check-ignore

        logger.debug("Adding Redis logger")
        logger.add(redis_sink, level="INFO")

    if settings.FILE_LOGGING:
        # File logger
        from .file_logger import file_sink  # import-check-ignore

        logger.debug("Adding File logger")
        logger.add(file_sink, level="DEBUG")

    if settings.API_LOGGING:
        from .api_logger import api_sink  # import-check-ignore

        logger.debug("Adding API logger")
        logger.add(api_sink, level="INFO")

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
    await logger.complete()

    if settings.API_LOGGING:
        from .api_logger import teardown_api_logger  # import-check-ignore

        logger.debug("Tearing down API logger")
        await teardown_api_logger()

    if settings.FILE_LOGGING:
        from .file_logger import teardown_file_logger  # import-check-ignore

        logger.debug("Tearing down File logger")
        await teardown_file_logger()
