"""Trajectory snapshots include the apps_data subsystem (VCA coordinator state)."""

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import HTTPException

from runner.data.snapshot import main as snapshot_main
from runner.utils.settings import get_settings

SnapshotS3Handler = Callable[..., Awaitable[Any]]


def test_file_snapshot_uploads_apps_data_subsystem() -> None:
    src = inspect.getsource(snapshot_main.handle_snapshot_s3_files)
    assert "APPS_DATA_SUBSYSTEM_NAME" in src or ".apps_data" in src


def test_settings_apps_data_names_coordinator_parent() -> None:
    assert get_settings().APPS_DATA_SUBSYSTEM_NAME == ".apps_data"


@pytest.mark.parametrize(
    "handler",
    [snapshot_main.handle_snapshot_s3, snapshot_main.handle_snapshot_s3_files],
)
@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "snap with spaces",
        "snap;rm -rf /",
        "../etc/passwd",
        "snap$id",
        "snap\nid",
        "snap.tar.gz",
        "nested/path/snap_1",
    ],
)
@pytest.mark.asyncio
async def test_snapshot_s3_handlers_reject_invalid_snapshot_id(
    handler: SnapshotS3Handler, bad_id: str
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await handler(snapshot_id=bad_id)
    assert exc_info.value.status_code == 400


@pytest.mark.parametrize(
    "handler",
    [snapshot_main.handle_snapshot_s3, snapshot_main.handle_snapshot_s3_files],
)
@pytest.mark.parametrize("good_id", ["snap_abc123", "snap-abc123"])
@pytest.mark.asyncio
async def test_snapshot_s3_handlers_accept_valid_snapshot_id(
    handler: SnapshotS3Handler, good_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid IDs pass the 400 gate; we sentinel-trip the next step to confirm."""

    class _Sentinel(Exception):
        pass

    class _StubCoordinator:
        async def finish_actions(self) -> None:
            raise _Sentinel()

    monkeypatch.setattr(snapshot_main, "get_coordinator", lambda: _StubCoordinator())
    with pytest.raises(_Sentinel):
        await handler(snapshot_id=good_id)
