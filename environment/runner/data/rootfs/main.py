"""Capture rootfs changes as an OCI image layer and upload to S3.

The regular snapshot flow only captures the 'filesystem' and '.apps_data'
subsystems, so system-level changes an agent makes during a trajectory
(``apt-get install postgresql``, npm/pip installs, useradd, ...) are lost and
unavailable when grading re-provisions the environment weeks later. This
module captures those changes as a single tar.gz suitable for appending onto
the platform's base image as an OCI layer (``crane append``).

Two-step protocol:

1. ``/data/rootfs/baseline`` touches a marker file after populate + hooks
   complete, so pre-existing image/populate state is excluded.
2. ``/data/rootfs/capture`` tars every file whose *status changed* after the
   marker (GNU tar ``--newer``, which compares ctime — dpkg preserves upstream
   mtimes when unpacking .debs, so an mtime diff would miss installed
   packages) and streams it to S3 next to the snapshot's files.

Unlike the snapshot tar (which normalizes owners/modes and dereferences
symlinks for deterministic grading diffs), the layer tar must preserve
numeric owners, modes, symlinks, hardlinks, and xattrs so the composed image
is faithful — hence GNU tar rather than the snapshot streaming path.
"""

import asyncio
import os
import time
from pathlib import Path

from fastapi import HTTPException
from loguru import logger

from runner.coordinator.runtime import get_coordinator
from runner.utils.metrics import distribution
from runner.utils.s3 import S3Credentials
from runner.utils.settings import get_settings

from ..snapshot.main import (
    _is_valid_snapshot_id,  # pyright: ignore[reportPrivateUsage]
)
from ..snapshot.utils import s3_stream_uploader
from .models import RootfsBaselineResult, RootfsCaptureResult

settings = get_settings()

# Touched by /data/rootfs/baseline; its ctime bounds what capture includes,
# and tar stats this path again at capture time. It therefore lives in a
# root-owned 0700 directory agents never write to (never /app, where agents
# build) so the path can't be pre-created or swapped for a symlink, and /run
# is excluded from the layer anyway. Also excluded by basename as
# belt-and-braces (see _build_tar_argv).
BASELINE_MARKER_PATH = "/run/rlstudio/.rootfs_capture_baseline"

CAPTURE_ROOT = "/"

# Paths never included in the layer. Members are named relative to the capture
# root ("./proc/..."), so exclude patterns use the "./" form. /filesystem and
# /.apps_data ride the regular snapshot; apt list and package caches are
# regenerable and would bloat the layer. /app (the runner install dir) is
# deliberately NOT excluded: some agents build their app there, and the
# runner's own files carry image-build-time ctimes that predate the baseline
# marker, so they never enter the layer anyway.
CAPTURE_EXCLUDES = (
    "./proc",
    "./sys",
    "./dev",
    "./run",
    "./tmp",
    "./var/tmp",
    "./var/lib/apt/lists",
    "./var/cache/apt",
    "./filesystem",
    "./.apps_data",
)

# Hard cap on the compressed layer size — a layer this large indicates a
# runaway capture (baseline never marked, exclusion miss) rather than real
# agent-installed dependencies.
MAX_LAYER_BYTES = 20 * 1024**3

_STDERR_TAIL_CHARS = 4000
_STDOUT_CHUNK_BYTES = 65536


async def handle_rootfs_baseline() -> RootfsBaselineResult:
    """Touch the baseline marker file for a later rootfs capture.

    Entry point for the /data/rootfs/baseline endpoint. Idempotent — calling
    again moves the baseline forward, excluding everything before the call.

    Symlink hardening: the runner runs as root, so the write refuses to
    follow a symlink (O_NOFOLLOW) and the ctime is read from the opened fd —
    a pre-created or swapped marker path can never redirect the write or the
    baseline timestamp.
    """
    marker = Path(BASELINE_MARKER_PATH)
    marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(
        str(marker),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(fd, str(time.time()).encode())
        marked_at = os.fstat(fd).st_ctime
    finally:
        os.close(fd)
    logger.info(f"Rootfs capture baseline marked at {marked_at} ({marker})")
    return RootfsBaselineResult(marker_path=str(marker), marked_at=marked_at)


def _build_tar_argv(marker_path: str, root: str) -> list[str]:
    """Build the GNU tar argv for the rootfs layer capture.

    ``--newer`` (not ``--newer-mtime``) so the comparison uses the marker
    file's ctime and includes members whose data *or status* changed — dpkg
    preserves upstream mtimes, so only ctime catches installed packages.
    No ``--dereference``: symlinks must stay symlinks in an image layer.
    """
    return [
        "tar",
        "--create",
        "--gzip",
        "--numeric-owner",
        "--xattrs",
        "--acls",
        f"--newer={marker_path}",
        *[f"--exclude={e}" for e in CAPTURE_EXCLUDES],
        # No slash in the pattern -> matches the basename at any depth, which
        # keeps the marker out of the layer even when it isn't under an
        # excluded directory (as in tests with a temporary capture root).
        f"--exclude={Path(marker_path).name}",
        "-C",
        root,
        "-f",
        "-",
        ".",
    ]


async def handle_rootfs_capture(
    snapshot_id: str,
    s3_credentials: S3Credentials | None = None,
) -> RootfsCaptureResult:
    """Capture post-baseline rootfs changes and upload as a tar.gz layer.

    Entry point for the /data/rootfs/capture endpoint. Streams tar output
    straight into a multipart S3 upload (constant memory), landing at
    ``s3://{bucket}/{prefix}/{snapshot_id}/rootfs-layer.tar.gz`` — the same
    prefix as the snapshot's individual files.

    Raises:
        HTTPException: 400 for an invalid snapshot_id or a missing baseline
            marker; 500 if tar fails or the size cap is exceeded.
    """
    if not _is_valid_snapshot_id(snapshot_id):
        raise HTTPException(status_code=400, detail="Invalid snapshot_id")
    if not Path(BASELINE_MARKER_PATH).exists():
        raise HTTPException(
            status_code=400,
            detail=(
                "No rootfs baseline marker found; call /data/rootfs/baseline "
                "before capturing"
            ),
        )

    await get_coordinator().finish_actions()

    object_key = f"{snapshot_id}/rootfs-layer.tar.gz"
    key_prefix = (
        settings.S3_SNAPSHOTS_PREFIX.rstrip("/") + "/"
        if settings.S3_SNAPSHOTS_PREFIX
        else ""
    )
    s3_uri = f"s3://{settings.S3_SNAPSHOTS_BUCKET}/{key_prefix}{object_key}"

    argv = _build_tar_argv(BASELINE_MARKER_PATH, CAPTURE_ROOT)
    logger.info(f"Starting rootfs layer capture {snapshot_id} -> {s3_uri}")
    start = time.monotonic()

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None

    stderr_tail = bytearray()

    async def _drain_stderr() -> None:
        # Keep only the tail; tar can emit one warning per changed file.
        assert proc.stderr is not None
        while chunk := await proc.stderr.read(_STDOUT_CHUNK_BYTES):
            stderr_tail.extend(chunk)
            del stderr_tail[:-_STDERR_TAIL_CHARS]

    try:
        async with s3_stream_uploader(object_key, s3_credentials) as uploader:
            stderr_task = asyncio.create_task(_drain_stderr())
            try:
                while chunk := await proc.stdout.read(_STDOUT_CHUNK_BYTES):
                    uploader.write(chunk)
                    if uploader.tell() > MAX_LAYER_BYTES:
                        raise HTTPException(
                            status_code=500,
                            detail=f"Rootfs layer exceeded {MAX_LAYER_BYTES} bytes; aborting capture",
                        )
            except BaseException:
                # Kill before awaiting the stderr drain: with tar still
                # running, stderr never reaches EOF and the finally would
                # hang. Killing closes both pipes.
                if proc.returncode is None:
                    proc.kill()
                raise
            finally:
                await stderr_task
            returncode = await proc.wait()
            # GNU tar exits 1 for "file changed as we read it" — expected on a
            # live system and the affected files are still archived; only >=2
            # is a real failure.
            if returncode not in (0, 1):
                stderr_text = stderr_tail.decode(errors="replace")
                raise HTTPException(
                    status_code=500,
                    detail=f"tar exited with code {returncode}: {stderr_text}",
                )
            size_bytes = uploader.tell()
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        raise

    duration_s = time.monotonic() - start
    distribution("studio.trajectory.rootfs_layer.total_bytes", size_bytes)
    distribution("studio.trajectory.rootfs_layer.duration_seconds", duration_s)
    logger.info(
        f"Rootfs layer capture {snapshot_id} completed: {size_bytes} bytes in {duration_s:.1f}s -> {s3_uri}"
    )
    return RootfsCaptureResult(
        s3_uri=s3_uri, size_bytes=size_bytes, duration_s=duration_s
    )
