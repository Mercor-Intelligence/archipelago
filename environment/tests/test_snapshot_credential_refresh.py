"""Tests for mid-harvest S3 credential rotation.

The sandbox uploads with a scoped STS lease that role chaining caps at 3600s,
minted before the harvest starts. A multi-GiB upload outlives it and dies on
its remaining PUTs with the work already done — and raising the TTL only moves
that ceiling, because payloads keep growing. So the lease is rotatable in
place: the caller re-mints and pushes to
``POST /data/snapshot/s3/credentials/{job_id}`` while the upload runs.

Covers the holder (``runner.utils.s3.RefreshableS3Credentials``), the registry
entry point (``runner.data.snapshot.jobs.refresh_snapshot_job_credentials``),
and the botocore wiring that makes a push actually reach the signer.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest
from aiobotocore.credentials import AioRefreshableCredentials
from pydantic import SecretStr

from runner.data.snapshot import jobs
from runner.data.snapshot.models import SnapshotFilesResult, SnapshotRequest
from runner.utils.s3 import (
    _LEASE_GUARD_SECONDS,
    RefreshableS3Credentials,
    S3Credentials,
    SnapshotCredentialsExpired,
    get_s3_client,
)

pytestmark = pytest.mark.asyncio


def _creds(key: str = "AKIA_ONE") -> S3Credentials:
    return S3Credentials(
        access_key_id=key,
        secret_access_key=SecretStr(f"secret-{key}"),
        session_token=SecretStr(f"token-{key}"),
        region="us-west-2",
    )


def _in(seconds: float) -> dt.datetime:
    return dt.datetime.now(dt.UTC) + dt.timedelta(seconds=seconds)


# ── The holder ───────────────────────────────────────────────────────


class TestRefreshableS3Credentials:
    async def test_update_swaps_credentials_and_extends_expiry(self) -> None:
        holder = RefreshableS3Credentials(_creds("AKIA_ONE"), _in(600))
        later = _in(4200)

        await holder.update(_creds("AKIA_TWO"), later)

        assert holder.refresh_count == 1
        assert holder.expires_at == later
        assert (await holder._refresh())["access_key"] == "AKIA_TWO"

    async def test_update_ignores_a_lease_that_is_not_newer(self) -> None:
        """Retries and out-of-order delivery are both possible on the caller's
        side; replacing a live lease with a staler one is worse than nothing."""
        original_expiry = _in(3600)
        holder = RefreshableS3Credentials(_creds("AKIA_ONE"), original_expiry)

        await holder.update(_creds("AKIA_STALE"), _in(120))

        assert holder.refresh_count == 0
        assert holder.expires_at == original_expiry
        assert (await holder._refresh())["access_key"] == "AKIA_ONE"

    async def test_naive_expiry_is_treated_as_utc(self) -> None:
        """botocore compares expiry against an aware now(); a naive datetime
        that slipped through would raise on the first comparison."""
        naive = dt.datetime.now() + dt.timedelta(seconds=3600)  # noqa: DTZ005
        holder = RefreshableS3Credentials(_creds(), naive)

        assert holder.expires_at.tzinfo is not None

    async def test_refresh_reports_empty_token_when_absent(self) -> None:
        holder = RefreshableS3Credentials(
            S3Credentials(
                access_key_id="AKIA_NO_TOKEN",
                secret_access_key=SecretStr("secret"),
                session_token=None,
                region="us-west-2",
            ),
            _in(3600),
        )
        assert (await holder._refresh())["token"] == ""

    async def test_refresh_raises_a_typed_error_once_the_lease_is_spent(self) -> None:
        """botocore raises its own opaque RuntimeError the moment it notices an
        expired lease during a mandatory refresh, and no classifier can safely
        recognise that. Failing first keeps the error typed and retryable."""
        holder = RefreshableS3Credentials(_creds(), _in(1))

        with pytest.raises(SnapshotCredentialsExpired):
            await holder._refresh()

    async def test_refresh_succeeds_while_the_lease_still_has_room(self) -> None:
        """The guard must not shorten a lease that is merely inside botocore's
        refresh window — that window is 15 minutes wide and the callback fires
        on every request through it."""
        holder = RefreshableS3Credentials(_creds(), _in(60))

        assert (await holder._refresh())["access_key"] == "AKIA_ONE"

    async def test_a_push_clears_the_expired_state(self) -> None:
        holder = RefreshableS3Credentials(_creds("AKIA_ONE"), _in(1))
        with pytest.raises(SnapshotCredentialsExpired):
            await holder._refresh()

        await holder.update(_creds("AKIA_TWO"), _in(3600))

        assert (await holder._refresh())["access_key"] == "AKIA_TWO"

    async def test_pushed_lease_reaches_the_signer(self) -> None:
        """The point of the whole mechanism: a push mid-upload must change what
        subsequent requests are signed with, not just what the holder stores."""
        holder = RefreshableS3Credentials(_creds("AKIA_ONE"), _in(3600))

        async with get_s3_client(holder) as s3:
            signer_creds = s3.meta.client._request_signer._credentials  # pyright: ignore[reportAttributeAccessIssue]
            assert (
                await signer_creds.get_frozen_credentials()
            ).access_key == "AKIA_ONE"

            await holder.update(_creds("AKIA_TWO"), _in(7200))

            # Forcing a refresh is what botocore does on its own once the lease
            # nears expiry; here we drive it directly so the test does not turn
            # on botocore's advisory-window timing.
            await signer_creds._protected_refresh(is_mandatory=False)
            assert (
                await signer_creds.get_frozen_credentials()
            ).access_key == "AKIA_TWO"


class TestLeaseGuardAssumptions:
    """Pin what the lease guard borrows from aiobotocore.

    `_refresh` raises a few seconds before expiry so the error stays typed and
    retryable instead of surfacing as botocore's opaque "still expired"
    RuntimeError. That only works while botocore keeps calling the refresh hook
    well before the lease actually lapses, which is an internal detail of a
    pinned dependency — so assert it here rather than discover it in a harvest.
    """

    async def test_botocore_asks_for_a_refresh_long_before_expiry(self) -> None:
        # Both windows are minutes wide; the guard is seconds. If a bump ever
        # shrank them below the guard, the hook could stop being consulted in
        # time and botocore would win the race with its untyped error.
        # getattr: these are private to aiobotocore, which is the point — the
        # coupling is real and this is what pins it.
        advisory = getattr(AioRefreshableCredentials, "_advisory_refresh_timeout", 0)
        mandatory = getattr(AioRefreshableCredentials, "_mandatory_refresh_timeout", 0)
        assert advisory > _LEASE_GUARD_SECONDS
        assert mandatory > _LEASE_GUARD_SECONDS

    async def test_guard_leaves_a_usable_lease(self) -> None:
        """The guard must stay far below a real lease (3600s) or it would start
        rejecting credentials that have most of their life left."""
        assert 0 < _LEASE_GUARD_SECONDS < 60

    async def test_expiry_metadata_round_trips_through_botocore(self) -> None:
        """`expiry_time` is handed over as an ISO string; botocore parses it
        itself. A format it cannot read would silently look already-expired."""
        holder = RefreshableS3Credentials(_creds(), _in(3600))
        creds = holder.as_botocore_credentials()

        assert not creds.refresh_needed(refresh_in=0)


# ── The registry entry point ─────────────────────────────────────────


def _running_job_request(*, refreshable: bool) -> SnapshotRequest:
    return SnapshotRequest(
        format="files",
        s3_credentials=_creds(),
        credentials_expire_at=_in(3600) if refreshable else None,
    )


class TestRefreshSnapshotJobCredentials:
    async def test_unknown_job_returns_none(self) -> None:
        assert (
            await jobs.refresh_snapshot_job_credentials(
                "no-such-job", _creds(), _in(3600)
            )
            is None
        )

    async def test_job_without_expiry_is_not_refreshable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Omitting `credentials_expire_at` means the caller never opted in, so
        a push would silently do nothing — say so instead."""
        gate = asyncio.Event()

        async def fake_handle(**_kwargs: Any) -> SnapshotFilesResult:
            await gate.wait()
            return SnapshotFilesResult(
                snapshot_id="snap_1", files_uploaded=0, total_bytes=0
            )

        monkeypatch.setattr(jobs, "handle_snapshot_s3_files", fake_handle)
        job_id = jobs.start_snapshot_job(_running_job_request(refreshable=False))
        await asyncio.sleep(0)

        with pytest.raises(jobs.SnapshotCredentialsNotRefreshable):
            await jobs.refresh_snapshot_job_credentials(job_id, _creds(), _in(3600))

        gate.set()
        job = jobs.get_snapshot_job(job_id)
        assert job is not None and job.task is not None
        await job.task

    async def test_running_job_takes_the_push(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gate = asyncio.Event()
        seen: dict[str, Any] = {}

        async def fake_handle(**kwargs: Any) -> SnapshotFilesResult:
            seen["credentials"] = kwargs["s3_credentials"]
            await gate.wait()
            return SnapshotFilesResult(
                snapshot_id="snap_1", files_uploaded=0, total_bytes=0
            )

        monkeypatch.setattr(jobs, "handle_snapshot_s3_files", fake_handle)
        job_id = jobs.start_snapshot_job(_running_job_request(refreshable=True))
        await asyncio.sleep(0)

        # The handler must have been handed the *holder*, not a frozen copy —
        # otherwise the push below could never reach the in-flight upload.
        assert isinstance(seen["credentials"], RefreshableS3Credentials)

        job = await jobs.refresh_snapshot_job_credentials(
            job_id, _creds("AKIA_REFRESHED"), _in(7200)
        )

        assert job is not None
        assert job.credentials is not None
        assert job.credentials.refresh_count == 1
        assert (await job.credentials._refresh())["access_key"] == "AKIA_REFRESHED"

        gate.set()
        assert job.task is not None
        await job.task

    async def test_push_after_the_harvest_finishes_is_a_no_op(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The push races the harvest's own completion. Losing that race is the
        good outcome, not an error."""

        async def fake_handle(**_kwargs: Any) -> SnapshotFilesResult:
            return SnapshotFilesResult(
                snapshot_id="snap_1", files_uploaded=1, total_bytes=1
            )

        monkeypatch.setattr(jobs, "handle_snapshot_s3_files", fake_handle)
        job_id = jobs.start_snapshot_job(_running_job_request(refreshable=True))
        job = jobs.get_snapshot_job(job_id)
        assert job is not None and job.task is not None
        await job.task
        assert job.status == "done"

        refreshed = await jobs.refresh_snapshot_job_credentials(
            job_id, _creds("AKIA_LATE"), _in(7200)
        )

        assert refreshed is job
        assert job.credentials is not None
        assert job.credentials.refresh_count == 0


async def test_credentials_stay_frozen_without_an_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Back-compat: a caller that does not send `credentials_expire_at` gets the
    previous behavior — plain credentials baked into the session."""
    seen: dict[str, Any] = {}

    async def fake_handle(**kwargs: Any) -> SnapshotFilesResult:
        seen["credentials"] = kwargs["s3_credentials"]
        return SnapshotFilesResult(
            snapshot_id="snap_1", files_uploaded=0, total_bytes=0
        )

    monkeypatch.setattr(jobs, "handle_snapshot_s3_files", fake_handle)
    job_id = jobs.start_snapshot_job(_running_job_request(refreshable=False))
    job = jobs.get_snapshot_job(job_id)
    assert job is not None and job.task is not None
    await job.task

    assert job.credentials is None
    assert isinstance(seen["credentials"], S3Credentials)
