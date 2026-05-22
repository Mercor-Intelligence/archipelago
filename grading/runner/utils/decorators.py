import asyncio
import functools
import random
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar

from loguru import logger

from runner.utils.metrics import distribution, increment

# Per-logical-call correlation id, stable across `with_retry` attempts of the
# same wrapped function call. Downstream code (e.g. `runner.utils.llm`) reads
# this to tag each LiteLLM request so Datadog can dedupe retries:
#   unique_count(@call_id where status:429) / unique_count(@call_id)
# gives the "% of logical grading-LLM calls that hit a 429" instead of the
# inflated per-attempt rate.
llm_call_id_ctx: ContextVar[str | None] = ContextVar("llm_call_id", default=None)
llm_attempt_ctx: ContextVar[int] = ContextVar("llm_attempt", default=1)


def with_retry(
    max_retries=3,
    base_backoff=1.5,
    jitter: float = 1.0,
    retry_on: tuple[type[Exception], ...] | None = None,
    skip_on: tuple[type[Exception], ...] | None = None,
    skip_if: Callable[[Exception], bool] | None = None,
):
    """
    This decorator is used to retry a function if it fails.
    It will retry the function up to the specified number of times, with a backoff between attempts.

    Args:
        max_retries: Maximum number of retry attempts
        base_backoff: Base backoff time in seconds
        jitter: Random jitter to add to backoff time
        retry_on: Tuple of exception types to retry on. If None, retries on all exceptions.
        skip_on: Tuple of exception types to never retry on, even if they match retry_on.
        skip_if: Predicate function that returns True if the exception should NOT be retried.
                 Useful for checking error messages (e.g., non-retriable BadRequestErrors).
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # One id for the whole logical call; reused across retry attempts
            # so each retry of the same call carries the same `call_id` tag.
            # If we're already inside an enclosing `with_retry` (nested
            # decorators), inherit its call_id to keep correlation intact.
            outer_call_id = llm_call_id_ctx.get()
            call_id = outer_call_id or uuid.uuid4().hex
            call_id_token = (
                llm_call_id_ctx.set(call_id) if outer_call_id is None else None
            )

            # Per-attempt + cumulative timing for the retry summary log. Used
            # by Datadog to sum "wall-time wasted on retries" across logical
            # grading-LLM calls. We track failed-attempt time and final-attempt
            # time separately so consumers can distinguish productive vs
            # wasted work.
            sequence_start = time.perf_counter()
            total_backoff_seconds = 0.0
            failed_attempt_seconds = 0.0
            final_attempt_seconds = 0.0
            final_status = "failure"
            final_error_class: str | None = None
            attempts_used = 0
            try:
                for attempt in range(1, max_retries + 1):
                    attempts_used = attempt
                    attempt_token = llm_attempt_ctx.set(attempt)
                    attempt_start = time.perf_counter()
                    try:
                        result = await func(*args, **kwargs)
                        final_attempt_seconds = time.perf_counter() - attempt_start
                        final_status = "success"
                        distribution(
                            "studio.grading.llm_attempt_seconds",
                            final_attempt_seconds,
                            tags=[
                                f"func:{func.__name__}",
                                "outcome:success",
                            ],
                        )
                        return result
                    except Exception as e:
                        attempt_duration = time.perf_counter() - attempt_start
                        # Tentatively classify this attempt as failed; if we
                        # decide below not to retry, promote it to "final".
                        failed_attempt_seconds += attempt_duration
                        final_error_class = type(e).__name__

                        # "skipped" = caller-handled exception (skip_on /
                        # skip_if / not in retry_on). Distinct from "failure"
                        # so dashboards don't inflate the failure rate with
                        # expected non-retriable cases (e.g. context-window).
                        is_skipped = (
                            (skip_on is not None and isinstance(e, skip_on))
                            or (skip_if is not None and skip_if(e))
                            or (retry_on is not None and not isinstance(e, retry_on))
                        )
                        distribution(
                            "studio.grading.llm_attempt_seconds",
                            attempt_duration,
                            tags=[
                                f"func:{func.__name__}",
                                f"outcome:{'skipped' if is_skipped else 'failure'}",
                                f"exc:{type(e).__name__}",
                            ],
                        )

                        if is_skipped:
                            failed_attempt_seconds -= attempt_duration
                            final_attempt_seconds = attempt_duration
                            raise

                        is_last_attempt = attempt >= max_retries
                        if is_last_attempt:
                            failed_attempt_seconds -= attempt_duration
                            final_attempt_seconds = attempt_duration
                            logger.error(
                                f"Error in {func.__name__}: {repr(e)}, after {max_retries} attempts (call_id={call_id})"
                            )
                            raise

                        backoff = base_backoff * (2 ** (attempt - 1))
                        jitter_delay = random.uniform(0, jitter) if jitter > 0 else 0
                        delay = backoff + jitter_delay
                        logger.warning(
                            f"Error in {func.__name__}: {repr(e)} (call_id={call_id}, attempt={attempt}/{max_retries})"
                        )
                        total_backoff_seconds += delay
                        await asyncio.sleep(delay)
                    finally:
                        llm_attempt_ctx.reset(attempt_token)
            finally:
                # Only the outermost retry wrapper emits the per-logical-call
                # summary; nested wrappers would double-count the same call.
                total_wall_seconds = time.perf_counter() - sequence_start
                if outer_call_id is None:
                    summary_tags = [
                        f"func:{func.__name__}",
                        f"status:{final_status}",
                    ]
                    if final_status != "success" and final_error_class is not None:
                        summary_tags.append(f"exc:{final_error_class}")
                    distribution(
                        "studio.grading.llm_total_seconds",
                        total_wall_seconds,
                        tags=summary_tags,
                    )
                    distribution(
                        "studio.grading.llm_attempts",
                        float(attempts_used),
                        tags=summary_tags,
                    )
                    if total_backoff_seconds > 0:
                        distribution(
                            "studio.grading.llm_backoff_seconds",
                            total_backoff_seconds,
                            tags=summary_tags,
                        )
                    if failed_attempt_seconds > 0:
                        distribution(
                            "studio.grading.llm_failed_attempt_seconds",
                            failed_attempt_seconds,
                            tags=summary_tags,
                        )
                    if attempts_used > 1:
                        increment(
                            "studio.grading.llm_retries",
                            value=attempts_used - 1,
                            tags=summary_tags,
                        )
                    if final_status != "success":
                        increment(
                            "studio.grading.llm_errors",
                            tags=summary_tags,
                        )

                # The structured retry-summary log keeps the same gate it had
                # before metrics existed: only log when something interesting
                # happened, since logs are the expensive ingestion path while
                # distributions aggregate server-side.
                should_emit = outer_call_id is None and (
                    attempts_used > 1 or final_status == "failure"
                )
                if should_emit:
                    logger.bind(
                        message_type="llm_retry_summary",
                        call_id=call_id,
                        func_name=func.__name__,
                        attempts=attempts_used,
                        retries_used=max(attempts_used - 1, 0),
                        total_backoff_seconds=round(total_backoff_seconds, 4),
                        failed_attempt_seconds=round(failed_attempt_seconds, 4),
                        final_attempt_seconds=round(final_attempt_seconds, 4),
                        total_wall_seconds=round(total_wall_seconds, 4),
                        final_status=final_status,
                        final_error_class=final_error_class,
                    ).info(
                        f"llm_retry_summary call_id={call_id} func={func.__name__} "
                        f"attempts={attempts_used} status={final_status} "
                        f"backoff_s={total_backoff_seconds:.2f} "
                        f"failed_attempt_s={failed_attempt_seconds:.2f} "
                        f"final_attempt_s={final_attempt_seconds:.2f}"
                    )
                if call_id_token is not None:
                    llm_call_id_ctx.reset(call_id_token)

        return wrapper

    return decorator


def with_concurrency_limit(max_concurrency: int):
    """
    This decorator is used to limit the concurrency of a function.
    It will limit concurrent calls to the function to the specified number within the same event loop.
    """

    _semaphores: dict[int, asyncio.Semaphore] = {}

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            loop = asyncio.get_running_loop()
            loop_id = id(loop)

            sem = _semaphores.get(loop_id)
            if sem is None:
                sem = asyncio.Semaphore(max_concurrency)
                _semaphores[loop_id] = sem

            async with sem:
                return await func(*args, **kwargs)

        return wrapper

    return decorator
