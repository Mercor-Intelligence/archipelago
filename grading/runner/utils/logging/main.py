import sys

from loguru import logger

from runner.utils.grading_log import grading_db_filter
from runner.utils.settings import Environment, get_settings

settings = get_settings()

# Guards real sink (re)configuration to once per container, and counts
# in-flight trajectories so teardown only closes the shared API sink once the
# last concurrent trajectory finishes. Under Modal's @modal.concurrent (the
# `scoring` and `batch_code_data` lanes), several calls share one
# container/process, so logger.remove() or an early sink close on any single
# call would break sibling calls still in flight.
_logger_configured = False
_active_trajectory_count = 0


def setup_logger() -> None:
    global _logger_configured, _active_trajectory_count
    _active_trajectory_count += 1
    if _logger_configured:
        return
    _logger_configured = True

    logger.remove()

    if settings.DATADOG_LOGGING:
        from .datadog_logger import datadog_sink  # import-check-ignore

        logger.debug("Adding Datadog logger")
        logger.add(datadog_sink, level="DEBUG", enqueue=True)

    if settings.REDIS_LOGGING:
        from .redis_logger import redis_sink  # import-check-ignore

        logger.debug("Adding Redis grading_logs logger")
        logger.add(redis_sink, level="INFO", filter=grading_db_filter)

    if settings.API_LOGGING:
        from .api_logger import api_sink  # import-check-ignore

        logger.debug("Adding API grading_logs logger")
        logger.add(api_sink, level="INFO", filter=grading_db_filter)

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
    """Flush pending log messages, then close the API sink once every
    concurrent trajectory in this container has finished.

    Closing the sink early (while a sibling call under @modal.concurrent is
    still logging) would drop that sibling's remaining log lines.
    """
    global _active_trajectory_count

    await logger.complete()

    _active_trajectory_count -= 1
    if _active_trajectory_count > 0:
        return

    if settings.API_LOGGING:
        from .api_logger import teardown_api_logger  # import-check-ignore

        logger.debug("Tearing down API grading_logs logger")
        await teardown_api_logger()
