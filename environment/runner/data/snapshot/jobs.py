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
                )
            else:
                job.result = await handle_snapshot_s3(
                    snapshot_id=request.snapshot_id,
                    pre_snapshot_hooks=hooks,
                    s3_credentials=request.s3_credentials,
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
