"""Tests for the async populate job registry (``runner.data.populate.jobs``).

The trajectory populate path runs the ingest as a background job so the caller
only ever holds short poll requests — Modal's connect-token sandbox proxy closes
a single blocking populate at ~5 min, which large-snapshot ingests exceed. These
tests cover the registry's terminal-state capture without spinning up the
environment container (the background task drives ``handle_populate``, which we
stub per-test).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from runner.data.populate import jobs
from runner.data.populate.models import (
    PopulateRequest,
    PopulateResult,
    PopulateSource,
)


def _request() -> PopulateRequest:
    return PopulateRequest(
        sources=[PopulateSource(url="s3://bucket/key/", subsystem="filesystem")]
    )


async def test_start_populate_job_records_result_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job whose populate succeeds ends 'done' carrying the PopulateResult."""
    result = PopulateResult(objects_added=7)

    async def fake_handle(_req: PopulateRequest) -> PopulateResult:
        return result

    monkeypatch.setattr(jobs, "handle_populate", fake_handle)

    job_id = jobs.start_populate_job(_request())
    job = jobs.get_populate_job(job_id)
    assert job is not None
    assert job.status == "running" or job.status == "done"
    assert job.task is not None

    await job.task  # let the background task finish

    assert job.status == "done"
    assert job.result is result
    assert job.error is None


async def test_start_populate_job_records_hook_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """handle_populate's HTTPException (hook failure) surfaces as 'error' + detail."""

    async def fake_handle(_req: PopulateRequest) -> PopulateResult:
        raise HTTPException(status_code=500, detail="hook boom")

    monkeypatch.setattr(jobs, "handle_populate", fake_handle)

    job_id = jobs.start_populate_job(_request())
    job = jobs.get_populate_job(job_id)
    assert job is not None
    assert job.task is not None
    await job.task

    assert job.status == "error"
    assert job.error == "hook boom"
    assert job.result is None


async def test_start_populate_job_records_unexpected_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-HTTP exception is captured as 'error' rather than escaping the task."""

    async def fake_handle(_req: PopulateRequest) -> PopulateResult:
        raise ValueError("kaboom")

    monkeypatch.setattr(jobs, "handle_populate", fake_handle)

    job_id = jobs.start_populate_job(_request())
    job = jobs.get_populate_job(job_id)
    assert job is not None
    assert job.task is not None
    await job.task

    assert job.status == "error"
    assert "kaboom" in (job.error or "")


def test_get_populate_job_unknown_returns_none() -> None:
    assert jobs.get_populate_job("does-not-exist") is None


async def test_cancel_populate_job_stops_a_running_ingest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provision retry re-runs populate on the same sandbox, so the ingest the
    caller gave up on has to be gone, not merely asked to stop, when cancel answers."""

    async def fake_handle(_req: PopulateRequest) -> PopulateResult:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    monkeypatch.setattr(jobs, "handle_populate", fake_handle)
    job_id = jobs.start_populate_job(_request())
    await asyncio.sleep(0)

    cancelled = await jobs.cancel_populate_job(job_id)

    assert cancelled is not None
    assert cancelled.status == "error"
    assert "cancelled" in (cancelled.error or "").lower()
    assert cancelled.task is not None and cancelled.task.done()


async def test_cancel_populate_job_keeps_a_finished_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = PopulateResult(objects_added=2)

    async def fake_handle(_req: PopulateRequest) -> PopulateResult:
        return result

    monkeypatch.setattr(jobs, "handle_populate", fake_handle)
    job_id = jobs.start_populate_job(_request())

    cancelled = await jobs.cancel_populate_job(job_id)

    assert cancelled is not None
    assert (cancelled.status, cancelled.result) == ("done", result)


async def test_cancel_populate_job_unknown_returns_none() -> None:
    assert await jobs.cancel_populate_job("does-not-exist") is None
