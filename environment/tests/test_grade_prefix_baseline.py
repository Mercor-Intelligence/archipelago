"""The prefix baseline: the route taken when a world has no prebuilt archive."""

from __future__ import annotations

import asyncio
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from runner import grade as grade_mod
from runner.grade import GradeRequest
from runner.utils.s3 import S3Credentials

_CREDS = S3Credentials(
    access_key_id="AK",
    secret_access_key=SecretStr("SK"),
    session_token=SecretStr("TOK"),
    region="us-west-2",
)


class _FakeDownload:
    """Stands in for `download_objects`, optionally materializing a tree."""

    def __init__(self, count: int = 1, layout: dict[str, str] | None = None) -> None:
        self.count = count
        self.layout = layout or {}
        self.calls = 0
        self.keys: list[str] = []

    async def __call__(
        self, *, dest_root: str | None = None, key: str = "", **_kw: Any
    ) -> int:
        self.calls += 1
        self.keys.append(key)
        if self.layout and dest_root is not None:
            for rel, content in self.layout.items():
                path = Path(dest_root) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
        return self.count


class _FakeUrlDownload:
    """Stands in for `_download_snapshot` (the presigned-URL route)."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def __call__(self, url: str, dest: Path) -> None:
        self.urls.append(url)
        with zipfile.ZipFile(dest, "w"):
            pass


_UNLINKED: list[str] = []
_REAL_UNLINK = Path.unlink


def _spy_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
    _UNLINKED.append(self.name)
    _REAL_UNLINK(self, *args, **kwargs)


_THREADED: list[str] = []
_REAL_TO_THREAD = asyncio.to_thread


async def _spy_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
    _THREADED.append(getattr(func, "__name__", repr(func)))
    return await _REAL_TO_THREAD(func, *args, **kwargs)


class _SlowPack:
    """A pack that runs long enough to still be working when the grade is cancelled."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.finished = False
        self.tree_present_at_end = False

    def __call__(self, root: Path, _dest: Path) -> None:
        self.started.set()
        time.sleep(0.3)
        self.tree_present_at_end = root.exists()
        self.finished = True


def _request(**over: Any) -> GradeRequest:
    base: dict[str, Any] = {
        "grading_settings_json": "{}",
        "verifiers_json": "[]",
        "eval_configs_json": "[]",
        "scoring_config_json": "{}",
        "trajectory_json": "{}",
        "grading_run_id": "gr-1",
        "trajectory_id": "tr-1",
    }
    base.update(over)
    return GradeRequest(**base)


def _prefix_request(**over: Any) -> GradeRequest:
    base: dict[str, Any] = {
        "baseline_s3_bucket": "bucket",
        "baseline_s3_world_prefix": "worlds/snap-1/",
        "baseline_s3_credentials": _CREDS,
    }
    base.update(over)
    return _request(**base)


@pytest.mark.asyncio
async def test_refuses_when_the_prefix_lists_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """download_objects RETURNS 0 for an empty listing rather than raising, so the route"""
    monkeypatch.setattr(grade_mod, "download_objects", _FakeDownload(count=0))
    dest = tmp_path / "b.zip"
    with pytest.raises(HTTPException) as err:
        await grade_mod._prefix_half(_prefix_request(), "worlds/snap-1/", dest)
    assert err.value.status_code == 409
    assert not dest.exists()


@pytest.mark.asyncio
async def test_entry_names_are_relative_to_the_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Must match build_snapshot_zip's `arcname = obj.key[len(prefix):]`, or a"""
    fake = _FakeDownload(
        count=2,
        layout={"filesystem/docs/a.txt": "a", ".apps_data/chat/m.json": "{}"},
    )
    monkeypatch.setattr(grade_mod, "download_objects", fake)
    dest = tmp_path / "b.zip"
    await grade_mod._prefix_half(_prefix_request(), "worlds/snap-1/", dest)
    with zipfile.ZipFile(dest) as zf:
        assert sorted(zf.namelist()) == [
            ".apps_data/chat/m.json",
            "filesystem/docs/a.txt",
        ]


@pytest.mark.asyncio
async def test_each_file_is_dropped_as_it_is_packed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The archive is ZIP_STORED, so keeping the tree alongside it peaks at ~2x the"""
    _UNLINKED.clear()
    fake = _FakeDownload(
        count=2, layout={"filesystem/a.txt": "a", ".apps_data/m.json": "{}"}
    )
    monkeypatch.setattr(grade_mod, "download_objects", fake)
    monkeypatch.setattr(Path, "unlink", _spy_unlink)
    await grade_mod._prefix_half(
        _prefix_request(), "worlds/snap-1/", tmp_path / "b.zip"
    )
    assert sorted(_UNLINKED) == ["a.txt", "m.json"]


@pytest.mark.asyncio
async def test_packing_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """/health shares this event loop. Walking and zipping a whole world inline would hold"""
    _THREADED.clear()
    fake = _FakeDownload(count=1, layout={"filesystem/a.txt": "a"})
    monkeypatch.setattr(grade_mod, "download_objects", fake)
    monkeypatch.setattr(grade_mod.asyncio, "to_thread", _spy_to_thread)
    await grade_mod._prefix_half(
        _prefix_request(), "worlds/snap-1/", tmp_path / "b.zip"
    )
    assert "_pack_baseline_tree" in _THREADED


@pytest.mark.asyncio
async def test_cancelling_a_grade_waits_for_the_packing_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cancellation cannot reach into a pool thread — it cancels the future while the"""
    slow = _SlowPack()
    monkeypatch.setattr(grade_mod, "download_objects", _FakeDownload(count=1))
    monkeypatch.setattr(grade_mod, "_pack_baseline_tree", slow)

    task = asyncio.ensure_future(
        grade_mod._prefix_half(_prefix_request(), "worlds/snap-1/", tmp_path / "b.zip")
    )
    await asyncio.to_thread(slow.started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert slow.finished is True
    assert slow.tree_present_at_end is True


def test_the_two_halves_are_armed_independently() -> None:
    """Each half resolves from its OWN prefix, and neither is merged here."""
    both = _prefix_request(baseline_s3_task_prefix="tasks/t-1/")
    assert (
        grade_mod._prefix_source(both, both.baseline_s3_world_prefix)
        == "worlds/snap-1/"
    )
    assert grade_mod._prefix_source(both, both.baseline_s3_task_prefix) == "tasks/t-1/"

    world_only = _prefix_request()
    assert (
        grade_mod._prefix_source(world_only, world_only.baseline_s3_task_prefix) is None
    )


@pytest.mark.asyncio
async def test_the_task_half_is_assembled_from_its_own_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The task overlay has the same two sources as the world half: a URL when Studio has an"""
    fake = _FakeDownload(count=1, layout={"filesystem/authored.txt": "task"})
    monkeypatch.setattr(grade_mod, "download_objects", fake)
    dest = tmp_path / "task.zip"
    await grade_mod._prefix_half(_prefix_request(), "tasks/t-1/", dest)
    with zipfile.ZipFile(dest) as zf:
        assert zf.namelist() == ["filesystem/authored.txt"]


@pytest.mark.asyncio
async def test_route_is_skipped_without_explicit_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without credentials get_s3_client would use the runner's AMBIENT identity, which"""
    fake = _FakeDownload()
    monkeypatch.setattr(grade_mod, "download_objects", fake)
    dest = tmp_path / "b.zip"
    await grade_mod._materialize_baseline(
        _request(
            baseline_s3_bucket="bucket", baseline_s3_world_prefix="worlds/snap-1/"
        ),
        dest,
    )
    assert fake.calls == 0
    with zipfile.ZipFile(dest) as zf:  # fell through to the empty-baseline branch
        assert zf.namelist() == []


@pytest.mark.asyncio
async def test_url_still_wins_over_the_prefix_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ordered last on purpose: nothing that works today may change route."""
    url_dl = _FakeUrlDownload()
    prefix_dl = _FakeDownload()
    monkeypatch.setattr(grade_mod, "_download_snapshot", url_dl)
    monkeypatch.setattr(grade_mod, "download_objects", prefix_dl)
    await grade_mod._materialize_baseline(
        _prefix_request(initial_snapshot_url="https://s3.amazonaws.com/a.tar.zst"),
        tmp_path / "b.zip",
    )
    assert url_dl.urls == ["https://s3.amazonaws.com/a.tar.zst"]
    assert prefix_dl.calls == 0


@pytest.mark.parametrize(
    "prefix", ["worlds/*/", "worlds/../secrets/", "", "worlds/snap?/"]
)
def test_unsafe_prefixes_are_refused(prefix: str) -> None:
    """The prefix arrives in the request, so a wildcard or traversal segment could list"""
    with pytest.raises(HTTPException) as err:
        grade_mod._validated_baseline_prefix(prefix)
    assert err.value.status_code == 400


def test_a_plain_prefix_is_normalised_with_a_trailing_slash() -> None:
    assert grade_mod._validated_baseline_prefix("worlds/snap-1") == "worlds/snap-1/"


@pytest.mark.asyncio
async def test_the_world_half_is_listed_with_the_normalized_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without the trailing slash the listing is wider than the snapshot root, so
    `worlds/snap-1` also matches `worlds/snap-10/` — another snapshot's objects."""
    fake = _FakeDownload(count=1, layout={"filesystem/a.txt": "a"})
    monkeypatch.setattr(grade_mod, "download_objects", fake)
    await grade_mod._materialize_baseline(
        _prefix_request(baseline_s3_world_prefix="worlds/snap-1"), tmp_path / "b.zip"
    )
    assert fake.keys == ["worlds/snap-1/"]


@pytest.mark.asyncio
async def test_an_empty_task_overlay_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ "No authored overlay" is a valid state, which populate and the async lane both treat
    as success. Writing nothing is the signal: _grade omits --task-snapshot."""
    monkeypatch.setattr(grade_mod, "download_objects", _FakeDownload(count=0))
    dest = tmp_path / "task.zip"
    await grade_mod._materialize_task_half(
        _prefix_request(baseline_s3_task_prefix="tasks/t-1/"), dest
    )
    assert not dest.exists()


@pytest.mark.asyncio
async def test_an_empty_world_baseline_still_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The asymmetry is the point: the seed fails closed, the overlay does not."""
    monkeypatch.setattr(grade_mod, "download_objects", _FakeDownload(count=0))
    with pytest.raises(HTTPException) as err:
        await grade_mod._materialize_baseline(_prefix_request(), tmp_path / "b.zip")
    assert err.value.status_code == 409


@pytest.mark.asyncio
async def test_a_listing_of_only_folder_markers_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """download_objects counts objects LISTED, so a prefix holding only zero-byte folder
    markers returns >0 while writing no usable file. The guard has to key on what was packed,
    or that world grades against an empty baseline."""
    monkeypatch.setattr(
        grade_mod, "download_objects", _FakeDownload(count=3, layout={})
    )
    dest = tmp_path / "b.zip"
    with pytest.raises(HTTPException) as err:
        await grade_mod._prefix_half(_prefix_request(), "worlds/snap-1/", dest)
    assert err.value.status_code == 409
    assert not dest.exists()


@pytest.mark.asyncio
async def test_a_marker_only_task_overlay_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        grade_mod, "download_objects", _FakeDownload(count=3, layout={})
    )
    dest = tmp_path / "task.zip"
    await grade_mod._prefix_half(
        _prefix_request(), "tasks/t-1/", dest, allow_empty=True
    )
    assert not dest.exists()


@pytest.mark.asyncio
async def test_preferring_local_falls_to_the_prefix_when_the_image_lacks_the_bake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The platform image, a stale image and a failed bake all land here. The URL is not
    consulted on this branch, so the prefix is what has to catch them."""
    fake = _FakeDownload(count=1, layout={"filesystem/a.txt": "a"})
    monkeypatch.setattr(grade_mod, "download_objects", fake)
    dest = tmp_path / "b.zip"
    await grade_mod._materialize_baseline(
        _prefix_request(
            prefer_local_baseline=True,
            initial_snapshot_local_id="c" * 32,
            initial_snapshot_url="https://s3.amazonaws.com/a.tar.zst",
        ),
        dest,
    )
    assert fake.keys == ["worlds/snap-1/"]
    with zipfile.ZipFile(dest) as zf:
        assert zf.namelist() == ["filesystem/a.txt"]


@pytest.mark.asyncio
async def test_preferring_local_refuses_rather_than_emptying(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A caller setting the flag has a world seed, so an empty baseline is never right —
    every seeded file would read as agent-created."""
    monkeypatch.setattr(grade_mod, "download_objects", _FakeDownload())
    with pytest.raises(HTTPException) as err:
        await grade_mod._materialize_baseline(
            _request(prefer_local_baseline=True, initial_snapshot_local_id="c" * 32),
            tmp_path / "b.zip",
        )
    assert err.value.status_code == 409
