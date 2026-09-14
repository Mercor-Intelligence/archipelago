"""The start + poll shape for the in-container grade (runner.grade_jobs).

A grade runs every verifier and an LLM judge, and Modal's connect-token sandbox
proxy closes a request that produces no response for ~5 min, so a blocking POST
loses the connection while the grade keeps running server-side. These cover the
shape that replaces it, and the availability check that had to move out of
`include_router` for a sandbox that mounts its engine after the agent loop.
"""

import asyncio

import pytest
from fastapi import HTTPException

from runner import grade_jobs
from runner.grade import GradeRequest, GradeResponse


def _request() -> GradeRequest:
    return GradeRequest(
        grading_run_id="gr_1",
        trajectory_id="traj_1",
        trajectory_json="{}",
        grading_settings_json="{}",
        verifiers_json="[]",
        eval_configs_json="{}",
        scoring_config_json="{}",
    )


@pytest.mark.asyncio
async def test_a_started_job_runs_then_reports_done(monkeypatch):
    """The caller gets an id immediately and polls to a terminal state, which is
    the whole reason for the shape: no held-open request to be closed."""
    released = asyncio.Event()

    async def _slow(_request):
        await released.wait()
        return GradeResponse(result={"grading_run_status": "completed"})

    monkeypatch.setattr(grade_jobs, "_grade", _slow)

    job_id = grade_jobs.start_grade_job(_request())
    job = grade_jobs.get_grade_job(job_id)
    assert job is not None and job.task is not None
    assert job.status == "running"

    released.set()
    await job.task

    status = await grade_jobs.grade_status(job_id)
    assert status.status == "done"
    assert status.result == {"grading_run_status": "completed"}


@pytest.mark.asyncio
async def test_a_raise_inside_the_grade_becomes_a_terminal_error(monkeypatch):
    """Captured onto the job, not escaping into the poller. A dropped connection
    is the one answer the caller must never have to interpret."""

    async def _boom(_request):
        raise HTTPException(status_code=500, detail="snapshot archive is neither")

    monkeypatch.setattr(grade_jobs, "_grade", _boom)

    job_id = grade_jobs.start_grade_job(_request())
    job = grade_jobs.get_grade_job(job_id)
    assert job is not None and job.task is not None
    await job.task

    status = await grade_jobs.grade_status(job_id)
    assert status.status == "error"
    assert "snapshot archive" in (status.error or "")


@pytest.mark.asyncio
async def test_an_unknown_job_id_is_a_404():
    with pytest.raises(HTTPException) as excinfo:
        _ = await grade_jobs.grade_status("nope")
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_no_engine_is_a_404_and_not_a_started_job(monkeypatch):
    """The check the route registration used to do at import. An image without a
    grader answers 404 rather than starting a job that can only fail."""
    monkeypatch.setattr(grade_jobs, "grading_available", lambda: False)

    with pytest.raises(HTTPException) as excinfo:
        _ = await grade_jobs.grade_start(_request())
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_two_grades_do_not_run_at_once(monkeypatch):
    """One mutable sandbox filesystem. The async routes take the same lock the
    blocking one does, so starting twice serializes rather than racing."""
    running = 0
    peak = 0

    async def _watch(_request):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)
        running -= 1
        return GradeResponse(result={})

    monkeypatch.setattr(grade_jobs, "_grade", _watch)

    first = grade_jobs.get_grade_job(grade_jobs.start_grade_job(_request()))
    second = grade_jobs.get_grade_job(grade_jobs.start_grade_job(_request()))
    assert first is not None and first.task is not None
    assert second is not None and second.task is not None
    await asyncio.gather(first.task, second.task)

    assert peak == 1


@pytest.mark.asyncio
async def test_a_finished_job_keeps_no_credentials(monkeypatch):
    """`_JOBS` holds the job for the sandbox's lifetime so the poller can read a
    terminal state. Holding the provider keys and presigned URLs that long is
    what this drops."""
    request = GradeRequest(
        grading_run_id="gr_1",
        trajectory_id="traj_1",
        trajectory_json="{}",
        grading_settings_json="{}",
        verifiers_json="[]",
        eval_configs_json="{}",
        scoring_config_json="{}",
        grading_credentials_json='{"OPENAI_API_KEY": "k"}',
        initial_snapshot_url="https://s3/world.zip",
        task_snapshot_url="https://s3/task.zip",
        golden_snapshot_urls=["https://s3/gold.zip"],
    )

    async def _fake_grade(_req):
        return GradeResponse(result={"scoring_results": {"final_score": 1.0}})

    monkeypatch.setattr(grade_jobs, "_grade", _fake_grade)

    job_id = grade_jobs.start_grade_job(request)
    job = grade_jobs.get_grade_job(job_id)
    assert job is not None
    assert job.task is not None
    await job.task

    assert request.grading_credentials_json == "{}"
    assert request.initial_snapshot_url == ""
    assert request.task_snapshot_url == ""
    assert request.golden_snapshot_urls == []
