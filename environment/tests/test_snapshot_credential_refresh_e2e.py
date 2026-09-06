"""End-to-end proof that a pushed lease rescues an in-flight upload.

The unit tests assert the holder stores what was pushed and that botocore
resolves against it. Neither shows the thing that actually failed in
production: an upload already streaming parts to S3 when its credentials
lapse. This drives the whole chain instead —

    POST /data/snapshot/s3/start  ->  handle_snapshot_s3_files
      -> aioboto3 multipart upload  ->  an S3 that REJECTS expired tokens
    POST /data/snapshot/s3/credentials/{job_id}  (mid-upload)
      -> the remaining parts sign with the new lease and the harvest completes

The S3 here is a real HTTP server that reads ``X-Amz-Security-Token`` off every
request and answers ``ExpiredToken`` once that token is past its expiry, so the
credential deadline is enforced by the peer exactly as S3 enforces it — not by
a mock we told when to fail.

Leases are 10s and parts are delayed server-side so the upload is guaranteed to
outlive the lease it started with. That makes these the slowest tests in the
file by design: the failure being reproduced is a deadline, and a deadline
cannot be faked without also faking the thing under test.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

# aiohttp is not declared in the dev group on purpose: it is a hard
# requirement of aiobotocore, which this package depends on directly and
# which the production S3 path imports, so it is as guaranteed as aioboto3
# itself. Declaring it would mean re-locking, and `[tool.uv] exclude-newer
# = "7 days"` makes any re-lock move the resolution window for every
# package — too much churn for a hygiene nicety.
from aiohttp import web
from fastapi import FastAPI

from runner.data.router import router as data_router
from runner.data.snapshot import main as snapshot_main

pytestmark = pytest.mark.asyncio

LEASE_SECONDS = 10.0
_PART_SIZE = 256 * 1024
_PART_DELAY_S = 1.5
_PAYLOAD_PARTS = 8  # 8 x 1.5s = 12s of upload against a 10s lease


class FakeS3:
    """Minimal S3 multipart endpoint that enforces credential expiry.

    Only the operations an upload actually issues are implemented. Every one of
    them goes through :meth:`_authorize` first, which is the whole point: the
    server decides a request is unauthorized because the token it carries has
    expired, the same way the real bucket did at 01:46 on the trajectory this
    fixes.
    """

    def __init__(self) -> None:
        self.leases: dict[str, dt.datetime] = {}
        self.tokens_seen: list[str] = []
        self.rejected: list[str] = []
        self.parts_stored = 0
        self.completed: list[str] = []
        self.part_delay_s = 0.0

    def grant(self, token: str, ttl_seconds: float) -> dt.datetime:
        expiry = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl_seconds)
        self.leases[token] = expiry
        return expiry

    def _authorize(self, request: web.Request) -> web.Response | None:
        token = request.headers.get("X-Amz-Security-Token", "")
        self.tokens_seen.append(token)
        expiry = self.leases.get(token)
        if expiry is None or dt.datetime.now(dt.UTC) >= expiry:
            self.rejected.append(token)
            return web.Response(
                status=400,
                content_type="application/xml",
                text=(
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    "<Error><Code>ExpiredToken</Code>"
                    "<Message>The provided token has expired.</Message>"
                    "</Error>"
                ),
            )
        return None

    async def handle(self, request: web.Request) -> web.Response:
        denied = self._authorize(request)
        if denied is not None:
            return denied

        query = request.query
        if request.method == "POST" and "uploads" in query:
            return web.Response(
                content_type="application/xml",
                text=(
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    "<InitiateMultipartUploadResult>"
                    "<Bucket>snapshots</Bucket><Key>k</Key>"
                    "<UploadId>upload-1</UploadId>"
                    "</InitiateMultipartUploadResult>"
                ),
            )
        if request.method == "PUT" and "partNumber" in query:
            await request.read()
            if self.part_delay_s:
                await asyncio.sleep(self.part_delay_s)
            self.parts_stored += 1
            return web.Response(headers={"ETag": f'"part-{query["partNumber"]}"'})
        if request.method == "POST" and "uploadId" in query:
            await request.read()
            self.completed.append(request.path)
            return web.Response(
                content_type="application/xml",
                text=(
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    "<CompleteMultipartUploadResult>"
                    "<Bucket>snapshots</Bucket><Key>k</Key>"
                    '<ETag>"final"</ETag>'
                    "</CompleteMultipartUploadResult>"
                ),
            )
        if request.method == "DELETE":
            return web.Response(status=204)
        # Plain PutObject (small files).
        await request.read()
        return web.Response(headers={"ETag": '"single"'})


# loop_scope pinned to the test: this suite sets
# asyncio_default_fixture_loop_scope="session", which would leave the server
# running on a loop nobody drives during the test — every request would hang
# on an accepted-but-unserviced socket rather than fail.
@pytest_asyncio.fixture(loop_scope="function")
async def s3() -> AsyncGenerator[FakeS3]:
    fake = FakeS3()
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    previous = os.environ.get("AWS_ENDPOINT_URL_S3")
    # Redirect botocore at the local endpoint without touching production code.
    os.environ["AWS_ENDPOINT_URL_S3"] = f"http://127.0.0.1:{port}"
    try:
        yield fake
    finally:
        if previous is None:
            os.environ.pop("AWS_ENDPOINT_URL_S3", None)
        else:
            os.environ["AWS_ENDPOINT_URL_S3"] = previous
        await runner.cleanup()


@pytest_asyncio.fixture(loop_scope="function")
async def client() -> AsyncGenerator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(data_router, prefix="/data")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://env.local"
    ) as c:
        yield c


@pytest.fixture
def payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One file big enough to span the lease, wired in as the snapshot's content.

    Part size is shrunk to keep the byte count small — the test needs many
    *parts* spread over time, not many bytes.
    """
    from boto3.s3.transfer import TransferConfig

    target = tmp_path / "workspace.db"
    target.write_bytes(b"\0" * (_PART_SIZE * _PAYLOAD_PARTS))

    monkeypatch.setattr(snapshot_main, "_MULTIPART_THRESHOLD", _PART_SIZE)
    monkeypatch.setattr(
        snapshot_main,
        "_transfer_config_for",
        lambda _size: TransferConfig(
            multipart_threshold=_PART_SIZE,
            multipart_chunksize=_PART_SIZE,
            io_chunksize=_PART_SIZE,
            max_concurrency=1,  # serial, so elapsed time is predictable
            max_io_queue=2,
        ),
    )
    monkeypatch.setattr(
        snapshot_main,
        "_collect_subsystem_files",
        lambda _subsystems, prefix, exclude_globs=None: [
            (str(target), f"{prefix}/.apps_data/foundry_google_workspace/workspace.db")
        ],
    )

    # A lapsed lease is (correctly) retryable, so the no-push case would
    # otherwise spend the full multi-pass retry budget re-streaming parts
    # before giving up. Bound it: exhaustion is what these tests need to
    # observe, and the retry policy itself is covered in
    # test_snapshot_upload_retry.py.
    monkeypatch.setattr(snapshot_main, "_UPLOAD_MAX_RETRIES", 2)
    monkeypatch.setattr(snapshot_main, "_UPLOAD_RETRY_PASSES", 1)

    class _StubCoordinator:
        async def finish_actions(self) -> None:
            return None

    monkeypatch.setattr(snapshot_main, "get_coordinator", lambda: _StubCoordinator())
    return target


def _start_body(token: str, expiry: dt.datetime) -> dict[str, Any]:
    return {
        "format": "files",
        "snapshot_id": "snap_e2e",
        "snapshot_zip_enabled": False,
        "s3_credentials": {
            "access_key_id": f"AKIA_{token}",
            "secret_access_key": "secret",
            "session_token": token,
            "region": "us-west-2",
        },
        "credentials_expire_at": expiry.isoformat(),
    }


async def _await_parts(s3: FakeS3, at_least: int, timeout_s: float = 30.0) -> None:
    """Block until the upload is demonstrably mid-flight.

    Waiting a fixed number of seconds instead would tie the test to wall clock
    on a runner executing a dozen other suites in parallel. The property that
    matters is "parts are already streaming", so wait for exactly that.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if s3.parts_stored >= at_least:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"upload never reached {at_least} part(s) (stored {s3.parts_stored})"
    )


async def _await_terminal(
    client: httpx.AsyncClient, job_id: str, timeout_s: float = 45.0
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/data/snapshot/s3/status/{job_id}")
        body = response.json()
        if body["status"] != "running":
            return body
        await asyncio.sleep(0.25)
    raise AssertionError(f"snapshot job {job_id} never reached a terminal state")


async def test_upload_dies_when_the_lease_is_never_refreshed(
    client: httpx.AsyncClient, s3: FakeS3, payload: Path
) -> None:
    """The control. Without a push, a 10s lease kills a 12s upload.

    This is the production failure reproduced end to end. It also proves the
    harness has teeth — if this passed, the success case below would prove
    nothing.
    """
    s3.part_delay_s = _PART_DELAY_S
    expiry = s3.grant("token-initial", LEASE_SECONDS)

    started = await client.post(
        "/data/snapshot/s3/start", json=_start_body("token-initial", expiry)
    )
    job_id = started.json()["job_id"]

    result = await _await_terminal(client, job_id)

    assert result["status"] == "error"
    assert not s3.completed, "the multipart upload must not have completed"
    # Some parts DID go out before the lease died — this is a mid-flight
    # expiry, not a request that never got off the ground.
    assert 0 < s3.parts_stored < _PAYLOAD_PARTS


async def test_pushed_lease_rescues_an_upload_already_in_flight(
    client: httpx.AsyncClient, s3: FakeS3, payload: Path
) -> None:
    """The fix. Same 10s lease, same 12s upload — but refreshed at 5s.

    The upload is already streaming parts when the new lease arrives, so this
    exercises the property the whole design rests on: botocore re-resolves
    credentials per request, so parts issued after the swap carry the new
    token while the transfer itself continues uninterrupted.
    """
    s3.part_delay_s = _PART_DELAY_S
    expiry = s3.grant("token-initial", LEASE_SECONDS)

    started = await client.post(
        "/data/snapshot/s3/start", json=_start_body("token-initial", expiry)
    )
    job_id = started.json()["job_id"]

    # Push once parts are demonstrably streaming and the original lease is
    # still live — i.e. squarely mid-upload, which is the case that failed in
    # production and the only one worth proving.
    await _await_parts(s3, at_least=2)
    assert dt.datetime.now(dt.UTC) < expiry, "push must precede the lease lapsing"
    refreshed_expiry = s3.grant("token-refreshed", 120.0)
    pushed = await client.post(
        f"/data/snapshot/s3/credentials/{job_id}",
        json={
            "s3_credentials": {
                "access_key_id": "AKIA_token-refreshed",
                "secret_access_key": "secret",
                "session_token": "token-refreshed",
                "region": "us-west-2",
            },
            "expires_at": refreshed_expiry.isoformat(),
        },
    )
    assert pushed.status_code == 200

    result = await _await_terminal(client, job_id)

    assert result["status"] == "done", result.get("error")
    assert result["result"]["files_uploaded"] == 1
    assert s3.completed, "the multipart upload should have completed"
    # The upload genuinely spanned the swap: parts went out under both leases.
    assert "token-initial" in s3.tokens_seen
    assert "token-refreshed" in s3.tokens_seen
    assert s3.parts_stored == _PAYLOAD_PARTS
