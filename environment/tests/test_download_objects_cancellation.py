"""A failed object download must not leave its siblings running."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from runner.data.populate import utils as utils_mod

_CANCELLED: list[str] = []


class _FakeSummary:
    def __init__(self, key: str) -> None:
        self.key = key
        self.size = 1


async def _aiter(summaries: list[_FakeSummary]) -> Any:
    for summary in summaries:
        yield summary


class _FakeObjectsCollection:
    def __init__(self, summaries: list[_FakeSummary]) -> None:
        self._summaries = summaries

    def filter(self, **_kw: Any) -> Any:
        return _aiter(self._summaries)


class _FakeBucket:
    def __init__(self, summaries: list[_FakeSummary]) -> None:
        self.objects = _FakeObjectsCollection(summaries)


class _FakeResource:
    def __init__(self, summaries: list[_FakeSummary]) -> None:
        self._bucket = _FakeBucket(summaries)
        self.meta = type("_Meta", (), {"client": object()})()

    async def Bucket(self, _name: str) -> _FakeBucket:  # noqa: N802 — aioboto3's name
        return self._bucket


class _FakeS3ClientCM:
    def __init__(self, summaries: list[_FakeSummary]) -> None:
        self._resource = _FakeResource(summaries)

    async def __aenter__(self) -> _FakeResource:
        return self._resource

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


async def _boom_or_hang(*, obj_summary: _FakeSummary, **_kw: Any) -> None:
    """One object fails immediately; the rest would run long past the failure."""
    if obj_summary.key.endswith("boom"):
        raise RuntimeError("object download failed")
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        _CANCELLED.append(obj_summary.key)
        raise


@pytest.mark.asyncio
async def test_a_failed_object_cancels_its_siblings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _CANCELLED.clear()
    summaries = [
        _FakeSummary("worlds/s1/a"),
        _FakeSummary("worlds/s1/boom"),
        _FakeSummary("worlds/s1/b"),
    ]
    monkeypatch.setattr(
        utils_mod, "get_s3_client", lambda credentials=None: _FakeS3ClientCM(summaries)
    )
    monkeypatch.setattr(utils_mod, "_download_single_object", _boom_or_hang)

    with pytest.raises(HTTPException):
        await asyncio.wait_for(
            utils_mod.download_objects(
                bucket="bucket",
                key="worlds/s1/",
                subsystem="",
                dest_root=str(tmp_path),
            ),
            timeout=5,
        )

    assert sorted(_CANCELLED) == ["worlds/s1/a", "worlds/s1/b"]
