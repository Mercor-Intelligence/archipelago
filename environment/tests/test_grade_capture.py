"""The live capture an in-sandbox grade reads, checked against the lane's snapshot."""

from __future__ import annotations

import os
import zipfile
from collections.abc import Iterator
from functools import partial
from pathlib import Path

import pytest

from runner import grade
from runner.data.snapshot import main as snapshot_main
from runner.data.snapshot.utils import iter_paths

_EXCLUDE = ["filesystem/.cache/*"]


def _iter_paths_under(
    root: Path,
    root_dir: str,
    arc_prefix: str,
    exclude_globs: list[str] | None = None,
) -> Iterator[tuple[Path, str]]:
    return iter_paths(str(root / root_dir.lstrip("/")), arc_prefix, exclude_globs)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "sandbox"
    monkeypatch.setattr(grade, "iter_paths", partial(_iter_paths_under, root))
    monkeypatch.setattr(snapshot_main, "iter_paths", partial(_iter_paths_under, root))
    return root


def _write(path: Path, body: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _captured(tmp_path: Path) -> dict[str, bytes]:
    dest = tmp_path / "final.zip"
    grade._capture_live_final_snapshot(dest, _EXCLUDE)
    with zipfile.ZipFile(dest) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def _lane_upload() -> dict[str, bytes]:
    files = snapshot_main._collect_subsystem_files(
        grade._SNAPSHOT_SUBSYSTEMS, "snap", _EXCLUDE
    )
    return {key.removeprefix("snap/"): Path(path).read_bytes() for path, key in files}


def test_capture_holds_exactly_what_the_lane_uploads(
    sandbox: Path, tmp_path: Path
) -> None:
    fs = sandbox / "filesystem"
    target = _write(fs / "report.txt", b"report")
    _write(fs / "nested" / "data.bin", os.urandom(4096))
    _write(fs / ".cache" / "scratch", b"excluded")
    _write(sandbox / ".apps_data" / "slack" / "state.json", b"{}")
    outside = _write(tmp_path / "outside.txt", b"outside the tree")
    (fs / "link_in").symlink_to(target)
    (fs / "link_out").symlink_to(outside)
    (fs / "broken").symlink_to(tmp_path / "missing")

    captured = _captured(tmp_path)

    assert captured == _lane_upload()
    assert captured["filesystem/link_in"] == b"report"
    assert captured["filesystem/link_out"] == b"outside the tree"
    assert "filesystem/broken" not in captured
    assert "filesystem/.cache/scratch" not in captured


def test_capture_keeps_a_file_dated_before_1980(sandbox: Path, tmp_path: Path) -> None:
    old = _write(sandbox / "filesystem" / "epoch.txt", b"epoch")
    os.utime(old, (0, 0))

    assert _captured(tmp_path) == {"filesystem/epoch.txt": b"epoch"}
