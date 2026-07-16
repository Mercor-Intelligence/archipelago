import sys

from loguru import logger

from runner.utils.settings import Environment, get_settings

settings = get_settings()

# Guards real sink (re)configuration to once per container, and counts
# in-flight trajectories so teardown only closes the shared API/file sinks
# once the last concurrent trajectory finishes. Under Modal's
# @modal.concurrent, several trajectories share one container/process, so
# logger.remove() or an early sink close on any single call would break
# sibling calls still in flight.
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
        # Skip records bound with ephemeral=True. Those are live telemetry an
        # agent already streams to Redis for the running view and re-emits in
        # full — in causal order with real per-event timestamps — into the
        # durable trajectory_logs afterwards. Persisting both produced a
        # duplicated, timestamp-misordered transcript. Only the durable sink
        # filters; the Redis live stream still carries them.
        logger.add(
            api_sink,
            level="INFO",
            filter=lambda record: not record["extra"].get("ephemeral"),
        )

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
    """Flush pending log messages, then close the API/file sinks once every
    concurrent trajectory in this container has finished.

    Closing the sinks early (while a sibling call under @modal.concurrent is
    still logging) would drop that sibling's remaining log lines. Only
    logger.complete() runs on every call; the actual sink teardown — which
    drains the API log queue's background HTTP worker and closes the file
    handle — waits for the last in-flight trajectory so pending logs are
    still shipped before a single-use container is destroyed.
    """
    global _active_trajectory_count

    await logger.complete()

    _active_trajectory_count -= 1
    if _active_trajectory_count > 0:
        return

    if settings.API_LOGGING:
        from .api_logger import teardown_api_logger  # import-check-ignore

        logger.debug("Tearing down API logger")
        await teardown_api_logger()

    if settings.FILE_LOGGING:
        from .file_logger import teardown_file_logger  # import-check-ignore

        logger.debug("Tearing down File logger")
        await teardown_file_logger()
