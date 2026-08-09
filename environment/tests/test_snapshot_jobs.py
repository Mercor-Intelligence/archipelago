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
