"""``POST /data/lifecycle-hooks`` runs one phase of hooks and fails on any failure."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from runner.data.router import router as data_router

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(data_router, prefix="/data")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://env.local") as c:
        yield c


async def test_runs_every_hook_and_reports_timings(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/data/lifecycle-hooks",
        json={
            "phase": "stop",
            "hooks": [
                {"name": "svc_a", "command": "true"},
                {"name": "svc_b", "command": "echo stopped"},
            ],
        },
    )
    assert response.status_code == 200
    assert sorted(t["name"] for t in response.json()["hook_timings"]) == [
        "svc_a",
        "svc_b",
    ]


async def test_a_failing_hook_is_a_500(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/data/lifecycle-hooks",
        json={"phase": "stop", "hooks": [{"name": "svc_a", "command": "exit 3"}]},
    )
    assert response.status_code == 500
    assert "svc_a" in response.json()["detail"]
