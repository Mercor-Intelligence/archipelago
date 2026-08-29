import asyncio
import sys

from loguru import logger

from runner.utils.cas_ledger_emit import flush_cas_ledger
from runner.utils.logging.extra_filter import stamp_trajectory_log_id
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

    # Mint each line's trajectory_log_id once, at capture, so the Postgres sink and the
    # S3 sink write the SAME id for the same line (RLS-9809). Installed before any sink
    # is added, and process-wide: the core patcher runs ahead of any per-logger
    # logger.patch(), so the agents that patch `time` compose with this rather than
    # replacing it.
    logger.configure(patcher=stamp_trajectory_log_id)

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

    if settings.S3_LOGGING:
        # S3 dual-write (RLS-9434): a first-class durable sink, peer of api_sink, with the
        # same ephemeral filter. Gated on its OWN toggle (not API_LOGGING) so S3 keeps
        # working once PG logging is turned off. No-op unless the server also minted a
        # policy for the trajectory (s3_log_writer.is_enabled); register()/finalize() are
        # gated on the same S3_LOGGING toggle (modal_labs) so no empty manifest is sealed.
        from .s3_log_writer import s3_log_sink  # import-check-ignore

        logger.add(
            s3_log_sink,
            level="INFO",
            filter=lambda record: not record["extra"].get("ephemeral"),
        )

    # Capture the last `message_type='final_answer'` emission per trajectory so
    # the runner can denormalize it onto the trajectory row at completion
    # (RLS-9433). In-memory and cheap; always on, and independent of the log
    # pipeline (the completion webhook, not a log sink, is what persists it).
    from .final_answer import final_answer_sink  # import-check-ignore

    logger.add(final_answer_sink, level="INFO")

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
        # Compact structured stdout for Modal containers. The previous
        # serialize=True sink shipped full record extras (model responses,
        # tool payloads) and blew Modal's per-container output rate limit,
        # dropping lines from the viewer and the modal.function-logs drain.
        # Full fidelity still flows via the Datadog/Redis/API sinks above.
        # backtrace keeps loguru's extended tracebacks (frames above the
        # catch point); format="" makes the handler's rendered text carry
        # ONLY that formatted exception, which the sink consumes. diagnose
        # stays off on purpose: it renders local variable values into
        # tracebacks, which here can include prompts and keys.
        from .stdout_logger import truncated_stdout_sink  # import-check-ignore

        logger.add(
            truncated_stdout_sink,
            level="DEBUG",
            enqueue=True,
            backtrace=True,
            diagnose=False,
            format="",
        )


async def teardown_logger(trajectory_id: str | None = None) -> None:
    """Flush pending log messages, then close the API/file sinks once every
    concurrent trajectory in this container has finished.

    Closing the sinks early (while a sibling call under @modal.concurrent is
    still logging) would drop that sibling's remaining log lines. Only
    logger.complete() runs on every call; the actual sink teardown — which
    drains the API log queue's background HTTP worker and closes the file
    handle — waits for the last in-flight trajectory so pending logs are
    still shipped before a single-use container is destroyed.

    Args:
        trajectory_id: when given, finalizes this trajectory's per-prefix work
            now that it's done. Both steps below are per-trajectory and run on
            *every* call — before the shared ref-count teardown — not once per
            container, and ``logger.complete()`` above guarantees this
            trajectory's records have already reached the sinks:
            - S3 log parts (RLS-9434): flush the remaining buffer + seal the
              manifest (records reached api_sink → the S3 writer's buffer).
              No-op when S3 writing wasn't enabled (no policy minted).
            - Redis live-tail: collapse that trajectory's stream to a short TTL
              (full logs are already durable via the API sink). Each trajectory
              owns its own stream key regardless of how many share this container.

    The CAS ledger drains first, on *every* call rather than once per container.
    This is the only seam every exit path shares -- the CLI reaches it through
    ``run_and_persist``, and modal_labs / k8s_worker / harbor call it directly --
    so anywhere else would leave production draining nothing. Draining per
    trajectory rather than behind the ref-count is deliberate: a sibling still
    running is no reason to sit on this trajectory's events while the container
    waits to be reaped. Ordering matters too, since the drain reports dropped
    events by logging, and ``logger.complete()`` below is what ships that line.
    """
    global _active_trajectory_count

    try:
        await asyncio.to_thread(flush_cas_ledger)
    except Exception as e:
        logger.warning(f"CAS ledger flush failed, ignoring: {e!r}")

    await logger.complete()

    if trajectory_id:
        from .s3_log_writer import finalize as finalize_s3_logs  # import-check-ignore

        await finalize_s3_logs(trajectory_id)

        from .redis_logger import expire_trajectory_stream  # import-check-ignore

        await expire_trajectory_stream(trajectory_id)

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
