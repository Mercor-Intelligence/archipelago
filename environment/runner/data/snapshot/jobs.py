"""In-memory registry for background (async) snapshot jobs.

A single blocking ``POST /data/snapshot/s3`` is held open for the entire
harvest (pre-snapshot hooks + S3 upload), and Modal's connect-token sandbox
proxy closes a request that produces no response for ~5 min — which a
large-world snapshot (multi-GB ``.apps_data`` export + upload) easily
exceeds. To avoid that, ``/snapshot/s3/start`` launches the snapshot as a
background task and returns a ``job_id``; the caller polls
``/snapshot/s3/status/{job_id}`` with short requests until it finishes.

The synchronous ``POST /data/snapshot/s3`` route is unchanged — small worlds
keep using it, and it is the 404 fallback for callers newer than this image.

State is per-process and lives for the sandbox's lifetime. Snapshots happen
at most a few times per sandbox (post-populate + final), so a plain
module-level dict is sufficient; no eviction. Mirrors ``..populate.jobs``.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Literal

from fastapi import HTTPException
from loguru import logger

from .main import handle_snapshot_s3, handle_snapshot_s3_files
from .models import SnapshotFilesResult, SnapshotRequest, SnapshotResult

JobStatus = Literal["running", "done", "error"]

# How long a cancel waits for the harvest to actually unwind before answering.
# Bounded well under the caller's own per-request timeout (60s on the Studio
# side) so a thread that ignores cancellation can't hold their request open —
# and long enough that an in-flight upload or hook normally finishes unwinding
# inside it.
CANCEL_GRACE_SECONDS = 30.0
# Event-loop ticks a cancel yields before pulling the trigger, so a harvest that
# has already done its work can record it. Ticks rather than a duration: this is
# about letting an at-most-scheduled task resume, not about waiting for work.
_CANCEL_SETTLE_TICKS = 3


@dataclass
class SnapshotJob:
    """Mutable state for one background snapshot, updated in place by its task."""

    status: JobStatus = "running"
    result: SnapshotResult | SnapshotFilesResult | None = None
    error: str | None = None
    # Hold a strong reference to the running task: asyncio keeps only a weak
    # reference to a bare task, so without this the GC can cancel it mid-run.
    task: asyncio.Task[None] | None = field(default=None, repr=False)


_JOBS: dict[str, SnapshotJob] = {}


def start_snapshot_job(request: SnapshotRequest) -> str:
    """Launch the snapshot in the background; return its job id.

    Runs the same work as the blocking ``POST /data/snapshot/s3`` route for
    the request's format. The returned id is polled via
    :func:`get_snapshot_job`. Any failure (including the ``HTTPException``
    the handlers raise on hook failure) is captured onto the job as
    ``status="error"`` so the poller sees a clean terminal state instead of a
    dropped connection.
    """
    job_id = uuid.uuid4().hex
    job = SnapshotJob()
    _JOBS[job_id] = job

    async def _run() -> None:
        try:
            hooks = request.pre_snapshot_hooks or None
            if request.format == "files":
                job.result = await handle_snapshot_s3_files(
                    snapshot_id=request.snapshot_id,
                    pre_snapshot_hooks=hooks,
                    s3_credentials=request.s3_credentials,
                    snapshot_zip_enabled=request.snapshot_zip_enabled,
                    exclude_globs=request.exclude_globs,
                )
            else:
                job.result = await handle_snapshot_s3(
                    snapshot_id=request.snapshot_id,
                    pre_snapshot_hooks=hooks,
                    s3_credentials=request.s3_credentials,
                    exclude_globs=request.exclude_globs,
                )
            job.status = "done"
            logger.info(f"Snapshot job {job_id} done: {job.result.snapshot_id}")
        except HTTPException as e:
            job.status = "error"
            job.error = str(e.detail)
            logger.error(f"Snapshot job {job_id} failed: {e.detail}")
        except Exception as e:  # noqa: BLE001 - record any failure for the poller
            job.status = "error"
            job.error = repr(e)
            logger.opt(exception=True).error(f"Snapshot job {job_id} crashed")

    job.task = asyncio.create_task(_run())
    n_hooks = len(request.pre_snapshot_hooks)
    logger.info(
        f"Started snapshot job {job_id} (format={request.format}, {n_hooks} hook(s))"
    )
    return job_id


def get_snapshot_job(job_id: str) -> SnapshotJob | None:
    """Return the job for ``job_id``, or ``None`` if it was never started."""
    return _JOBS.get(job_id)


async def cancel_snapshot_job(
    job_id: str, grace_seconds: float = CANCEL_GRACE_SECONDS
) -> SnapshotJob | None:
    """Stop a running snapshot job and wait for it to actually stop.

    Called when the poller gives up (its deadline expired). The caller releases
    the per-playground lock that serializes snapshots the moment this returns,
    so a harvest still running at that point would overlap the next one against
    an env that rejects concurrent snapshots — the exact race the lock exists to
    prevent.

    That is why this awaits the task rather than just cancelling it.
    ``Task.cancel()`` only *requests* cancellation: the coroutine keeps running
    until its next suspension point, and a hook subprocess can be mid-flight for
    a while yet. Returning right after the request would make the HTTP response
    mean "asked to stop", which is not what the caller needs to hear before
    dropping the lock.

    One honest limit on that guarantee: it covers the ``await``-ing work, which
    is all of the S3 traffic (aioboto3) and the hooks. It does **not** cover the
    prebuilt-ZIP build, which runs under ``asyncio.to_thread`` — cancelling that
    future resolves the awaiting coroutine at once while the worker thread keeps
    compressing, and ``_upload_snapshot_zip``'s ``finally`` then unlinks the temp
    archive out from under it. So on a zip-enabled world the sandbox may still be
    burning CPU and disk briefly after this returns. Harmless for S3 correctness
    (the archive upload comes after the build and simply doesn't happen), but the
    caller is not entitled to assume the box is idle.

    The wait is bounded — a thread that ignores cancellation must not hold the
    caller's request open past its own timeout — and a job that outlives the
    grace is logged loudly, since the overlap window is then genuinely open.

    Marking the job terminal also means a late poll reads ``error`` instead of
    ``running`` forever. Whatever partial objects the harvest wrote are orphaned
    under a snapshot_id no row will ever reference.
    """
    job = _JOBS.get(job_id)
    if job is None:
        return None
    if job.status != "running":
        return job

    task = job.task
    if task is not None:
        # Give a harvest that has already finished its work a chance to record
        # it before we cancel. `Task.cancel()` on a task whose awaited future
        # has resolved but which has not been resumed yet still raises
        # CancelledError into it, discarding a result whose objects are already
        # in S3 — so without this yield the "cancel raced a finishing harvest"
        # case is unrecoverable rather than merely rare.
        for _ in range(_CANCEL_SETTLE_TICKS):
            if task.done():
                break
            await asyncio.sleep(0)
        if job.status != "running":
            logger.info(
                f"Snapshot job {job_id} finished just before the cancel; "
                f"keeping its {job.status} outcome"
            )
            return job

        task.cancel()
        # asyncio.wait rather than awaiting the task directly — it reports
        # completion instead of re-raising the CancelledError we just caused.
        done, _pending = await asyncio.wait({task}, timeout=grace_seconds)
        if not done:
            job.status = "error"
            job.error = "Snapshot cancelled by the caller"
            logger.error(
                f"Snapshot job {job_id} did not stop within {grace_seconds:.0f}s "
                "of being cancelled; the harvest may still be writing while the "
                "caller releases the playground's snapshot lock"
            )
            return job

    # Only now, and only if the harvest didn't beat us to it. A cancel that
    # arrives just as `_run` returns finds a genuine result already recorded —
    # its objects are in S3 — and overwriting that with "cancelled" would fail
    # the caller and make them redo minutes of completed work. This is the race
    # the caller's keep-the-result path exists for, so it has to survive here.
    #
    # Checked after the await rather than before the cancel: the window is
    # between the two, so an early write would clobber exactly the case it is
    # meant to preserve. CancelledError derives from BaseException, so `_run`'s
    # `except Exception` never records it — a truly cancelled job is still
    # "running" at this point and gets its terminal state from us.
    if job.status == "running":
        job.status = "error"
        job.error = "Snapshot cancelled by the caller"
        logger.warning(f"Snapshot job {job_id} cancelled by the caller")
    else:
        logger.info(
            f"Snapshot job {job_id} finished as it was being cancelled; "
            f"keeping its {job.status} outcome"
        )
    return job
