"""Tests for ``POST /data/snapshot/s3/credentials/{job_id}``.

Hermetic: the data router is mounted on a bare FastAPI app and driven through
``httpx.ASGITransport`` (no real sockets, no container), so this runs under
network-isolated CI. The route's job is narrow — resolve the job, refuse
clearly when it cannot take the push, and never report success for a push that
had no effect.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from runner.data.router import router as data_router
from runner.data.snapshot import jobs
from runner.data.snapshot.models import SnapshotFilesResult

pytestmark = pytest.mark.asyncio


def _payload(access_key: str = "AKIA_REFRESHED", ttl: float = 3600.0) -> dict[str, Any]:
    return {
        "s3_credentials": {
            "access_key_id": access_key,
            "secret_access_key": "secret",
            "session_token": "token",
            "region": "us-west-2",
        },
        "expires_at": (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl)).isoformat(),
    }


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(data_router, prefix="/data")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://env.local") as c:
        yield c


def _start_job(
    monkeypatch: pytest.MonkeyPatch, *, refreshable: bool, gate: asyncio.Event
) -> str:
    async def fake_handle(**_kwargs: Any) -> SnapshotFilesResult:
        await gate.wait()
        return SnapshotFilesResult(
            snapshot_id="snap_1", files_uploaded=0, total_bytes=0
        )

    monkeypatch.setattr(jobs, "handle_snapshot_s3_files", fake_handle)
    return jobs.start_snapshot_job(
        jobs.SnapshotRequest(
            format="files",
            s3_credentials={  # pyright: ignore[reportArgumentType]
                "access_key_id": "AKIA_ORIGINAL",
                "secret_access_key": "secret",
                "session_token": "token",
                "region": "us-west-2",
            },
            credentials_expire_at=(
                dt.datetime.now(dt.UTC) + dt.timedelta(seconds=3600)
                if refreshable
                else None
            ),
        )
    )


async def _drain(job_id: str, gate: asyncio.Event) -> None:
    gate.set()
    job = jobs.get_snapshot_job(job_id)
    assert job is not None and job.task is not None
    await job.task


async def test_unknown_job_is_404(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/data/snapshot/s3/credentials/no-such-job", json=_payload()
    )
    assert response.status_code == 404


async def test_non_refreshable_job_is_409(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not 200: the job is real but the push cannot take effect, and answering
    'fine' would let the caller believe its uploads are covered."""
    gate = asyncio.Event()
    job_id = _start_job(monkeypatch, refreshable=False, gate=gate)
    await asyncio.sleep(0)

    response = await client.post(
        f"/data/snapshot/s3/credentials/{job_id}", json=_payload()
    )

    assert response.status_code == 409
    assert "credentials_expire_at" in response.json()["detail"]
    await _drain(job_id, gate)


async def test_running_job_accepts_the_push(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()
    job_id = _start_job(monkeypatch, refreshable=True, gate=gate)
    await asyncio.sleep(0)

    response = await client.post(
        f"/data/snapshot/s3/credentials/{job_id}", json=_payload()
    )

    assert response.status_code == 200
    assert response.json()["status"] == "running"
    job = jobs.get_snapshot_job(job_id)
    assert job is not None and job.credentials is not None
    assert (await job.credentials._refresh())["access_key"] == "AKIA_REFRESHED"
    await _drain(job_id, gate)


async def test_stale_push_is_accepted_but_ignored(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry can deliver an older lease than one already applied. The route
    answers normally — there is nothing for the caller to do — but the live
    lease must not regress."""
    gate = asyncio.Event()
    job_id = _start_job(monkeypatch, refreshable=True, gate=gate)
    await asyncio.sleep(0)

    await client.post(
        f"/data/snapshot/s3/credentials/{job_id}",
        json=_payload("AKIA_NEWER", ttl=7200),
    )
    response = await client.post(
        f"/data/snapshot/s3/credentials/{job_id}",
        json=_payload("AKIA_OLDER", ttl=100),
    )

    assert response.status_code == 200
    job = jobs.get_snapshot_job(job_id)
    assert job is not None and job.credentials is not None
    assert (await job.credentials._refresh())["access_key"] == "AKIA_NEWER"
    await _drain(job_id, gate)


async def test_malformed_body_is_rejected(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()
    job_id = _start_job(monkeypatch, refreshable=True, gate=gate)
    await asyncio.sleep(0)

    response = await client.post(
        f"/data/snapshot/s3/credentials/{job_id}", json={"expires_at": "not-a-date"}
    )

    assert response.status_code == 422
    await _drain(job_id, gate)
