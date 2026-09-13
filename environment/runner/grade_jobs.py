"""Async (start + poll) shape for the in-container grade, beside the blocking one.

`POST /grade` is held open for the whole grade, and Modal's connect-token
sandbox proxy closes a request that produces no response for ~5 min. A grade
runs every verifier and an LLM judge, so it passes that easily. `/grade/start`
launches it as a background task and returns a `job_id`; the caller polls
`/grade/status/{job_id}` with short requests until it finishes. Exactly the
shape `data/populate/jobs.py` already uses, and for the same reason.

The blocking `POST /grade` is unchanged. hosted-envs calls it from its own
servicer, which does not go through that proxy, and nothing about that path
needs to move.

Both routes hand the same `GradeRequest` to the same `_grade` coroutine under
the same lock, so the request model, the SSRF boundary on snapshot URLs, the
credential allowlist and the root-owned scratch dir are the ones already
written in `grade.py`.

State is per-process and lives for the sandbox's lifetime. There is at most one
grade per sandbox, so a module-level dict is sufficient; no eviction.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Literal

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel

# Private, and deliberately: the alternative is a second copy of the lock
# discipline that keeps two grades off one mutable filesystem.
from .grade import (
    _GRADE_LOCK,
    _GRADE_WORK_DIR,
    GradeRequest,
    GradeResponse,
    _capture_live_final_snapshot,
    _grade,
    capture_dir,
    grading_available,
)
from .sweep import read_uptime_ticks, sweep_agent_processes

router = APIRouter()

JobStatus = Literal["running", "done", "error"]


@dataclass
class GradeJob:
    """Mutable state for one background grade, updated in place by its task."""

    status: JobStatus = "running"
    result: GradeResponse | None = None
    error: str | None = None
    # Hold a strong reference to the running task: asyncio keeps only a weak
    # reference to a bare task, so without this the GC can cancel it mid-run.
    task: asyncio.Task[None] | None = field(default=None, repr=False)


_JOBS: dict[str, GradeJob] = {}


class GradeJobStarted(BaseModel):
    """The id to poll."""

    job_id: str


class GradeJobStatus(BaseModel):
    """`running` until the grade finishes, then `done` with the CLI's result, or
    `error` with the reason. A dropped connection is not one of the answers,
    which is the point of the shape."""

    status: JobStatus
    result: dict[str, object] | None = None
    error: str | None = None


def start_grade_job(request: GradeRequest) -> str:
    """Launch the grade in the background; return its job id.

    Any failure is captured onto the job as `status="error"`, including the
    `HTTPException` the grade raises, so the poller sees a terminal state
    rather than a dropped connection.
    """
    job_id = uuid.uuid4().hex
    job = GradeJob()
    _JOBS[job_id] = job

    async def _run() -> None:
        try:
            async with _GRADE_LOCK:
                job.result = await _grade(request)
            job.status = "done"
            logger.info(f"Grade job {job_id} done")
        except HTTPException as e:
            job.status = "error"
            job.error = str(e.detail)
            logger.error(f"Grade job {job_id} failed: {e.detail}")
        except Exception as e:  # noqa: BLE001 - record any failure for the poller
            job.status = "error"
            job.error = repr(e)
            # A formatted traceback and not `logger.opt(exception=True)`: both
            # sinks set diagnose=True, which prints each frame's values, and
            # this frame holds the GradeRequest.
            logger.error(
                f"Grade job {job_id} crashed: {repr(e)}\n{traceback.format_exc()}"
            )
        finally:
            # `_JOBS` holds this closure for the sandbox's lifetime, so drop
            # what is worth stealing now the grade is over.
            request.grading_credentials_json = "{}"
            request.initial_snapshot_url = ""
            request.task_snapshot_url = ""
            request.golden_snapshot_urls = []
            # A workspace zip, so it is not left on the sandbox's disk for the
            # rest of its life.
            if request.capture_id:
                shutil.rmtree(capture_dir(request.capture_id), ignore_errors=True)

    job.task = asyncio.create_task(_run())
    logger.info(f"Started grade job {job_id} for run {request.grading_run_id}")
    return job_id


def get_grade_job(job_id: str) -> GradeJob | None:
    """The job for `job_id`, or None if it was never started."""
    return _JOBS.get(job_id)


class UptimeResponse(BaseModel):
    uptime_ticks: float


@router.get("/grade/uptime", response_model=UptimeResponse)
async def uptime() -> UptimeResponse:
    """This sandbox's monotonic clock, for the caller to hold as a reference.

    Read before the agent loop; the sweep later kills everything that started
    after it. Ticks since boot, so a root process calling `settimeofday` cannot
    move it.
    """
    return UptimeResponse(uptime_ticks=read_uptime_ticks())


class SweepRequest(BaseModel):
    """Ticks since boot, captured by the caller before the agent loop began.

    Sent by the caller and not read here, because this process starts before the
    agent and cannot know when the loop began. Monotonic, so a root process
    calling `settimeofday` cannot move it.
    """

    reference_ticks: float


class SweepResponse(BaseModel):
    clean: bool
    killed: list[int]
    survivors: list[int]
    proc_trustworthy: bool
    detail: str = ""


@router.post("/grade/sweep", response_model=SweepResponse)
async def sweep(request: SweepRequest) -> SweepResponse:
    """Kill the agent's processes, before the caller mounts the grading image.

    `clean=False` means the caller must not grade here. It is not an error, so
    this answers 200: the caller reads the flag and leaves the trajectory to the
    lane, the same as every other refusal on this path.
    """
    result = await asyncio.to_thread(sweep_agent_processes, request.reference_ticks)
    return SweepResponse(
        clean=result.clean,
        killed=result.killed,
        survivors=result.survivors,
        proc_trustworthy=result.proc_trustworthy,
        detail=result.detail,
    )


class CaptureRequest(BaseModel):
    snapshot_exclude_globs: list[str] = []


class CaptureResponse(BaseModel):
    capture_id: str


@router.post("/grade/capture", response_model=CaptureResponse)
async def capture(request: CaptureRequest) -> CaptureResponse:
    """Zip the live tree now, for a grade that runs once it has been mutated.

    The caller's end-of-run image builders delete files and stop services, and
    they run between this call and the grade. Capturing inside the grade would
    read what they left behind, while the lane scores the S3 snapshot taken
    before them, so the two would disagree on the same trajectory.

    Deliberately does not require a mounted grading engine: this runs before the
    sweep and the mount, so nothing is there to check yet.
    """
    capture_id = uuid.uuid4().hex
    target = capture_dir(capture_id)
    os.makedirs(_GRADE_WORK_DIR, mode=0o700, exist_ok=True)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    # CPU-bound, so off the event loop, exactly as the in-grade capture is.
    await asyncio.to_thread(
        _capture_live_final_snapshot,
        target / "final.zip",
        request.snapshot_exclude_globs or None,
    )
    logger.info(f"Captured the final tree as {capture_id}")
    return CaptureResponse(capture_id=capture_id)


@router.post("/grade/start", response_model=GradeJobStarted)
async def grade_start(request: GradeRequest) -> GradeJobStarted:
    """Start a grade and return a job id to poll.

    404 when the grading venv is absent, which is the same answer the blocking
    route gives and the reason the check is per request: an image that mounts
    the engine AFTER the agent loop has no venv at boot, and a route registered
    at import time would never exist for it.
    """
    if not grading_available():
        raise HTTPException(status_code=404, detail="No grading engine in this sandbox")
    return GradeJobStarted(job_id=start_grade_job(request))


@router.get("/grade/status/{job_id}", response_model=GradeJobStatus)
async def grade_status(job_id: str) -> GradeJobStatus:
    """Poll a grade started via `/grade/start`."""
    job = get_grade_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown grade job: {job_id}")
    return GradeJobStatus(
        status=job.status,
        result=dict(job.result.result) if job.result is not None else None,
        error=job.error,
    )
