"""Regression tests for writable files populated into the shared filesystem."""

import os
import stat
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from runner.data.populate import utils


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None


class _NoopAsync:
    async def stop(self) -> None:
        return None


class _Body:
    async def read(self) -> bytes:
        return b"sqlite"


class _S3Client:
    async def get_object(self, **_kwargs: object) -> dict[str, _Body]:
        return {"Body": _Body()}


class _Objects:
    def __init__(self, obj: SimpleNamespace) -> None:
        self.obj = obj

    async def filter(self, **_kwargs: object) -> AsyncIterator[SimpleNamespace]:
        yield self.obj


class _Bucket:
    def __init__(self, obj: SimpleNamespace) -> None:
        self.objects = _Objects(obj)


class _S3Resource:
    def __init__(self, obj: SimpleNamespace) -> None:
        self.obj = obj
        self.meta = SimpleNamespace(client=_S3Client())

    async def Bucket(self, _name: str) -> _Bucket:
        return _Bucket(self.obj)


class _S3Context:
    def __init__(self, obj: SimpleNamespace) -> None:
        self.resource = _S3Resource(obj)

    async def __aenter__(self) -> _S3Resource:
        return self.resource

    async def __aexit__(self, *_args: object) -> None:
        return None


class _S5cmdDownloader:
    async def download_prefix(
        self, _bucket: str, _key: str, destination: str, **_kwargs
    ) -> None:
        target_dir = os.path.join(destination, "faslr")
        os.makedirs(target_dir, mode=0o700)
        target = os.path.join(target_dir, "faslr.db")
        with open(target, "wb") as f:
            f.write(b"sqlite")
        os.chmod(target_dir, 0o700)
        os.chmod(target, 0o600)


@pytest.mark.asyncio
async def test_boto3_download_makes_shared_filesystem_file_group_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filesystem_root = tmp_path / "filesystem"
    filesystem_root.mkdir(mode=0o770)
    os.chmod(filesystem_root, 0o2770)
    monkeypatch.setattr(utils, "FILESYSTEM_ROOT", str(filesystem_root))

    object_key = "worlds/snap_123/filesystem/faslr/faslr.db"
    await utils._download_single_object(
        obj_summary=SimpleNamespace(key=object_key, size=6),
        key="worlds/snap_123/filesystem",
        subsystem_root=str(filesystem_root),
        s3_client=_S3Client(),
        bucket_name="snapshots",
    )

    target = filesystem_root / "faslr" / "faslr.db"
    parent_mode = stat.S_IMODE(target.parent.stat().st_mode)
    file_mode = stat.S_IMODE(target.stat().st_mode)

    assert target.read_bytes() == b"sqlite"
    assert parent_mode & 0o2070 == 0o2070
    assert file_mode & 0o060 == 0o060
    assert target.parent.stat().st_gid == filesystem_root.stat().st_gid
    assert target.stat().st_gid == filesystem_root.stat().st_gid


@pytest.mark.asyncio
async def test_s5cmd_download_makes_shared_filesystem_file_group_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filesystem_root = tmp_path / "filesystem"
    filesystem_root.mkdir(mode=0o770)
    os.chmod(filesystem_root, 0o2770)
    monkeypatch.setattr(utils, "FILESYSTEM_ROOT", str(filesystem_root))

    key = "worlds/snap_123/filesystem"
    obj = SimpleNamespace(key=f"{key}/faslr/faslr.db", size=6)
    monkeypatch.setattr(utils, "get_s3_client", lambda **_kwargs: _S3Context(obj))
    monkeypatch.setattr(
        utils, "get_s5cmd_downloader", lambda _backend: _S5cmdDownloader()
    )
    monkeypatch.setattr(utils, "key_is_eligible", lambda _key: True)

    count = await utils.download_objects(
        bucket="snapshots",
        key=key,
        subsystem=str(filesystem_root).lstrip("/"),
        backend="s5cmd",
    )

    target = filesystem_root / "faslr" / "faslr.db"
    assert count == 1
    assert stat.S_IMODE(target.parent.stat().st_mode) & 0o2070 == 0o2070
    assert stat.S_IMODE(target.stat().st_mode) & 0o060 == 0o060


def test_permission_repair_is_targeted_and_idempotent(tmp_path: Path) -> None:
    filesystem_root = tmp_path / "filesystem"
    target_dir = filesystem_root / "nested"
    target_dir.mkdir(parents=True)
    target = target_dir / "data.db"
    target.write_bytes(b"db")
    sibling = filesystem_root / "leave-alone.txt"
    sibling.write_text("unchanged")

    os.chmod(filesystem_root, 0o2770)
    os.chmod(target_dir, 0o700)
    os.chmod(target, 0o600)
    os.chmod(sibling, 0o600)

    utils._make_shared_filesystem_path_writable(str(target), str(filesystem_root))
    first_dir_mode = stat.S_IMODE(target_dir.stat().st_mode)
    first_file_mode = stat.S_IMODE(target.stat().st_mode)
    utils._make_shared_filesystem_path_writable(str(target), str(filesystem_root))

    assert first_dir_mode == stat.S_IMODE(target_dir.stat().st_mode)
    assert first_file_mode == stat.S_IMODE(target.stat().st_mode)
    assert first_dir_mode & 0o2070 == 0o2070
    assert first_file_mode & 0o060 == 0o060
    assert stat.S_IMODE(sibling.stat().st_mode) == 0o600


@pytest.fixture
def _restore_umask() -> Iterator[None]:
    """`os.umask` is process-global, so a test that changes it must put it back or it
    leaks into every test that runs afterwards."""
    original = os.umask(0o022)
    try:
        yield
    finally:
        _ = os.umask(original)


@pytest.mark.asyncio
async def test_the_lifespan_sets_the_umask_that_makes_world_dirs_group_writable(
    monkeypatch: pytest.MonkeyPatch, _restore_umask: None
) -> None:
    """The runner sets its own umask, because three of the six launchers cannot (exec-form
    `CMD`, no shell). Pinning it here is what makes the six agree."""
    from runner import main as runner_main

    # The startup path validates the signing key and the shutdown path tears down
    # collaborators that do not exist in a unit test; neither is what this pins.
    monkeypatch.setattr(runner_main, "signing_enabled", lambda: True)
    monkeypatch.setattr(runner_main, "get_coordinator", lambda: _NoopAsync())
    monkeypatch.setattr(runner_main, "close_proxy_client", _noop)
    monkeypatch.setattr(runner_main, "shutdown_stateful_proxy", _noop)
    monkeypatch.setattr(runner_main, "get_mcp_lifespan_manager", lambda: None)
    monkeypatch.setattr(runner_main, "teardown_logger", _noop)

    os.umask(0o022)
    async with runner_main.lifespan(runner_main.app):
        # Read it back without disturbing it.
        current = os.umask(0o002)
    assert current == 0o002, f"lifespan left umask {current:04o}, expected 0002"


@pytest.mark.asyncio
async def test_a_world_dir_outside_the_filesystem_root_is_group_writable_under_0002(
    tmp_path: Path, _restore_umask: None
) -> None:
    """The consequence: nothing repairs modes outside `FILESYSTEM_ROOT`, so for a world
    directory under `/.apps_data` the umask alone decides whether the owning app can write
    there. Both arms asserted, so this fails if that stops being true in either
    direction."""
    apps_data_root = tmp_path / ".apps_data"
    apps_data_root.mkdir()
    app_dir = apps_data_root / "email"
    app_dir.mkdir()
    os.chown(app_dir, os.getuid(), os.getgid())
    os.chmod(app_dir, 0o2770)  # what the app image builds: setgid, group-writable

    async def _download_into(subdir: str) -> int:
        key = "worlds/snap_123/.apps_data"
        await utils._download_single_object(
            obj_summary=SimpleNamespace(key=f"{key}/email/{subdir}/store.db", size=6),
            key=key,
            subsystem_root=str(apps_data_root),
            s3_client=_S3Client(),
            bucket_name="snapshots",
        )
        return stat.S_IMODE((app_dir / subdir).stat().st_mode)

    os.umask(0o002)
    generous = await _download_into("attachments")
    os.umask(0o022)
    stingy = await _download_into("archive")

    assert generous & stat.S_IWGRP, (
        f"under umask 0002 a world directory landed {generous:04o} — the app's group "
        f"cannot write, which is the outage this setting exists to prevent"
    )
    assert not stingy & stat.S_IWGRP, (
        f"under umask 0022 a world directory landed {stingy:04o} — if this ever becomes "
        f"group-writable on its own, the umask is no longer what decides it and the "
        f"lifespan setting above is dead weight"
    )
