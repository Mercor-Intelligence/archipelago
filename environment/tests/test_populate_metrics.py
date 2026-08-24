"""Regression tests for the byte totals reported by an S3 populate.

`ObjectSummary.size` is a coroutine property under aioboto3. Reading it without
awaiting yields a truthy coroutine that is never an `int`, so the byte total
silently summed to 0: `studio.trajectory.populate_download_bytes` was never
emitted (it is guarded on `total_bytes > 0`) and every populate was tagged
`snapshot_size_bucket:lt500m` regardless of size.

The doubles here model `size` the way aioboto3 actually exposes it. The previous
doubles handed it over as a plain `int`, which is why a green suite never saw
this.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from runner.data.populate import utils

_SIX_HUNDRED_MB = 600_000_000


class _AsyncSizeSummary:
    """An S3 object summary whose `size` is awaitable, as aioboto3 exposes it."""

    def __init__(self, key: str, size: int) -> None:
        self.key = key
        self._size = size

    @property
    async def size(self) -> int:
        return self._size


class _Body:
    async def read(self) -> bytes:
        return b"sqlite"


class _S3Client:
    async def get_object(self, **_kwargs: object) -> dict[str, _Body]:
        return {"Body": _Body()}


class _Objects:
    def __init__(self, objs: list[_AsyncSizeSummary]) -> None:
        self.objs = objs

    async def filter(self, **_kwargs: object) -> AsyncIterator[_AsyncSizeSummary]:
        for obj in self.objs:
            yield obj


class _Bucket:
    def __init__(self, objs: list[_AsyncSizeSummary]) -> None:
        self.objects = _Objects(objs)


class _S3Resource:
    def __init__(self, objs: list[_AsyncSizeSummary]) -> None:
        self.objs = objs
        self.meta = SimpleNamespace(client=_S3Client())

    async def Bucket(self, _name: str) -> _Bucket:
        return _Bucket(self.objs)


class _S3Context:
    def __init__(self, objs: list[_AsyncSizeSummary]) -> None:
        self.resource = _S3Resource(objs)

    async def __aenter__(self) -> _S3Resource:
        return self.resource

    async def __aexit__(self, *_args: object) -> None:
        return None


class _RecordDistribution:
    """Captures `distribution()` calls instead of shipping them to Datadog."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float, list[str]]] = []

    def __call__(
        self, metric: str, value: float, tags: list[str] | None = None
    ) -> None:
        self.calls.append((metric, value, tags or []))

    def value_for(self, metric: str) -> float | None:
        for name, value, _tags in self.calls:
            if name == metric:
                return value
        return None

    def tags_for(self, metric: str) -> list[str]:
        for name, _value, tags in self.calls:
            if name == metric:
                return tags
        return []


class _WriteEmptyFile:
    """Stands in for the range download: makes the file, transfers nothing."""

    async def __call__(self, *, target_path: str, **_kwargs: object) -> None:
        with open(target_path, "wb") as fh:
            fh.write(b"")


@pytest.mark.asyncio
async def test_object_size_bytes_resolves_the_aioboto3_coroutine_property() -> None:
    summary = _AsyncSizeSummary(key="worlds/snap_1/filesystem/a.bin", size=1234)
    assert await utils.object_size_bytes(summary) == 1234


@pytest.mark.asyncio
async def test_object_size_bytes_tolerates_a_plain_int_and_a_missing_size() -> None:
    assert await utils.object_size_bytes(SimpleNamespace(key="k", size=7)) == 7
    assert await utils.object_size_bytes(SimpleNamespace(key="k")) == 0


@pytest.mark.asyncio
async def test_populate_reports_real_bytes_and_size_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The download metrics must describe the snapshot that was actually pulled.

    Before the fix this emitted no `populate_download_bytes` at all and tagged a
    600 MB snapshot `lt500m`.
    """
    filesystem_root = tmp_path / "filesystem"
    filesystem_root.mkdir()
    monkeypatch.setattr(utils, "FILESYSTEM_ROOT", str(filesystem_root))

    key = "worlds/snap_123/filesystem"
    objs = [
        _AsyncSizeSummary(f"{key}/big.bin", _SIX_HUNDRED_MB - 1000),
        _AsyncSizeSummary(f"{key}/small.bin", 1000),
    ]
    recorder = _RecordDistribution()
    monkeypatch.setattr(utils, "get_s3_client", lambda **_kwargs: _S3Context(objs))
    monkeypatch.setattr(utils, "get_s5cmd_downloader", lambda _backend: None)
    monkeypatch.setattr(utils, "distribution", recorder)
    monkeypatch.setattr(utils, "_download_with_ranges", _WriteEmptyFile())

    count = await utils.download_objects(
        bucket="snapshots",
        key=key,
        subsystem=str(filesystem_root).lstrip("/"),
    )

    assert count == 2
    assert recorder.value_for("studio.trajectory.populate_download_bytes") == float(
        _SIX_HUNDRED_MB
    )
    assert recorder.value_for("studio.trajectory.populate_download_files") == 2.0
    assert "snapshot_size_bucket:500m-5g" in recorder.tags_for(
        "studio.trajectory.populate_download_seconds"
    )
