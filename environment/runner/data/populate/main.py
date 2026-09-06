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
import time
from pathlib import Path

from fastapi import HTTPException
from loguru import logger

from runner.utils.metrics import distribution, safe_tag_value
from runner.utils.settings import get_settings

from .file_subtraction import resolve_markers
from .models import (
    HookTiming,
    LifecycleHook,
    PopulateRequest,
    PopulateResult,
    PopulateSource,
)
from .streaming import get_subsystem_paths
from .utils import parse_s3_url, populate_data

settings = get_settings()


_STREAM_LIMIT = 1024 * 1024  # 1 MiB — well above any realistic log line

# Backstop for a hook whose process never exits.
_HOOK_TIMEOUT_SECONDS = float(os.environ.get("LIFECYCLE_HOOK_TIMEOUT_SECONDS", "3000"))
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


def _has_authored_task_layer(sources: list[PopulateSource]) -> bool:
    """True when a source is a ``tasks/`` snapshot — an authored task overlay.

    Subtraction markers are authored into a task snapshot. Everything else a
    populate can carry is either the world baseline or, for a continuation, the
    parent trajectory's captured end state, and neither is a place a marker may
    be honored from: the trajectory snapshot is whatever the agent left behind.
    """
    for source in sources:
        try:
            _, key = parse_s3_url(source.url)
        except ValueError:
            continue
        if key.startswith("tasks/"):
            return True
    return False


def _emit_hook_duration(
    hook: LifecycleHook, phase: str, start: float, status: str
) -> None:
    """One hook's wall clock, tagged by outcome.

    Emitted on the failing paths too: `_HOOK_TIMEOUT_SECONDS` defaults to 3000s,
    so a hook that times out is by definition the one that burned the most clock
    and is exactly the one worth seeing. Answers "which hook is expensive" — NOT
    "how much did hooks add to populate", which is
    `lifecycle_hooks_phase_seconds`, since hooks run concurrently and these
    durations overlap.
    """
    distribution(
        "studio.trajectory.lifecycle_hook_seconds",
        time.perf_counter() - start,
        tags=[
            f"hook:{safe_tag_value(hook.name)}",
            f"phase:{safe_tag_value(phase)}",
            f"status:{status}",
        ],
    )


async def run_lifecycle_hook(hook: LifecycleHook, phase: str = "unspecified") -> float:
    """Run a lifecycle hook command.

    Executes a shell command with optional environment variables.
    Secrets are already resolved by the agent before being sent to the environment.

    Stdout/stderr are streamed to the logger line-by-line. Completion is gated
    on the process exiting, not pipe EOF: a hook that backgrounds a long-lived
    daemon (e.g. Fineract's `nohup java …` server) leaves a child holding the
    pipe open, which would otherwise hang the hook until the populate timeout.

    Args:
        hook: The lifecycle hook to execute

    Returns:
        Duration in seconds the hook took to execute.

    Raises:
        RuntimeError: If the command fails (non-zero exit code) or does not
            finish within the hook's ``timeout_seconds`` (default
            ``_HOOK_TIMEOUT_SECONDS``).
    """
    start = time.perf_counter()
    logger.info(f"Running lifecycle hook for service '{hook.name}'")
    logger.debug(f"Hook command: {hook.command}")

    # Build environment: start with container env, add hook-specific vars
    run_env = dict(os.environ)
    # Hooks do not need direct access to the runner's Modal OIDC token.
    run_env.pop("MODAL_IDENTITY_TOKEN", None)
    if hook.env:
        run_env.update(hook.env)

    timeout_seconds = (
        hook.timeout_seconds
        if hook.timeout_seconds is not None
        else _HOOK_TIMEOUT_SECONDS
    )

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
        await asyncio.wait_for(_wait_process_exited(proc), timeout=timeout_seconds)
    except TimeoutError:
        # May have exited between the last poll and the timeout firing; only a
        # real timeout if still running.
        if proc.returncode is None:
            timed_out = True
            logger.error(
                f"Lifecycle hook '{hook.name}' did not finish within "
                f"{timeout_seconds:.0f}s; killing it"
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
        _emit_hook_duration(hook, phase, start, "timeout")
        raise RuntimeError(
            f"Lifecycle hook '{hook.name}' timed out after {timeout_seconds:.0f}s"
        )

    if proc.returncode != 0:
        error_msg = "\n".join(stderr_lines) if stderr_lines else "No error output"
        logger.error(
            f"Lifecycle hook '{hook.name}' failed with exit code {proc.returncode}: {error_msg}"
        )
        _emit_hook_duration(hook, phase, start, "failed")
        raise RuntimeError(
            f"Lifecycle hook '{hook.name}' failed with exit code {proc.returncode}: {error_msg}"
        )

    duration = time.perf_counter() - start
    logger.info(
        f"Lifecycle hook '{hook.name}' completed successfully in {duration:.1f}s"
    )
    _emit_hook_duration(hook, phase, start, "ok")
    return duration


async def run_lifecycle_hooks(
    hooks: list[LifecycleHook], phase: str = "unspecified"
) -> list[HookTiming]:
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
        return []
    # Wall clock for the PHASE, which is not the sum of the per-hook durations:
    # these run concurrently, so two 100s hooks cost the populate ~100s, not
    # 200s. Subtracting summed hook time from the populate total would
    # over-attribute and can drive the residual negative. `finally`, because a
    # failing hook still consumed the clock.
    phase_start = time.perf_counter()
    try:
        if len(hooks) == 1:
            duration = await run_lifecycle_hook(hooks[0], phase)
            return [HookTiming(name=hooks[0].name, duration_s=duration)]

        results = await asyncio.gather(
            *[run_lifecycle_hook(h, phase) for h in hooks],
            return_exceptions=True,
        )
        timings: list[HookTiming] = []
        failures = []
        for hook, r in zip(hooks, results, strict=True):
            if isinstance(r, BaseException):
                failures.append(r)
            else:
                timings.append(HookTiming(name=hook.name, duration_s=r))
        if len(failures) == 1:
            raise failures[0]
        if failures:
            msgs = [f"  - {type(f).__name__}: {f}" for f in failures]
            raise RuntimeError(
                f"{len(failures)} lifecycle hooks failed:\n" + "\n".join(msgs)
            )
        return timings
    finally:
        distribution(
            "studio.trajectory.lifecycle_hooks_phase_seconds",
            time.perf_counter() - phase_start,
            tags=[f"phase:{safe_tag_value(phase)}"],
        )


async def _timed_marker_resolution(root: str, paths: list[Path]) -> None:
    """Resolve one root's subtraction markers, and record how long it took.

    The only populate phase with no timing at all: a tree walk with unlinks, run
    off the event loop. Untimed it is indistinguishable from extraction inside
    the ~405s of a ~492s populate that no metric accounts for.
    """
    start = time.perf_counter()
    await asyncio.to_thread(resolve_markers, paths)
    elapsed = time.perf_counter() - start
    logger.info(f"marker resolution [{root}] took {elapsed:.1f}s")
    distribution(
        "studio.trajectory.populate_marker_resolution_seconds",
        elapsed,
        tags=[f"root:{safe_tag_value(root)}"],
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
            backend=request.s3_transfer_backend,
        )

        logger.info(
            f"Populated {result.objects_added} object(s) from {len(request.sources)} source(s)"
        )

        # 2. Resolve subtraction markers. Part of materializing the filesystem,
        # not a hook: the overlay leaves each marker beside the path it removes,
        # and the environment is not finished until that pair is resolved. Runs
        # BEFORE the hooks so a hook observes a completed tree — and a failure
        # here fails the populate rather than being swallowed, which is the whole
        # difference between a step and the best-effort hook it replaces.
        #
        # ONLY when an authored `tasks/` layer is among the sources. A
        # continuation populates its PARENT TRAJECTORY's snapshot instead, which
        # is agent output rather than authored intent — so a `.rls_removed` the
        # agent happened to create must not start deleting the world out from
        # under the next turn. The source URLs are the only place that
        # distinction survives into the sandbox, which is why it is read here
        # rather than inferred from the tree (a merged tree cannot say which
        # layer delivered a file).
        #
        # Off the event loop: this walks the tree and unlinks with blocking
        # syscalls, and the runner serves status polling and health on the same
        # loop while a populate job runs.
        #
        # ONE ROOT PER PASS, and which root goes in which pass is load-bearing.
        # Resolution consumes a marker along with its target, so whichever pass
        # reaches a root first is the ONLY one that can ever act on it. Taking
        # both roots here would eat the `.apps_data` markers before the hooks
        # below re-seed those paths, leaving step 4 nothing to rediscover — the
        # island splice shipped exactly that bug and had to be split the same
        # way (`ISLAND_ROOTS` in the server copy).
        #
        # `filesystem` belongs in this pass: nothing after the download stage
        # writes there, so an early pass is final, and a post-populate hook
        # still observes a finished public tree rather than one about to change.
        resolve_subtraction = _has_authored_task_layer(request.sources)
        subsystem_paths = get_subsystem_paths()
        if resolve_subtraction:
            await _timed_marker_resolution(
                "filesystem", [subsystem_paths[settings.FILESYSTEM_SUBSYSTEM_NAME]]
            )

        # 3. Run post-populate hooks (in parallel — services have isolated state)
        hook_timings: list[HookTiming] = []
        if request.post_populate_hooks:
            logger.info(
                f"Running {len(request.post_populate_hooks)} post-populate hook(s)"
            )
            hook_timings = await run_lifecycle_hooks(
                request.post_populate_hooks, phase="post_populate"
            )
            logger.info("All post-populate hooks completed")

        # 4. Resolve `.apps_data` subtraction markers, AFTER the hooks. Service
        # state is the one root a hook writes — a mysql hook loading its dump
        # re-creates the very path a task subtracted — so a pass before them is
        # deterministically undone, and the marker is gone by then so nothing
        # can undo it back. This is the only pass that can subtract service
        # state, which is what makes `.apps_data` subtraction work here rather
        # than being documented as unsupported.
        #
        # Runs even with no hooks attached: then it is simply the same tree a
        # single pass would have seen.
        #
        # This pass was inert while the server still attached
        # `SUBTRACTION_HOOK_COMMAND`: that shell swept BOTH roots from inside the
        # gather above, so the `.apps_data` markers were consumed before this ran
        # and it found nothing. The hook is gone as of this change, so this is now
        # the only thing that can subtract service state, and the ordering above
        # is what makes it work rather than lose to a re-seeding hook.
        if resolve_subtraction:
            await _timed_marker_resolution(
                "apps_data", [subsystem_paths[settings.APPS_DATA_SUBSYSTEM_NAME]]
            )

        return PopulateResult(
            objects_added=result.objects_added,
            download_seconds=result.download_seconds,
            hook_timings=hook_timings,
        )
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
