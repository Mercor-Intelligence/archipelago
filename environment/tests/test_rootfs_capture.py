"""Rootfs layer capture: baseline marker, tar diff semantics, upload plumbing.

The tar-semantics tests run the real tar binary and are skipped when GNU tar
is unavailable (macOS ships bsdtar, which lacks ``--newer=FILE``); they run in
CI on Linux and match the Debian environment image.
"""

import asyncio
import io
import os
import shutil
import subprocess
import tarfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import HTTPException

from runner.data.rootfs import main as rootfs_main
from runner.data.rootfs.main import (
    _build_tar_argv,
    handle_rootfs_baseline,
    handle_rootfs_capture,
)


def _gnu_tar_available() -> bool:
    tar = shutil.which("tar")
    if not tar:
        return False
    out = subprocess.run([tar, "--version"], capture_output=True, text=True)
    return "GNU tar" in out.stdout


requires_gnu_tar = pytest.mark.skipif(
    not _gnu_tar_available(), reason="requires GNU tar (--newer=FILE)"
)


# --- baseline ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_touches_marker(tmp_path, monkeypatch):
    marker = tmp_path / ".rootfs_capture_baseline"
    monkeypatch.setattr(rootfs_main, "BASELINE_MARKER_PATH", str(marker))

    result = await handle_rootfs_baseline()

    assert marker.exists()
    assert result.marker_path == str(marker)
    assert result.marked_at == marker.stat().st_ctime


@pytest.mark.asyncio
async def test_baseline_is_idempotent(tmp_path, monkeypatch):
    marker = tmp_path / ".rootfs_capture_baseline"
    monkeypatch.setattr(rootfs_main, "BASELINE_MARKER_PATH", str(marker))

    first = await handle_rootfs_baseline()
    second = await handle_rootfs_baseline()

    assert second.marked_at >= first.marked_at


@pytest.mark.asyncio
async def test_baseline_creates_parent_dir(tmp_path, monkeypatch):
    marker = tmp_path / "rlstudio" / ".rootfs_capture_baseline"
    monkeypatch.setattr(rootfs_main, "BASELINE_MARKER_PATH", str(marker))

    await handle_rootfs_baseline()

    assert marker.exists()
    assert marker.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_baseline_refuses_symlinked_marker(tmp_path, monkeypatch):
    """The root runner must never follow a pre-created symlink at the marker
    path (write redirection hardening — O_NOFOLLOW)."""
    target = tmp_path / "victim"
    target.write_text("do not clobber")
    marker = tmp_path / ".rootfs_capture_baseline"
    marker.symlink_to(target)
    monkeypatch.setattr(rootfs_main, "BASELINE_MARKER_PATH", str(marker))

    with pytest.raises(OSError):
        await handle_rootfs_baseline()

    assert target.read_text() == "do not clobber"


# --- capture validation -----------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    ["", "snap with spaces", "snap;rm -rf /", "../etc/passwd", "snap$id", "a/b"],
)
@pytest.mark.asyncio
async def test_capture_rejects_invalid_snapshot_id(bad_id):
    with pytest.raises(HTTPException) as exc_info:
        await handle_rootfs_capture(snapshot_id=bad_id)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_capture_requires_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(
        rootfs_main, "BASELINE_MARKER_PATH", str(tmp_path / "missing_marker")
    )
    with pytest.raises(HTTPException) as exc_info:
        await handle_rootfs_capture(snapshot_id="snap_abc123")
    assert exc_info.value.status_code == 400
    assert "baseline" in exc_info.value.detail


# --- tar argv ---------------------------------------------------------------


def test_tar_argv_shape():
    argv = _build_tar_argv("/app/.rootfs_capture_baseline", "/")

    assert argv[0] == "tar"
    assert "--newer=/app/.rootfs_capture_baseline" in argv
    # ctime comparison is the point: --newer-mtime would miss dpkg-installed
    # files, which keep upstream mtimes.
    assert not any(a.startswith("--newer-mtime") for a in argv)
    assert "--numeric-owner" in argv
    # Symlinks must stay symlinks in an image layer.
    assert "--dereference" not in argv and "-h" not in argv
    # Members are named "./..." relative to the capture root, so excludes use
    # the "./" form.
    for excluded in ("./proc", "./sys", "./dev", "./filesystem", "./.apps_data"):
        assert f"--exclude={excluded}" in argv
    # /app is NOT excluded: agents sometimes build their app there, and the
    # runner's own files predate the ctime baseline anyway.
    assert "--exclude=./app" not in argv
    assert argv[-1] == "."


# --- tar diff semantics (real GNU tar) ---------------------------------------


def _run_capture_tar(marker: Path, root: Path) -> tarfile.TarFile:
    argv = _build_tar_argv(str(marker), str(root))
    out = subprocess.run(argv, capture_output=True)
    assert out.returncode in (0, 1), out.stderr.decode(errors="replace")
    return tarfile.open(fileobj=io.BytesIO(out.stdout), mode="r:gz")


@requires_gnu_tar
def test_capture_includes_only_post_baseline_changes(tmp_path):
    root = tmp_path / "root"
    (root / "usr/bin").mkdir(parents=True)
    (root / "usr/bin/preexisting").write_text("old")

    marker = tmp_path / ".rootfs_capture_baseline"
    # ctime granularity can be a full second on some filesystems.
    time.sleep(1.1)
    marker.write_text("baseline")
    time.sleep(1.1)

    (root / "usr/bin/newtool").write_text("new")
    os.chmod(root / "usr/bin/newtool", 0o750)
    (root / "usr/bin/newtool-link").symlink_to("newtool")
    # dpkg preserves upstream mtimes when unpacking .debs — a freshly
    # installed file can carry an ancient mtime but always has a fresh ctime.
    dpkg_file = root / "usr/bin/dpkg-installed"
    dpkg_file.write_text("payload")
    os.utime(dpkg_file, (0, 0))

    with _run_capture_tar(marker, root) as tf:
        names = tf.getnames()
        assert not any("preexisting" in n for n in names)
        assert any(n.endswith("usr/bin/newtool") for n in names)
        assert any(n.endswith("usr/bin/dpkg-installed") for n in names)

        link = next(m for m in tf.getmembers() if m.name.endswith("newtool-link"))
        assert link.issym()
        assert link.linkname == "newtool"

        tool = next(
            m
            for m in tf.getmembers()
            if m.name.endswith("usr/bin/newtool") and m.isfile()
        )
        assert tool.mode & 0o777 == 0o750


@requires_gnu_tar
def test_capture_excludes_virtual_and_subsystem_dirs(tmp_path):
    root = tmp_path / "root"
    marker = tmp_path / ".rootfs_capture_baseline"
    root.mkdir()
    marker.write_text("baseline")
    time.sleep(1.1)

    for excluded in ("proc", "tmp", "filesystem", ".apps_data"):
        d = root / excluded
        d.mkdir()
        (d / "inside").write_text("x")
    (root / "etc").mkdir()
    (root / "etc/passwd").write_text("postgres:x:999:999::/var/lib/postgresql:")
    # /app is not excluded — agents sometimes build their app there, and
    # pre-baseline runner files never enter the layer (ctime) anyway.
    (root / "app").mkdir()
    (root / "app/agent-built-app.js").write_text("x")

    with _run_capture_tar(marker, root) as tf:
        names = tf.getnames()
        assert any(n.endswith("etc/passwd") for n in names)
        assert any(n.endswith("app/agent-built-app.js") for n in names)
        assert not any("inside" in n for n in names)


@requires_gnu_tar
def test_capture_excludes_marker_in_root(tmp_path):
    """A marker placed inside the capture root must not leak into the layer."""
    root = tmp_path / "root"
    root.mkdir()
    marker = root / ".rootfs_capture_baseline"
    marker.write_text("baseline")
    time.sleep(1.1)
    (root / "newfile").write_text("x")

    with _run_capture_tar(marker, root) as tf:
        names = tf.getnames()
        assert any(n.endswith("newfile") for n in names)
        assert not any(".rootfs_capture_baseline" in n for n in names)


# --- capture handler plumbing -------------------------------------------------


class _StubCoordinator:
    async def finish_actions(self) -> None:
        pass


@pytest.fixture
def stub_coordinator(monkeypatch):
    """The real coordinator store roots at /.apps_data, absent on dev hosts."""
    monkeypatch.setattr(rootfs_main, "get_coordinator", _StubCoordinator)


class _FakeUploader:
    def __init__(self):
        self.data = bytearray()

    def write(self, chunk: bytes) -> int:
        self.data.extend(chunk)
        return len(chunk)

    def tell(self) -> int:
        return len(self.data)


@pytest.mark.asyncio
async def test_capture_streams_tar_output_to_uploader(
    tmp_path, monkeypatch, stub_coordinator
):
    marker = tmp_path / ".rootfs_capture_baseline"
    marker.write_text("baseline")
    monkeypatch.setattr(rootfs_main, "BASELINE_MARKER_PATH", str(marker))

    payload = tmp_path / "payload"
    payload.write_bytes(b"layer-bytes" * 1000)
    # Stand in for tar with a portable byte producer; the tar semantics are
    # covered by the GNU-tar tests above.
    monkeypatch.setattr(
        rootfs_main, "_build_tar_argv", lambda m, r: ["cat", str(payload)]
    )

    fake = _FakeUploader()

    @asynccontextmanager
    async def fake_uploader(object_key, s3_credentials=None):
        assert object_key == "snap_abc123/rootfs-layer.tar.gz"
        yield fake

    monkeypatch.setattr(rootfs_main, "s3_stream_uploader", fake_uploader)

    result = await handle_rootfs_capture(snapshot_id="snap_abc123")

    assert bytes(fake.data) == payload.read_bytes()
    assert result.size_bytes == len(fake.data)
    assert result.s3_uri.endswith("snap_abc123/rootfs-layer.tar.gz")


@pytest.mark.asyncio
async def test_capture_fails_on_tar_error(tmp_path, monkeypatch, stub_coordinator):
    marker = tmp_path / ".rootfs_capture_baseline"
    marker.write_text("baseline")
    monkeypatch.setattr(rootfs_main, "BASELINE_MARKER_PATH", str(marker))
    # Exit 1 is tolerated (GNU tar's "file changed as we read it"); only >=2
    # is treated as a failure.
    monkeypatch.setattr(
        rootfs_main,
        "_build_tar_argv",
        lambda m, r: ["sh", "-c", "echo boom >&2; exit 2"],
    )

    @asynccontextmanager
    async def fake_uploader(object_key, s3_credentials=None):
        yield _FakeUploader()

    monkeypatch.setattr(rootfs_main, "s3_stream_uploader", fake_uploader)

    with pytest.raises(HTTPException) as exc_info:
        await handle_rootfs_capture(snapshot_id="snap_abc123")
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_capture_aborts_when_size_cap_exceeded(
    tmp_path, monkeypatch, stub_coordinator
):
    marker = tmp_path / ".rootfs_capture_baseline"
    marker.write_text("baseline")
    monkeypatch.setattr(rootfs_main, "BASELINE_MARKER_PATH", str(marker))
    monkeypatch.setattr(rootfs_main, "MAX_LAYER_BYTES", 100)

    payload = tmp_path / "payload"
    payload.write_bytes(b"x" * 100_000)
    monkeypatch.setattr(
        rootfs_main, "_build_tar_argv", lambda m, r: ["cat", str(payload)]
    )

    @asynccontextmanager
    async def fake_uploader(object_key, s3_credentials=None):
        yield _FakeUploader()

    monkeypatch.setattr(rootfs_main, "s3_stream_uploader", fake_uploader)

    with pytest.raises(HTTPException) as exc_info:
        await asyncio.wait_for(
            handle_rootfs_capture(snapshot_id="snap_abc123"), timeout=30
        )
    assert exc_info.value.status_code == 500
    assert "exceeded" in exc_info.value.detail
