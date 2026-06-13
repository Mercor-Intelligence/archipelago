"""Populate subsystems with data from S3-compatible storage.

This module handles downloading objects from S3 (either single objects or
prefixes containing multiple objects) and placing them into subsystem
directories. Supports overwrite semantics where later sources overwrite
earlier ones with the same destination path.

Also supports post-populate hooks that run shell commands after data extraction.
"""

import asyncio
import contextlib
import os

from fastapi import HTTPException
from loguru import logger

from runner.utils.settings import get_settings

from .models import LifecycleHook, PopulateRequest, PopulateResult
from .utils import populate_data

settings = get_settings()


_STREAM_LIMIT = 1024 * 1024  # 1 MiB — well above any realistic log line

# Backstop for a hook whose process never exits.
_HOOK_TIMEOUT_SECONDS = float(os.environ.get("LIFECYCLE_HOOK_TIMEOUT_SECONDS", "1500"))
# After the process exits, how long to let the drains flush before cancelling.
_STREAM_DRAIN_GRACE_SECONDS = 5.0
_PROCESS_POLL_SECONDS = 0.1


async def _wait_process_exited(proc: asyncio.subprocess.Process) -> None:
    """Block until the process is reaped, by polling ``proc.returncode``.

    ``proc.wait()`` won't do: asyncio resolves it only once the process exits
    *and* all pipe transports are lost, so a backgrounded daemon that inherited
    the hook's pipes keeps it blocked forever. ``returncode`` is set the moment
    the process is reaped, independent of the pipes.
    """
    while proc.returncode is None:
        await asyncio.sleep(_PROCESS_POLL_SECONDS)


async def _stream_lines(
    stream: asyncio.StreamReader | None,
    hook_name: str,
    label: str,
    collected: list[str] | None = None,
) -> None:
    """Read lines from a subprocess stream and log them as they arrive."""
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            break
        text = line.decode(errors="replace").rstrip("\n")
        if collected is not None:
            collected.append(text)
        logger.info(f"[{hook_name}] {label}: {text}")


async def run_lifecycle_hook(hook: LifecycleHook) -> None:
    """Run a lifecycle hook command.

    Executes a shell command with optional environment variables.
    Secrets are already resolved by the agent before being sent to the environment.

    Stdout/stderr are streamed to the logger line-by-line. Completion is gated
    on the process exiting, not pipe EOF: a hook that backgrounds a long-lived
    daemon (e.g. Fineract's `nohup java …` server) leaves a child holding the
    pipe open, which would otherwise hang the hook until the populate timeout.

    Args:
        hook: The lifecycle hook to execute

    Raises:
        RuntimeError: If the command fails (non-zero exit code) or does not
            finish within ``_HOOK_TIMEOUT_SECONDS``.
    """
    logger.info(f"Running lifecycle hook for service '{hook.name}'")
    logger.debug(f"Hook command: {hook.command}")

    # Build environment: start with container env, add hook-specific vars
    run_env = dict(os.environ)
    # Hooks do not need direct access to the runner's Modal OIDC token.
    run_env.pop("MODAL_IDENTITY_TOKEN", None)
    if hook.env:
        run_env.update(hook.env)

    proc = await asyncio.create_subprocess_shell(
        hook.command,
        env=run_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_STREAM_LIMIT,
    )

    # Drain in the background so output is live and the pipe buffer can't fill
    # (which would deadlock the process). stderr is kept for the error message.
    stderr_lines: list[str] = []
    stdout_task = asyncio.create_task(_stream_lines(proc.stdout, hook.name, "stdout"))
    stderr_task = asyncio.create_task(
        _stream_lines(proc.stderr, hook.name, "stderr", stderr_lines)
    )

    timed_out = False
    try:
        await asyncio.wait_for(
            _wait_process_exited(proc), timeout=_HOOK_TIMEOUT_SECONDS
        )
    except TimeoutError:
        # May have exited between the last poll and the timeout firing; only a
        # real timeout if still running.
        if proc.returncode is None:
            timed_out = True
            logger.error(
                f"Lifecycle hook '{hook.name}' did not finish within "
                f"{_HOOK_TIMEOUT_SECONDS:.0f}s; killing it"
            )
            # suppress: the loop may have just reaped the child.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            try:
                await asyncio.wait_for(_wait_process_exited(proc), timeout=10)
            except TimeoutError:
                logger.error(f"Lifecycle hook '{hook.name}' did not die after kill")

    # Process is gone; let the drains flush, then cancel any still blocked on a
    # pipe a detached daemon is holding open.
    _, pending = await asyncio.wait(
        {stdout_task, stderr_task}, timeout=_STREAM_DRAIN_GRACE_SECONDS
    )
    for task in pending:
        task.cancel()
    drain_results = await asyncio.gather(
        stdout_task, stderr_task, return_exceptions=True
    )
    # Warn (don't swallow) on real drain errors, e.g. LimitOverrunError; log
    # capture only, so it doesn't change the hook's pass/fail.
    for result in drain_results:
        if isinstance(result, BaseException) and not isinstance(
            result, asyncio.CancelledError
        ):
            logger.warning(
                f"Lifecycle hook '{hook.name}' log drain errored: {result!r}"
            )

    if timed_out:
        raise RuntimeError(
            f"Lifecycle hook '{hook.name}' timed out after {_HOOK_TIMEOUT_SECONDS:.0f}s"
        )

    if proc.returncode != 0:
        error_msg = "\n".join(stderr_lines) if stderr_lines else "No error output"
        logger.error(
            f"Lifecycle hook '{hook.name}' failed with exit code {proc.returncode}: {error_msg}"
        )
        raise RuntimeError(
            f"Lifecycle hook '{hook.name}' failed with exit code {proc.returncode}: {error_msg}"
        )

    logger.info(f"Lifecycle hook '{hook.name}' completed successfully")


async def run_lifecycle_hooks(hooks: list[LifecycleHook]) -> None:
    """Run multiple lifecycle hooks in parallel.

    Uses asyncio.gather with return_exceptions=True so all hooks run to
    completion even if one fails (avoids leaving a service half-populated).

    Args:
        hooks: The lifecycle hooks to execute concurrently

    Raises:
        RuntimeError: If one hook fails, re-raises the original exception.
            If multiple hooks fail, raises a combined RuntimeError.
    """
    if not hooks:
        return
    if len(hooks) == 1:
        await run_lifecycle_hook(hooks[0])
        return

    results = await asyncio.gather(
        *[run_lifecycle_hook(h) for h in hooks],
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    if len(failures) == 1:
        raise failures[0]
    if failures:
        msgs = [f"  - {type(f).__name__}: {f}" for f in failures]
        raise RuntimeError(
            f"{len(failures)} lifecycle hooks failed:\n" + "\n".join(msgs)
        )


async def handle_populate(request: PopulateRequest) -> PopulateResult:
    """Handle populate endpoint request.

    Entry point for the /data/populate endpoint. Validates settings,
    processes the request, runs post-populate hooks, and returns results.

    Args:
        request: PopulateRequest containing list of S3 sources to download
            and optional post-populate hooks

    Returns:
        PopulateResult with total number of objects added

    Raises:
        HTTPException: If populate operation fails or S3 configuration is invalid
    """
    logger.debug(f"Processing populate request with {len(request.sources)} source(s)")
    logger.debug(f"Using explicit S3 credentials: {request.s3_credentials is not None}")

    try:
        # 1. Extract data from S3
        result = await populate_data(
            sources=request.sources,
            s3_credentials=request.s3_credentials,
        )

        logger.info(
            f"Populated {result.objects_added} object(s) from {len(request.sources)} source(s)"
        )

        # 2. Run post-populate hooks (in parallel — services have isolated state)
        if request.post_populate_hooks:
            logger.info(
                f"Running {len(request.post_populate_hooks)} post-populate hook(s)"
            )
            await run_lifecycle_hooks(request.post_populate_hooks)
            logger.info("All post-populate hooks completed")

        return result
    except HTTPException:
        raise
    except RuntimeError as e:
        # Hook failure
        logger.error(f"Post-populate hook failed: {repr(e)}")
        raise HTTPException(
            status_code=500,
            detail=str(e),
        ) from e
    except Exception as e:
        source_count = len(request.sources)
        logger.error(f"Error populating data from {source_count} source(s): {repr(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to populate {source_count} source(s): {str(e)}",
        ) from e
