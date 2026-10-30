"""Tests for the async snapshot job registry (``runner.data.snapshot.jobs``).

The trajectory snapshot path runs the harvest (pre-snapshot hooks + S3
upload) as a background job so the caller only ever holds short poll
requests — Modal's connect-token sandbox proxy closes a single blocking
snapshot at ~5 min, which large-world snapshots exceed. These tests cover
the registry's terminal-state capture without spinning up the environment
container (the background task drives ``handle_snapshot_s3_files`` /
``handle_snapshot_s3``, which we stub per-test). Mirrors
``test_populate_jobs.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from fastapi import HTTPException

from runner.data.snapshot import jobs
from runner.data.snapshot.models import (
    SnapshotFilesResult,
    SnapshotRequest,
)


def _request(fmt: str = "files") -> SnapshotRequest:
    return SnapshotRequest(format=fmt)


async def test_start_snapshot_job_records_result_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job whose snapshot succeeds ends 'done' carrying the result."""
    result = SnapshotFilesResult(snapshot_id="snap_1", files_uploaded=2, total_bytes=10)

    async def fake_handle(**_kwargs: Any) -> SnapshotFilesResult:
        return result

    monkeypatch.setattr(jobs, "handle_snapshot_s3_files", fake_handle)

    job_id = jobs.start_snapshot_job(_request())
    job = jobs.get_snapshot_job(job_id)
    assert job is not None
    assert job.status == "running" or job.status == "done"
    assert job.task is not None

    await job.task  # let the background task finish

    assert job.status == "done"
    assert job.result is result
    assert job.error is None


async def test_start_snapshot_job_routes_non_files_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-'files' format routes to handle_snapshot_s3, matching the
    blocking route's dispatch."""
    called = False

    async def fake_tar(**_kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise ValueError("stop here")

    monkeypatch.setattr(jobs, "handle_snapshot_s3", fake_tar)

    job_id = jobs.start_snapshot_job(_request(fmt="tar.gz"))
    job = jobs.get_snapshot_job(job_id)
    assert job is not None
    assert job.task is not None
    await job.task

    assert called
    assert job.status == "error"


async def test_start_snapshot_job_records_hook_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler's HTTPException (hook failure) surfaces as 'error' + detail."""

    async def fake_handle(**_kwargs: Any) -> SnapshotFilesResult:
        raise HTTPException(status_code=500, detail="hook boom")

    monkeypatch.setattr(jobs, "handle_snapshot_s3_files", fake_handle)

    job_id = jobs.start_snapshot_job(_request())
    job = jobs.get_snapshot_job(job_id)
    assert job is not None
    assert job.task is not None
    await job.task

    assert job.status == "error"
    assert job.error == "hook boom"
    assert job.result is None


async def test_start_snapshot_job_records_unexpected_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-HTTP exception is captured as 'error' rather than escaping the task."""

    async def fake_handle(**_kwargs: Any) -> SnapshotFilesResult:
        raise ValueError("kaboom")

    monkeypatch.setattr(jobs, "handle_snapshot_s3_files", fake_handle)

    job_id = jobs.start_snapshot_job(_request())
    job = jobs.get_snapshot_job(job_id)
    assert job is not None
    assert job.task is not None
    await job.task

    assert job.status == "error"
    assert "kaboom" in (job.error or "")


def test_get_snapshot_job_unknown_returns_none() -> None:
    assert jobs.get_snapshot_job("does-not-exist") is None


async def test_cancel_snapshot_job_stops_a_running_harvest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller only cancels after giving up its poll, by which point it has
    released the lock that serializes snapshots — so a harvest left running
    would overlap the next one on an env that rejects concurrent snapshots."""

    async def fake_handle(**_kwargs: Any) -> SnapshotFilesResult:
        await asyncio.sleep(3600)  # still harvesting when the caller gives up
        raise AssertionError("unreachable")

    monkeypatch.setattr(jobs, "handle_snapshot_s3_files", fake_handle)

    job_id = jobs.start_snapshot_job(_request())
    await asyncio.sleep(0)  # let the task reach the await

    cancelled = await jobs.cancel_snapshot_job(job_id)

    assert cancelled is not None
    # Terminal here too, so a late poll reads `error` rather than `running`
    # forever — CancelledError derives from BaseException, so the task's own
    # `except Exception` would never have recorded it.
    assert cancelled.status == "error"
    assert "cancelled" in (cancelled.error or "").lower()
    # Already finished when cancel returned: the caller drops the snapshot lock
    # the moment it gets this answer, so "asked to stop" would not be enough.
    assert cancelled.task is not None
    assert cancelled.task.done()


async def test_cancel_snapshot_job_waits_for_the_harvest_to_unwind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A harvest that ignores cancellation briefly must still be gone before the
    cancel answers — `Task.cancel()` only requests it, and the caller releases
    the lock on the reply."""
    unwound = asyncio.Event()

    async def fake_handle(**_kwargs: Any) -> SnapshotFilesResult:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Stand-in for an upload/hook that takes a moment to let go.
            await asyncio.sleep(0.05)
            unwound.set()
            raise
        raise AssertionError("unreachable")

    monkeypatch.setattr(jobs, "handle_snapshot_s3_files", fake_handle)

    job_id = jobs.start_snapshot_job(_request())
    await asyncio.sleep(0)

    cancelled = await jobs.cancel_snapshot_job(job_id)

    assert cancelled is not None
    assert unwound.is_set()
    assert cancelled.task is not None and cancelled.task.done()


async def test_cancel_snapshot_job_gives_up_after_the_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task that never lets go must not hold the caller's request open past
    its own timeout — the wait is bounded, and the overlap window is logged."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_handle(**_kwargs: Any) -> SnapshotFilesResult:
        started.set()
        # Swallows the first cancellation, standing in for a thread or
        # subprocess that doesn't let go promptly. `release` lets the test end
        # it deterministically rather than leaving an unkillable task behind.
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await release.wait()
            raise
        raise AssertionError("unreachable")

    monkeypatch.setattr(jobs, "handle_snapshot_s3_files", fake_handle)

    job_id = jobs.start_snapshot_job(_request())
    await started.wait()

    cancelled = await jobs.cancel_snapshot_job(job_id, grace_seconds=0.05)

    assert cancelled is not None
    # Still reported terminal: a late poll must not read `running` forever, even
    # though the harvest is demonstrably still going.
    assert cancelled.status == "error"
    assert cancelled.task is not None and not cancelled.task.done()

    release.set()
    with contextlib.suppress(asyncio.CancelledError):
        await cancelled.task


async def test_cancel_racing_a_finishing_harvest_keeps_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race the caller's keep-the-result path exists for.

    A cancel arriving just as the harvest returns must not overwrite a genuine
    result: those objects are already in S3, and reporting "cancelled" fails the
    caller and makes them redo minutes of completed work.
    """
    result = SnapshotFilesResult(
        snapshot_id="snap_won", files_uploaded=3, total_bytes=9
    )
    running = asyncio.Event()
    finish = asyncio.Event()

    async def fake_handle(**_kwargs: Any) -> SnapshotFilesResult:
        running.set()
        await finish.wait()
        return result

    monkeypatch.setattr(jobs, "handle_snapshot_s3_files", fake_handle)

    job_id = jobs.start_snapshot_job(_request())
    await running.wait()

    # Let the harvest complete in the same tick the cancel is issued.
    finish.set()
    cancelled = await jobs.cancel_snapshot_job(job_id)

    assert cancelled is not None
    assert cancelled.status == "done"
    assert cancelled.result is result


async def test_cancel_snapshot_job_leaves_a_finished_job_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotent: a cancel racing a job that just succeeded must not rewrite a
    real result into an error the caller would then retry."""
    result = SnapshotFilesResult(snapshot_id="snap_1", files_uploaded=2, total_bytes=10)

    async def fake_handle(**_kwargs: Any) -> SnapshotFilesResult:
        return result

    monkeypatch.setattr(jobs, "handle_snapshot_s3_files", fake_handle)

    job_id = jobs.start_snapshot_job(_request())
    job = jobs.get_snapshot_job(job_id)
    assert job is not None and job.task is not None
    await job.task

    cancelled = await jobs.cancel_snapshot_job(job_id)

    assert cancelled is not None
    assert cancelled.status == "done"
    assert cancelled.result is result


async def test_cancel_snapshot_job_unknown_returns_none() -> None:
    assert await jobs.cancel_snapshot_job("does-not-exist") is None
