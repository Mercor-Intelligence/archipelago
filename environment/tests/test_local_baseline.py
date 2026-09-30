"""The post-populate archive kept on the sandbox's own disk.

The archive is built there either way. Keeping it spares the grade a download,
and it then sits in a tree the model runs in as root for the whole agent loop,
so the grade checks it against what the runner recorded.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest
import zstandard
from fastapi import HTTPException

from runner import grade, grading_paths
from runner.data.snapshot import main as snapshot_main
from runner.data.snapshot.models import SnapshotFilesResult, SnapshotRequest


def _tar_zst(path: Path, members: dict[str, bytes]) -> None:
    """A real archive, so the transcode under test has something to read."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    path.write_bytes(zstandard.ZstdCompressor().compress(raw.getvalue()))


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point both writer and reader at a tmp work dir."""
    monkeypatch.setattr(grading_paths, "GRADE_WORK_DIR", str(tmp_path))
    return tmp_path


# ── the path builder ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    ["", "../etc", "a" * 31, "A" * 32, "0123456789abcdef0123456789abcdeg", "../" * 8],
)
def test_a_baseline_id_that_is_not_32_hex_is_refused(bad: str) -> None:
    """The id leaves the sandbox in a result body and comes back in a request,
    so it is caller text by the time it builds a path."""
    with pytest.raises(ValueError):
        _ = grading_paths.baseline_dir(bad)


def test_a_well_formed_id_lands_under_the_work_dir(work_dir: Path) -> None:
    got = grading_paths.baseline_dir("0" * 32)
    assert got.parent == work_dir.resolve()
    assert got.name == "baseline-" + "0" * 32


def test_the_capture_and_the_baseline_do_not_share_a_directory() -> None:
    """Both are named by a 32-hex id, so a capture id must not resolve onto a
    baseline's file or either could be read as the other."""
    same = "b" * 32
    assert grading_paths.capture_dir(same) != grading_paths.baseline_dir(same)


# ── keeping it ───────────────────────────────────────────────────────


def test_an_archive_within_the_cap_is_moved_and_reported(work_dir: Path) -> None:
    src = work_dir / "built.tar.zst"
    _tar_zst(src, {"filesystem/a.txt": b"seeded"})
    expected = hashlib.sha256(src.read_bytes()).hexdigest()
    size = src.stat().st_size

    kept = snapshot_main._keep_local_baseline(str(src), max_bytes=size)

    assert kept is not None
    assert kept.local_baseline_sha256 == expected
    assert kept.local_baseline_bytes == size
    landed = (
        grading_paths.baseline_dir(kept.local_baseline_id)
        / grading_paths.BASELINE_ARCHIVE_NAME
    )
    assert landed.is_file()
    # MOVED, not copied: two copies of a multi-hundred-MB archive is the disk
    # cost this feature was capped to avoid.
    assert not src.exists()


def test_an_archive_over_the_cap_is_not_kept(work_dir: Path) -> None:
    """The cap is the whole reason this is safe on a 13 GB tree."""
    src = work_dir / "built.tar.zst"
    _tar_zst(src, {"filesystem/a.txt": b"x" * 4096})

    assert snapshot_main._keep_local_baseline(str(src), max_bytes=1) is None
    # Left where it was, for the caller's `finally` to unlink.
    assert src.exists()


def test_the_decline_is_reported_and_not_silent(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declined baseline sends the trajectory to the lane, and the runner logs
    that as "not in the rollout". Without a signal here that is indistinguishable
    from the world never having been enabled."""
    seen: list[tuple[str, float, list[str]]] = []

    def _record(name: str, value: float, tags: list[str] | None = None) -> None:
        seen.append((name, value, list(tags or ())))

    monkeypatch.setattr(snapshot_main, "distribution", _record)
    src = work_dir / "built.tar.zst"
    _tar_zst(src, {"filesystem/a.txt": b"x" * 4096})

    assert snapshot_main._keep_local_baseline(str(src), max_bytes=1) is None

    assert [t for _n, _v, tags in seen for t in tags if t == "kept:false"]


def test_the_kept_directory_is_not_group_or_world_readable(work_dir: Path) -> None:
    """It holds the baseline for the whole run in a tree the model can traverse
    wherever uid separation is off."""
    src = work_dir / "built.tar.zst"
    _tar_zst(src, {"filesystem/a.txt": b"seeded"})

    kept = snapshot_main._keep_local_baseline(str(src), max_bytes=1 << 30)

    assert kept is not None
    d = grading_paths.baseline_dir(kept.local_baseline_id)
    assert d.stat().st_mode & 0o077 == 0
    assert (d / grading_paths.BASELINE_ARCHIVE_NAME).stat().st_mode & 0o077 == 0


# ── reading it back ──────────────────────────────────────────────────


def _request(**kwargs: Any) -> grade.GradeRequest:
    base: dict[str, Any] = {
        "grading_run_id": "gr_1",
        "trajectory_id": "traj_1",
        "trajectory_json": "{}",
        "grading_settings_json": "{}",
        "verifiers_json": "[]",
        "eval_configs_json": "[]",
        "scoring_config_json": "{}",
    }
    return grade.GradeRequest(**(base | kwargs))


def _kept(work_dir: Path, body: bytes = b"seeded") -> Any:
    src = work_dir / "built.tar.zst"
    _tar_zst(src, {"filesystem/a.txt": body})
    kept = snapshot_main._keep_local_baseline(str(src), max_bytes=1 << 30)
    assert kept is not None
    return kept


def test_a_matching_baseline_is_transcoded_to_a_zip(work_dir: Path) -> None:
    """Every verifier opens the baseline with `zipfile.ZipFile`, so a tar.zst
    that reached them unconverted would fail all of them."""
    kept = _kept(work_dir)
    dest = work_dir / "initial.zip"

    grade._local_baseline(
        _request(
            initial_snapshot_local_id=kept.local_baseline_id,
            initial_snapshot_local_sha256=kept.local_baseline_sha256,
            initial_snapshot_local_bytes=kept.local_baseline_bytes,
        ),
        dest,
    )

    with zipfile.ZipFile(dest) as zf:
        assert zf.read("filesystem/a.txt") == b"seeded"


def test_a_rewritten_baseline_is_refused(work_dir: Path) -> None:
    """A root agent has the whole loop to rewrite this file. Accepting it would
    let the run choose the tree its own diff is measured against."""
    kept = _kept(work_dir)
    landed = (
        grading_paths.baseline_dir(kept.local_baseline_id)
        / grading_paths.BASELINE_ARCHIVE_NAME
    )
    # Same length, different content: caught by the digest and not the size.
    forged = work_dir / "forged.tar.zst"
    _tar_zst(forged, {"filesystem/a.txt": b"forged"})
    landed.write_bytes(
        forged.read_bytes().ljust(kept.local_baseline_bytes, b"\0")[
            : kept.local_baseline_bytes
        ]
    )

    with pytest.raises(HTTPException) as caught:
        grade._local_baseline(
            _request(
                initial_snapshot_local_id=kept.local_baseline_id,
                initial_snapshot_local_sha256=kept.local_baseline_sha256,
                initial_snapshot_local_bytes=kept.local_baseline_bytes,
            ),
            work_dir / "initial.zip",
        )
    assert caught.value.status_code == 409
    assert "does not match" in str(caught.value.detail)


def test_a_truncated_baseline_is_refused(work_dir: Path) -> None:
    kept = _kept(work_dir)
    landed = (
        grading_paths.baseline_dir(kept.local_baseline_id)
        / grading_paths.BASELINE_ARCHIVE_NAME
    )
    landed.write_bytes(b"")

    with pytest.raises(HTTPException) as caught:
        grade._local_baseline(
            _request(
                initial_snapshot_local_id=kept.local_baseline_id,
                initial_snapshot_local_sha256=kept.local_baseline_sha256,
                initial_snapshot_local_bytes=kept.local_baseline_bytes,
            ),
            work_dir / "initial.zip",
        )
    assert caught.value.status_code == 409
    assert "does not match" in str(caught.value.detail)


def test_a_wrong_size_is_caught_without_digesting_the_file(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The size is checked first so a disagreement costs a stat and not a read
    of a file that can run to hundreds of MB. Any tampering the size catches,
    the digest would catch too, so this is the only thing that test can prove.
    """
    kept = _kept(work_dir)
    landed = (
        grading_paths.baseline_dir(kept.local_baseline_id)
        / grading_paths.BASELINE_ARCHIVE_NAME
    )
    landed.write_bytes(landed.read_bytes() + b"extra")

    def _explode() -> Any:
        raise AssertionError("digested a file whose size already disagreed")

    monkeypatch.setattr(grade.hashlib, "sha256", _explode)

    with pytest.raises(HTTPException) as caught:
        grade._local_baseline(
            _request(
                initial_snapshot_local_id=kept.local_baseline_id,
                initial_snapshot_local_sha256=kept.local_baseline_sha256,
                initial_snapshot_local_bytes=kept.local_baseline_bytes,
            ),
            work_dir / "initial.zip",
        )
    assert caught.value.status_code == 409


def test_a_deleted_baseline_says_so_rather_than_reporting_a_mismatch(
    work_dir: Path,
) -> None:
    """A full disk and a rewrite are the same lost grade to the caller and
    different events to whoever reads the log."""
    with pytest.raises(HTTPException) as caught:
        grade._local_baseline(
            _request(
                initial_snapshot_local_id="c" * 32,
                initial_snapshot_local_sha256="0" * 64,
                initial_snapshot_local_bytes=10,
            ),
            work_dir / "initial.zip",
        )
    assert caught.value.status_code == 409
    assert "not on disk" in str(caught.value.detail)


# ── the wire ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("route", ["async", "sync"])
def test_both_http_routes_forward_the_cap(
    route: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap rides on the request model and the handler takes it as an
    argument, so only the routers bridge the two. Neither did, the handler saw
    0, and nothing was ever kept — the feature shipped inert. `create_snapshot`
    uses the async route and falls back to the sync one on an older image, so
    both have to carry it.
    """
    seen: dict[str, Any] = {}

    async def _spy(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return SnapshotFilesResult(
            snapshot_id="snap_1", files_uploaded=1, total_bytes=1
        )

    request = SnapshotRequest(format="files", keep_local_baseline_max_bytes=4096)

    # By module path: `runner.data.__init__` re-exports an `APIRouter` named
    # `router`, so `from runner.data import router` is the object and not the
    # module the handler is looked up on.
    import importlib

    if route == "sync":
        data_router = importlib.import_module("runner.data.router")
        monkeypatch.setattr(data_router, "handle_snapshot_s3_files", _spy)
        _ = asyncio.run(data_router.snapshot_s3(request))
    else:
        jobs = importlib.import_module("runner.data.snapshot.jobs")
        monkeypatch.setattr(jobs, "handle_snapshot_s3_files", _spy)

        async def _start_and_wait() -> str:
            # `start_snapshot_job` schedules the work, so it needs a loop and
            # the handler runs after this returns.
            job_id: str = jobs.start_snapshot_job(request)
            deadline = time.monotonic() + 10
            while not seen and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            return job_id

        job_id = asyncio.run(_start_and_wait())
        assert seen, f"the async job never invoked the handler (job {job_id})"

    assert seen.get("keep_local_baseline_max_bytes") == 4096


def test_a_url_beside_a_local_id_uses_the_url(work_dir: Path) -> None:
    """The server withholds the URL only when its own resolution is the capture
    this sandbox kept. A URL arriving anyway means the two describe different
    trees, and grading the local one would diff against populated state while
    the lane diffs against the world seed."""
    kept = _kept(work_dir)
    fetched: list[str] = []

    async def _fake_download(url: str, dest: Path) -> None:
        fetched.append(url)
        with zipfile.ZipFile(dest, "w"):
            pass

    request = _request(
        initial_snapshot_url="https://s3/world.zip",
        initial_snapshot_local_id=kept.local_baseline_id,
        initial_snapshot_local_sha256=kept.local_baseline_sha256,
        initial_snapshot_local_bytes=kept.local_baseline_bytes,
    )

    import runner.grade as grade_mod

    original = grade_mod._download_snapshot
    grade_mod._download_snapshot = _fake_download
    try:
        asyncio.run(grade._materialize_baseline(request, work_dir / "initial.zip"))
    finally:
        grade_mod._download_snapshot = original

    assert fetched == ["https://s3/world.zip"]


def test_a_local_id_alone_is_used(work_dir: Path) -> None:
    """With no URL the local copy is the whole delivery, which is the point."""
    kept = _kept(work_dir)
    dest = work_dir / "initial.zip"

    asyncio.run(
        grade._materialize_baseline(
            _request(
                initial_snapshot_local_id=kept.local_baseline_id,
                initial_snapshot_local_sha256=kept.local_baseline_sha256,
                initial_snapshot_local_bytes=kept.local_baseline_bytes,
            ),
            dest,
        )
    )

    with zipfile.ZipFile(dest) as zf:
        assert zf.read("filesystem/a.txt") == b"seeded"


def test_an_older_caller_keeps_nothing() -> None:
    """The cap is the switch. A caller built before the field omits it and must
    behave as it does today, which is to unlink the archive after the upload."""
    assert SnapshotRequest().keep_local_baseline_max_bytes == 0


def test_a_result_without_a_kept_baseline_names_none() -> None:
    result = SnapshotFilesResult(snapshot_id="snap_1", files_uploaded=1, total_bytes=1)
    assert result.local_baseline_id == ""
    assert result.local_baseline_sha256 == ""
    assert result.local_baseline_bytes == 0


def test_a_grade_request_without_a_local_baseline_falls_back_to_the_url() -> None:
    assert _request().initial_snapshot_local_id == ""
