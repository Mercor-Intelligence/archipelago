import sys

from loguru import logger

from runner.utils.grading_log import grading_db_filter
from runner.utils.settings import Environment, get_settings

settings = get_settings()


def setup_logger() -> None:
    logger.remove()

    if settings.DATADOG_LOGGING:
        from .datadog_logger import datadog_sink  # import-check-ignore

        logger.debug("Adding Datadog logger")
        logger.add(datadog_sink, level="DEBUG", enqueue=True)

    if settings.REDIS_LOGGING:
        from .redis_logger import redis_sink  # import-check-ignore

        logger.debug("Adding Redis grading_logs logger")
        logger.add(redis_sink, level="INFO", filter=grading_db_filter)

    if settings.POSTGRES_LOGGING:
        from .postgres_logger import postgres_sink  # import-check-ignore

        logger.debug("Adding Postgres grading_logs logger")
        logger.add(postgres_sink, level="INFO", filter=grading_db_filter)

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


async def teardown_logger() -> None:
    await logger.complete()

    if settings.POSTGRES_LOGGING:
        from .postgres_logger import teardown_postgres_logger  # import-check-ignore

        logger.debug("Tearing down Postgres grading_logs logger")
        await teardown_postgres_logger()
