"""In-memory registry for background (async) populate jobs.

A single blocking ``POST /data/populate/s3`` is held open for the entire
ingest, and Modal's connect-token sandbox proxy closes a request that produces
no response for ~5 min — which a large-snapshot populate (e.g. a multi-GB
CSV -> SQLite ingest) easily exceeds. To avoid that, ``/populate/s3/start``
launches the populate as a background task and returns a ``job_id``; the caller
polls ``/populate/s3/status/{job_id}`` with short requests until it finishes.

The synchronous ``POST /data/populate/s3`` route is unchanged — small worlds
(and the playground path) keep using it.

State is per-process and lives for the sandbox's lifetime. There is at most one
populate per sandbox, so a plain module-level dict is sufficient; no eviction.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Literal

from fastapi import HTTPException
from loguru import logger

from .main import handle_populate
from .models import PopulateRequest, PopulateResult

JobStatus = Literal["running", "done", "error"]


@dataclass
class PopulateJob:
    """Mutable state for one background populate, updated in place by its task."""

    status: JobStatus = "running"
    result: PopulateResult | None = None
    error: str | None = None
    # Hold a strong reference to the running task: asyncio keeps only a weak
    # reference to a bare task, so without this the GC can cancel it mid-run.
    task: asyncio.Task[None] | None = field(default=None, repr=False)


_JOBS: dict[str, PopulateJob] = {}


def start_populate_job(request: PopulateRequest) -> str:
    """Launch ``handle_populate(request)`` in the background; return its job id.

    The returned id is polled via :func:`get_populate_job`. Any failure
    (including the ``HTTPException`` ``handle_populate`` raises on hook failure)
    is captured onto the job as ``status="error"`` so the poller sees a clean
    terminal state instead of a dropped connection.
    """
    job_id = uuid.uuid4().hex
    job = PopulateJob()
    _JOBS[job_id] = job

    async def _run() -> None:
        try:
            job.result = await handle_populate(request)
            job.status = "done"
            added = job.result.objects_added
            logger.info(f"Populate job {job_id} done: {added} object(s) added")
        except HTTPException as e:
            job.status = "error"
            job.error = str(e.detail)
            logger.error(f"Populate job {job_id} failed: {e.detail}")
        except Exception as e:  # noqa: BLE001 - record any failure for the poller
            job.status = "error"
            job.error = repr(e)
            logger.opt(exception=True).error(f"Populate job {job_id} crashed")

    job.task = asyncio.create_task(_run())
    n_sources = len(request.sources)
    n_hooks = len(request.post_populate_hooks)
    logger.info(
        f"Started populate job {job_id} ({n_sources} source(s), {n_hooks} hook(s))"
    )
    return job_id


def get_populate_job(job_id: str) -> PopulateJob | None:
    """Return the job for ``job_id``, or ``None`` if it was never started."""
    return _JOBS.get(job_id)
