"""Snapshot subsystems to S3 or stream as tar.gz.

This module handles creating tar.gz archives of subsystem directories and
either uploading them to S3 or streaming them back as HTTP responses.
Currently snapshots include only 'filesystem' and '.apps_data' subsystems.

The implementation can stream tar.gz data directly to S3 using multipart upload,
or stream it back as an HTTP response, allowing it to handle TB-scale snapshots
without loading everything into memory.

There are two S3 upload modes:
1. tar.gz archive: Single compressed file
2. Individual files: Preserves directory structure

Also supports pre-snapshot hooks that run shell commands before creating the archive.
"""

import asyncio
import os
import random
import tarfile
import tempfile
import time
from collections.abc import Callable, Coroutine, Iterator, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4 as uuid

import aiofiles
import zstandard
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from fastapi import HTTPException
from loguru import logger

from runner.coordinator.runtime import get_coordinator
from runner.utils.decorators import with_concurrency_limit
from runner.utils.metrics import (
    distribution,
    peak_memory_bytes,
    snapshot_size_bucket,
)
from runner.utils.s3 import (
    RefreshableS3Credentials,
    S3Credentials,
    SnapshotCredentialsExpired,
    get_s3_client,
)
from runner.utils.settings import get_settings

from ..populate.main import run_lifecycle_hooks
from ..populate.models import HookTiming, LifecycleHook
from .models import SnapshotFilesResult, SnapshotResult
from .streaming import create_tar_gz_stream
from .utils import generate_presigned_url, iter_paths, s3_stream_uploader

if TYPE_CHECKING:
    from boto3.s3.transfer import TransferConfig

settings = get_settings()


def _is_valid_snapshot_id(s: str) -> bool:
    ALLOWED = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    )
    return bool(s) and all(c in ALLOWED for c in s)


async def handle_snapshot(
    pre_snapshot_hooks: list[LifecycleHook] | None = None,
    exclude_globs: Sequence[str] | None = None,
) -> tuple[Iterator[bytes], str, list[HookTiming]]:
    """Create a tar.gz archive of all subsystems and stream it back.

    Entry point for the /data/snapshot endpoint. Runs any pre-snapshot hooks
    first, then creates a compressed tar archive containing all files from
    the 'filesystem' and '.apps_data' subsystems and streams it back as an
    HTTP response.

    The snapshot includes a unique ID in the filename and can be called
    multiple times to create incremental snapshots of the environment state.

    This implementation streams data directly to the HTTP response using a
    queue-based approach, allowing it to handle TB-scale snapshots without
    loading everything into memory. Chunks are yielded as soon as they're
    compressed by tarfile, enabling true streaming.

    Args:
        pre_snapshot_hooks: Optional list of hooks to run before creating snapshot
            (e.g., database dumps)
        exclude_globs: Optional fnmatch patterns tested against each file's
            archive name; matching files are excluded from the snapshot

    Returns:
        Tuple of (generator yielding bytes chunks, filename, hook_timings)

    Raises:
        HTTPException: If hooks fail or snapshot creation fails
    """
    snapshot_id = f"snap_{uuid().hex}"
    filename = f"{snapshot_id}.tar.gz"
    await get_coordinator().finish_actions()

    # Run pre-snapshot hooks in parallel (e.g., database dumps — services have isolated state)
    stream_hook_timings: list[HookTiming] = []
    if pre_snapshot_hooks:
        logger.info(f"Running {len(pre_snapshot_hooks)} pre-snapshot hook(s)")
        try:
            stream_hook_timings = await run_lifecycle_hooks(
                pre_snapshot_hooks, phase="pre_snapshot"
            )
            for ht in stream_hook_timings:
                logger.info(
                    f"Pre-snapshot hook '{ht.name}' completed in {ht.duration_s:.1f}s"
                )
            logger.info("All pre-snapshot hooks completed")
        except RuntimeError as e:
            logger.error(f"Pre-snapshot hook failed: {repr(e)}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    # Subsystems to snapshot
    subsystems = [settings.FILESYSTEM_SUBSYSTEM_NAME, settings.APPS_DATA_SUBSYSTEM_NAME]

    logger.debug(
        f"Starting snapshot stream {snapshot_id} for subsystems: {', '.join(subsystems)}"
    )

    try:
        # Create generator that yields chunks directly as tarfile compresses
        return (
            create_tar_gz_stream(
                subsystems,
                snapshot_id,
                partial(iter_paths, exclude_globs=exclude_globs),
            ),
            filename,
            stream_hook_timings,
        )
    except Exception as e:
        logger.error(f"Error creating snapshot {snapshot_id}: {repr(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create snapshot {snapshot_id}: {str(e)}",
        ) from e


async def handle_snapshot_s3(
    snapshot_id: str | None = None,
    pre_snapshot_hooks: list[LifecycleHook] | None = None,
    s3_credentials: S3Credentials | RefreshableS3Credentials | None = None,
    exclude_globs: Sequence[str] | None = None,
) -> SnapshotResult:
    """Create a tar.gz archive of all subsystems and upload to S3.

    Entry point for the /data/snapshot/s3 endpoint. Runs any pre-snapshot hooks
    first, then creates a compressed tar archive containing all files from the
    'filesystem' and '.apps_data' subsystems, streams it directly to S3 using
    multipart upload, and returns metadata including a pre-signed download URL.

    The snapshot includes a unique ID and can be called multiple times
    to create incremental snapshots of the environment state.

    This implementation streams data directly to S3, allowing it to handle
    TB-scale snapshots without loading everything into memory.

    Args:
        snapshot_id: Optional unique identifier for this snapshot, preallocated
            by caller
        pre_snapshot_hooks: Optional list of hooks to run before creating snapshot
            (e.g., database dumps)
        exclude_globs: Optional fnmatch patterns tested against each file's
            archive name; matching files are excluded from the snapshot

    Returns:
        SnapshotResult containing:
        - snapshot_id: Unique identifier for this snapshot
        - s3_uri: Full S3 URI of the uploaded archive
        - presigned_url: Temporary download URL (expires in 7 days)
        - size_bytes: Size of the archive in bytes

    Raises:
        HTTPException: If S3 is not configured (S3_SNAPSHOTS_BUCKET not set),
            hooks fail, or if snapshot creation/upload fails
    """
    if snapshot_id is None:
        snapshot_id = f"snap_{uuid().hex}"
    elif not _is_valid_snapshot_id(snapshot_id):
        raise HTTPException(status_code=400, detail="Invalid snapshot ID")
    await get_coordinator().finish_actions()

    # 1. Run pre-snapshot hooks in parallel (e.g., database dumps — services have isolated state)
    snapshot_hook_timings: list[HookTiming] = []
    if pre_snapshot_hooks:
        logger.info(f"Running {len(pre_snapshot_hooks)} pre-snapshot hook(s)")
        try:
            snapshot_hook_timings = await run_lifecycle_hooks(
                pre_snapshot_hooks, phase="pre_snapshot"
            )
            logger.info("All pre-snapshot hooks completed")
        except RuntimeError as e:
            logger.error(f"Pre-snapshot hook failed: {repr(e)}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    object_key = f"{snapshot_id}.tar.gz"

    # Build S3 key early for error messages
    key = (
        settings.S3_SNAPSHOTS_PREFIX.rstrip("/") + "/"
        if settings.S3_SNAPSHOTS_PREFIX
        else ""
    )
    key += object_key

    # Subsystems to snapshot
    subsystems = [settings.FILESYSTEM_SUBSYSTEM_NAME, settings.APPS_DATA_SUBSYSTEM_NAME]

    logger.debug(
        f"Starting snapshot {snapshot_id} for subsystems: {', '.join(subsystems)}"
    )
    logger.debug(f"Target S3 location: s3://{settings.S3_SNAPSHOTS_BUCKET}/{key}")

    try:
        # Stream tar.gz directly to S3 using multipart upload
        size_bytes = 0
        async with s3_stream_uploader(object_key, s3_credentials) as uploader:
            # Create tar.gz and write directly to S3 uploader
            # tarfile will call uploader.write() as it compresses files
            with tarfile.open(mode="w:gz", fileobj=uploader) as tf:
                for subsystem in subsystems:
                    subsystem_path = f"/{subsystem}"
                    logger.debug(
                        f"Adding subsystem '{subsystem}' from {subsystem_path} to archive"
                    )
                    # Use subsystem name as arc prefix (handles nested paths correctly)
                    file_count = 0
                    for path, arcname in iter_paths(
                        subsystem_path, subsystem, exclude_globs=exclude_globs
                    ):
                        tf.add(path, arcname=arcname, recursive=False)
                        file_count += 1
                    logger.debug(
                        f"Added {file_count} file(s) from subsystem '{subsystem}'"
                    )

            # Flush any remaining buffered data before closing
            await uploader.flush()
            # Get size before context manager closes
            size_bytes = uploader.total_size
            logger.debug(f"Completed streaming {size_bytes} bytes to S3")

        # Generate pre-signed URL
        logger.debug(f"Generating pre-signed URL for {object_key}")
        presigned_url = await generate_presigned_url(
            object_key, s3_credentials=s3_credentials
        )

        s3_uri = f"s3://{settings.S3_SNAPSHOTS_BUCKET}/{key}"

        logger.info(
            f"Created snapshot {snapshot_id} ({size_bytes} bytes) with {len(subsystems)} subsystem(s): {', '.join(subsystems)}"
        )

        return SnapshotResult(
            snapshot_id=snapshot_id,
            s3_uri=s3_uri,
            presigned_url=presigned_url,
            size_bytes=size_bytes,
            hook_timings=snapshot_hook_timings,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating snapshot {snapshot_id}: {repr(e)}")
        s3_location = (
            f"s3://{settings.S3_SNAPSHOTS_BUCKET}/{key}"
            if settings.S3_SNAPSHOTS_BUCKET
            else "unknown location"
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create snapshot {snapshot_id} at {s3_location}: {str(e)}",
        ) from e


_UPLOAD_MAX_RETRIES = 3
# Orchestration-level retry passes over files still failing with a *transient*
# error after the first upload pass. A snapshot with thousands of tiny files
# (e.g. a uv/pip cache) issues thousands of PUTs, so even a low per-file
# transient-failure rate leaves a few stragglers that would otherwise fail the
# whole bundle (RLS / Alchemist world_ed64088d…). Retrying them across several
# backed-off passes rides out a transient S3 degradation window without
# dropping any files. Tunable via SNAPSHOT_UPLOAD_RETRY_PASSES.
_UPLOAD_RETRY_PASSES = int(os.getenv("SNAPSHOT_UPLOAD_RETRY_PASSES") or 4)
_UPLOAD_RETRY_PASS_BASE_DELAY_S = 2.0
_UPLOAD_RETRY_PASS_MAX_DELAY_S = 30.0
# Overall wall-clock budget for ALL retry passes combined. Bounds the worst
# case: a genuinely wedged file surfaces as a retryable `TimeoutError` (the
# `_upload_with_timeout` stall), so without a time cap the pass count would
# multiply its 3×~300s per-pass cost into ~an hour. With the budget, one such
# file gets ~one pass (matching prior behavior) while fast-failing transient
# stragglers still get many passes within the window. The snapshot job has no
# server-side deadline (see note below), so this cap protects the caller's
# polling budget. Tunable via SNAPSHOT_UPLOAD_RETRY_TOTAL_BUDGET_S.
#
# IT BOUNDS THE PASSES, NOT THE FIRST ONE. The check runs before pass 1 and
# after, so the initial `asyncio.gather` — which is not in `_retry_failed_uploads`
# at all — can exceed this on its own. A file read over a mount has a wider
# per-attempt budget still, which is why `slow_read_ceiling` bounds that
# separately against the caller's deadline rather than relying on this.
_UPLOAD_RETRY_TOTAL_BUDGET_S = float(
    os.getenv("SNAPSHOT_UPLOAD_RETRY_TOTAL_BUDGET_S") or 900.0
)
_UPLOAD_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
    ConnectionResetError,
    TimeoutError,
    # The lease lapsed before the caller's push landed. Retryable for the same
    # reason ExpiredToken is: the push is in flight and the retry can pick it
    # up. Note this does NOT reach us as a ClientError — once credentials are
    # refreshable, botocore fails the refresh itself and S3 is never asked, so
    # classifying only the S3 error code would miss every occurrence.
    SnapshotCredentialsExpired,
)
# S3 error codes worth retrying — mirrors the set in rl-studio's helpers.py.
#
# The expiry codes are retryable only because the credentials are rotatable
# (see runner.utils.s3.RefreshableS3Credentials): the caller pushes a fresh STS
# lease over the status-poll channel while the harvest runs, so a part that
# raced the swap succeeds on the retry. Treating them as permanent is what
# turned one large file's expiry into a whole failed snapshot — the retry
# passes re-attempted every other file against the same dead lease and
# reported all of them as failed.
_UPLOAD_RETRYABLE_S3_CODES = frozenset(
    {
        "IncompleteBody",
        "RequestTimeout",
        "ServiceUnavailable",
        "SlowDown",
        "InternalError",
        "ThrottlingException",
        "ExpiredToken",
        "ExpiredTokenException",
        "RequestExpired",
    }
)


def _is_retryable_upload_error(exc: BaseException) -> bool:
    """Return True if the exception is a transient S3/network error."""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return code in _UPLOAD_RETRYABLE_S3_CODES
    return isinstance(exc, _UPLOAD_RETRYABLE_EXCEPTIONS)


# A stalled multipart PUT has no deadline of its own: botocore's read_timeout
# only bounds *reads*, so an upload whose send side wedges hangs forever. The
# snapshot job has no server-side timeout either (see .jobs), so the stall was
# only ever discovered by the caller's multi-hour polling deadline — one wedged
# file burned the entire trajectory. Give each attempt its own budget instead,
# scaled by size so a 4 GiB DB isn't held to a small file's allowance.
#
# The floor is what most files actually get: at 5 MiB/s it only starts to bind
# above ~1.5 GiB, so on a typical world every file but the largest DB runs on
# the floor alone. Measured over 20 sandboxes uploading the same 6.5 GB payload,
# the slowest *successful* upload of any single file was 143s (a 1.4 GiB DB
# under 7-way contention; medians were 3-52s). 300s is ~2x that worst case, so
# a transient multi-minute wedge is absorbed inside one attempt instead of
# burning all three — the 120s this started at killed 72-468 MiB files three
# times over on sandboxes that then completed fine on the retry pass. A
# genuinely stalled upload is still cut loose in 5 minutes rather than hanging
# until the caller's deadline.
_UPLOAD_TIMEOUT_FLOOR_SECONDS = float(
    os.getenv("SNAPSHOT_UPLOAD_TIMEOUT_FLOOR_SECONDS") or 300.0
)
_UPLOAD_MIN_THROUGHPUT_MIB_S = float(
    os.getenv("SNAPSHOT_UPLOAD_MIN_THROUGHPUT_MIB_S") or 5.0
)
# BOTH NUMBERS ABOVE DESCRIBE A READ FROM LOCAL DISK, and a subsystem root that
# is a live Modal image mount is not one. The mount presents as `9p rw`, and a
# full-tree read over it measured ~4.6x slower than local disk. The floor's
# safety margin is only ~2x (300s against a 143s worst case), so a file read
# through a mount can exhaust its budget while still transferring — and
# `_upload_with_timeout` ABANDONS at that point. The failure mode is a LOST
# snapshot, not a slow one, which is why the read side cannot mount `.apps_data`
# until this scaling exists.
#
# Applied to the WHOLE budget rather than the floor alone, so the same ~2x
# margin survives on both terms: a mounted 143s worst case becomes ~658s and the
# floor becomes ~1380s, and the throughput term stays honest for a multi-GB DB
# whose bytes now arrive at ~1.1 MiB/s rather than 5.
#
# PER FILE, NOT PER SNAPSHOT. Only the roots the agent actually reported as
# mounted are scaled; a downloaded half in the same snapshot keeps the local-disk
# budget, so enabling a mount cannot slow the detection of a genuinely wedged
# upload anywhere else in the tree.
_UPLOAD_MOUNT_SLOWDOWN_FACTOR = float(
    os.getenv("SNAPSHOT_UPLOAD_MOUNT_SLOWDOWN_FACTOR") or 4.6
)
# How often the in-flight watchdog reports which uploads are still running.
_UPLOAD_PROGRESS_INTERVAL_SECONDS = float(
    os.getenv("SNAPSHOT_UPLOAD_PROGRESS_INTERVAL_SECONDS") or 60.0
)

# Timed-out upload tasks are cancelled but never awaited (see
# _upload_with_timeout), so hold a strong reference until they actually finish:
# asyncio keeps only a weak one, and a GC'd pending task logs
# "Task was destroyed but it is pending" and can tear down mid-cancellation.
_abandoned_uploads: set[asyncio.Task[object]] = set()


def slow_read_ceiling(job_deadline_seconds: float | None) -> float | None:
    """The largest per-attempt budget whose full retry sequence fits the deadline.

    ``_upload_single_file`` spends up to ``_UPLOAD_MAX_RETRIES`` attempts of one
    budget each, and the caller abandons the whole job at its polling deadline.
    Unbounded, the mount factor pushes 3 x 300s to 3 x 1380s = 4140s against a
    3300s deadline, so a wedged file would be cut off mid-sequence — losing the
    snapshot anyway, having spent the entire trajectory's snapshot window to get
    there. Dividing the deadline by the attempt count keeps the sequence inside
    it.

    ``None`` when the caller states no deadline, which is what an agent older
    than the field sends. That agent also sends no ``mounted_subsystems``, so it
    never reaches the scaled path and there is nothing to bound.
    """
    if job_deadline_seconds is None or job_deadline_seconds <= 0:
        return None
    return job_deadline_seconds / _UPLOAD_MAX_RETRIES


def _upload_timeout_seconds(
    file_size: int, *, slow_read: bool = False, ceiling: float | None = None
) -> float:
    """Per-attempt upload budget for a file of *file_size* bytes.

    ``slow_read`` marks a file whose bytes come off a live 9p image mount rather
    than local disk. See ``_UPLOAD_MOUNT_SLOWDOWN_FACTOR``.

    ``ceiling`` bounds ONLY the mount-scaled budget, never the local-disk one.
    The unscaled path's own relationship to the caller's deadline is whatever it
    has always been — a 10 GiB file already asks for more than a third of it —
    and clamping that here would shrink budgets on trees that have nothing to do
    with mounting. This bounds the inflation, not the baseline.
    """
    size_mib = file_size / (1024 * 1024)
    budget = max(_UPLOAD_TIMEOUT_FLOOR_SECONDS, size_mib / _UPLOAD_MIN_THROUGHPUT_MIB_S)
    if not slow_read:
        return budget
    scaled = budget * _UPLOAD_MOUNT_SLOWDOWN_FACTOR
    # Never BELOW the local-disk budget: a deadline short enough to push the
    # ceiling under it would otherwise make mounting a root tighter than not
    # mounting it, which is the opposite of what the scaling is for.
    return max(budget, min(scaled, ceiling)) if ceiling is not None else scaled


def _is_under_roots(local_path: str, roots: frozenset[str]) -> bool:
    """Whether *local_path* sits under one of the subsystem *roots*.

    Roots arrive as subsystem NAMES (``filesystem``, ``.apps_data``) because that
    is what the mount plan and the snapshot walk both speak; the paths they
    produce are absolute. Matching on ``/<root>/`` rather than ``/<root>`` keeps
    a hypothetical `/filesystem_backup` from being read as the mounted root.
    """
    return any(local_path.startswith(f"/{root}/") for root in roots)


async def _upload_with_timeout(
    coro: Coroutine[Any, Any, object], timeout: float, s3_key: str
) -> None:
    """Await *coro* under *timeout*, escaping even if it ignores cancellation.

    ``asyncio.wait_for`` cancels the inner future and then *awaits* the
    cancellation, so a stall parked at an uninterruptible point would hang the
    wrapper too — exactly the failure this bounds. ``asyncio.wait`` returns on
    timeout without waiting for the cancel to land, so we always get control
    back; the abandoned task is left to unwind on its own.
    """
    task = asyncio.ensure_future(coro)

    def _abandon() -> None:
        task.cancel()
        _abandoned_uploads.add(task)
        task.add_done_callback(_abandoned_uploads.discard)

    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        # asyncio.wait leaves the awaited task running when the *waiter* is
        # cancelled, so an in-flight upload would outlive the snapshot job.
        _abandon()
        raise
    if not done:
        _abandon()
        raise TimeoutError(
            f"upload of {s3_key} exceeded {timeout:.0f}s; abandoning this attempt"
        )
    # Surfaces the upload's own exception, if it raised.
    task.result()


_MIB = 1024 * 1024
_MULTIPART_THRESHOLD = 20 * _MIB
# Part size is deliberately unchanged from the long-standing value. Measured
# against a local S3 with per-part latency, growing it to 64 MiB bought ~nothing
# in throughput (329 vs 338 MB/s for 20 MiB parts on the same payload) while
# raising peak RSS for a single 1.2 GB upload from ~565 MB to ~1.57 GB — the
# buffers scale with part size, and 8 files upload concurrently.
_MULTIPART_CHUNKSIZE = 20 * _MIB
# THE knob that actually mattered. aioboto3's upload_file is not boto3's
# S3Transfer: it delegates to upload_fileobj, which reads forward in
# io_chunksize slices, each an aiofiles hop onto the shared thread pool. At the
# 256 KiB default a 1.2 GB file costs ~4,800 of them; at 4 MiB it costs ~300,
# for the same throughput at ~150 MB more peak RSS.
_UPLOAD_IO_CHUNKSIZE = 4 * _MIB
# Bounded explicitly. The default of 100 made the real per-file ceiling
# 100 x 20 MiB = 2 GiB — not the "10 x 20 MiB" an older comment here claimed,
# since it is the queue and not the request concurrency that bounds buffering.
_UPLOAD_MAX_IO_QUEUE = 10
_UPLOAD_PART_CONCURRENCY = 10
# S3 refuses a multipart upload of more than 10,000 parts.
_S3_MAX_PARTS = 10_000


def _transfer_config_for(file_size: int) -> "TransferConfig":
    """Multipart tuning for a file of *file_size* bytes.

    One profile for every file: the read size and queue depth above are what
    fixed the pathological case, and neither has a reason to vary with size.
    Part size grows only when it must — doubling until the object fits inside
    S3's part ceiling — so this is a correctness guard rather than a
    performance knob, and it does not engage at any world size seen today.
    """
    from boto3.s3.transfer import TransferConfig

    chunksize = _MULTIPART_CHUNKSIZE
    while file_size > chunksize * _S3_MAX_PARTS:
        chunksize *= 2

    return TransferConfig(
        multipart_threshold=_MULTIPART_THRESHOLD,
        multipart_chunksize=chunksize,
        io_chunksize=_UPLOAD_IO_CHUNKSIZE,
        max_concurrency=_UPLOAD_PART_CONCURRENCY,
        max_io_queue=_UPLOAD_MAX_IO_QUEUE,
    )


@with_concurrency_limit(max_concurrency=8)
async def _upload_single_file(
    s3_bucket,
    local_path: str,
    s3_key: str,
    progress: "_UploadProgress | None" = None,
    *,
    slow_read: bool = False,
    ceiling: float | None = None,
) -> int:
    """Upload a single file to S3 and return its size.

    Files >= 20 MiB stream from disk via ``upload_file``; smaller files use a
    single PUT. See :func:`_transfer_config_for` for the multipart tuning.

    Concurrency is capped at 8 files. Per file, bytes in flight are the queue
    (``max_io_queue`` x ``multipart_chunksize`` = 200 MiB) PLUS one part body
    per uploader coroutine, since ``upload_fileobj``'s uploaders pop the body
    off the queue and hold it for the duration of the PUT
    (``max_concurrency`` x ``multipart_chunksize`` = another 200 MiB), plus the
    reader's part-in-progress. Counting only the queue understates it ~2x.

    So ~400 MiB per file and ~3.2 GiB across the 8-file gate. Measured, not
    derived: 8 concurrent 1.2 GB uploads peak at 3198 MB RSS, against 2661 MB
    for the pre-retune config. The retune therefore RAISES the realized floor
    by ~20% and lowers the ceiling by ~5x — main's 100-deep queue tops out at
    2.2 GiB per file (17.6 GiB across the gate) and only stays under that
    because a fast link keeps the reader from ever filling it. A slow or wedged
    upload is precisely when it would fill, which is the case this path already
    OOM'd on at 20 concurrent uploads.

    *progress* is advanced as parts land so the watchdog can tell a slow
    upload from a wedged one, and is restarted at the top of every attempt
    because a retry re-sends the file from byte zero.

    Transient S3/network errors are retried up to ``_UPLOAD_MAX_RETRIES`` times
    with jittered exponential backoff.

    Args:
        s3_bucket: S3 bucket resource
        local_path: Local file path to upload
        s3_key: S3 key (destination path)
        progress: Optional per-file transfer state for the watchdog
        slow_read: True when this file's bytes come off a live 9p image mount,
            which widens the per-attempt budget. See
            ``_UPLOAD_MOUNT_SLOWDOWN_FACTOR``.
        ceiling: Upper bound on that widened budget, from
            ``slow_read_ceiling``. Ignored for a local-disk read.

    Returns:
        Size of the uploaded file in bytes
    """
    file_size = os.path.getsize(local_path)
    timeout = _upload_timeout_seconds(file_size, slow_read=slow_read, ceiling=ceiling)

    for attempt in range(_UPLOAD_MAX_RETRIES):
        started = time.monotonic()
        # Rebound per attempt: the previous attempt's sink stops counting the
        # moment this one starts, so an abandoned upload's late callbacks are
        # dropped instead of landing on this attempt's tally.
        advance = progress.restart(s3_key) if progress is not None else None
        try:
            s3_object = await s3_bucket.Object(s3_key)

            if file_size >= _MULTIPART_THRESHOLD:
                await _upload_with_timeout(
                    s3_object.upload_file(
                        local_path,
                        Config=_transfer_config_for(file_size),
                        Callback=advance,
                    ),
                    timeout,
                    s3_key,
                )
            else:
                async with aiofiles.open(local_path, "rb") as f:
                    content = await f.read()
                await _upload_with_timeout(s3_object.put(Body=content), timeout, s3_key)

            elapsed = time.monotonic() - started
            logger.debug(
                f"_upload_single_file {s3_key}: {file_size} bytes in "
                f"{elapsed:.1f}s ({file_size / (1024 * 1024) / max(elapsed, 1e-6):.1f} MiB/s)"
            )
            return file_size
        except Exception as exc:
            is_last = attempt >= _UPLOAD_MAX_RETRIES - 1
            if is_last or not _is_retryable_upload_error(exc):
                logger.error(
                    f"_upload_single_file {s3_key}: failed after "
                    f"{attempt + 1}/{_UPLOAD_MAX_RETRIES} attempts "
                    f"({file_size} bytes, {local_path}): {repr(exc)}"
                )
                raise
            backoff = min(60.0, 4.0 * (2**attempt))
            delay = random.uniform(0, backoff)
            logger.warning(
                f"_upload_single_file {s3_key}: attempt "
                f"{attempt + 1}/{_UPLOAD_MAX_RETRIES} failed "
                f"({repr(exc)}); retry in {delay:.1f}s"
            )
            await asyncio.sleep(delay)

    raise RuntimeError("_upload_single_file retry loop fell through")


class _UploadProgress:
    """Per-file transfer state for the watchdog.

    Restarted at the top of every upload *attempt*, because a retry re-uploads
    the file from byte zero. Carrying the failed attempt's bytes and start time
    forward would report throughput that was never achieved — and worse, would
    let a wedged retry keep showing a healthy MiB/s off the back of whatever the
    previous attempt managed. That is precisely the signal this exists to give,
    and expiry retries (now that a lapsed lease is retryable) are exactly when
    it would have lied.
    """

    def __init__(self) -> None:
        self._started_at: dict[str, float] = {}
        self._sent: dict[str, int] = {}
        self._attempt: dict[str, int] = {}

    def restart(self, s3_key: str) -> "Callable[[int], None]":
        """Begin a new attempt for *s3_key*; return that attempt's byte sink.

        The sink is bound to this attempt rather than to the key because a
        timed-out upload is *abandoned*, not awaited (see
        :func:`_upload_with_timeout`): its part uploaders keep running and keep
        invoking their callback for seconds afterwards. Measured against a real
        multipart upload, five further callbacks landed after the abandon. A
        key-scoped sink would credit those bytes to the retry that has since
        started — reintroducing exactly the inflated MiB/s that restarting is
        here to prevent, and on a wedged retry at that.
        """
        attempt = self._attempt.get(s3_key, 0) + 1
        self._attempt[s3_key] = attempt
        self._started_at[s3_key] = time.monotonic()
        self._sent[s3_key] = 0

        def advance(sent_bytes: int) -> None:
            if self._attempt.get(s3_key) != attempt:
                return  # a straggler from an abandoned attempt
            self._sent[s3_key] = self._sent.get(s3_key, 0) + sent_bytes

        return advance

    def finish(self, s3_key: str) -> None:
        self._started_at.pop(s3_key, None)
        self._sent.pop(s3_key, None)
        # Dropping the attempt number invalidates any sink still held by an
        # abandoned upload, so a straggler cannot resurrect a finished key.
        self._attempt.pop(s3_key, None)

    def outstanding(self) -> int:
        return len(self._started_at)

    def oldest(self, limit: int) -> list[tuple[str, float, int]]:
        """``(key, seconds in the current attempt, bytes sent this attempt)``."""
        now = time.monotonic()
        oldest = sorted(self._started_at.items(), key=lambda kv: kv[1])[:limit]
        return [(key, now - at, self._sent.get(key, 0)) for key, at in oldest]


async def _log_upload_progress(
    progress: _UploadProgress,
    total: int,
    interval: float = _UPLOAD_PROGRESS_INTERVAL_SECONDS,
) -> None:
    """Report outstanding uploads every *interval* seconds until cancelled.

    Purely diagnostic, and the only signal emitted while an upload is *still*
    stalled: the pass otherwise logs nothing between "Found N files to upload"
    and "Created files snapshot", so a stall was indistinguishable from a slow
    upload until the caller's deadline fired hours later.

    Reporting elapsed time alone was not enough to tell those apart — a long
    hang and a long crawl produce the same line. The per-file byte counter makes
    the distinction explicit: "0.0 MiB/s" is a wedge, anything else is
    throughput to compare against the deadline the file was given. Both figures
    are scoped to the current attempt (see :class:`_UploadProgress`).
    """
    while True:
        await asyncio.sleep(interval)
        outstanding = progress.outstanding()
        if not outstanding:
            continue
        parts = [
            f"{key} ({age:.0f}s, {sent / _MIB:.0f} MiB, "
            f"{sent / _MIB / max(age, 1e-6):.1f} MiB/s)"
            for key, age, sent in progress.oldest(5)
        ]
        logger.info(
            f"Snapshot upload progress: {total - outstanding}/{total} uploaded, "
            f"{outstanding} queued/in-flight — oldest: {', '.join(parts)}"
        )


async def _retry_failed_uploads(
    bucket,
    files_to_upload: list[tuple[str, str]],
    failed: list[tuple[int, BaseException]],
    sizes: list[int],
    slow_read_roots: frozenset[str] = frozenset(),
    ceiling: float | None = None,
) -> None:
    """Retry files that failed during the first upload pass.

    Transient errors are retried across up to ``_UPLOAD_RETRY_PASSES``
    orchestration-level passes with jittered backoff — a snapshot with thousands
    of tiny files issues thousands of PUTs, so even a low per-file
    transient-failure rate leaves a few stragglers that would otherwise fail the
    whole bundle; spreading retries across backed-off passes rides out a
    transient S3 degradation window without dropping any files. Permanent
    failures (e.g. ``AccessDenied``, ``FileNotFoundError``) are reported
    immediately without retry. Mutates *sizes* in-place, appending successful
    results. Raises RuntimeError if any files still fail after all passes.
    """
    still_failed: list[tuple[str, BaseException]] = [
        (files_to_upload[i][1], exc)
        for i, exc in failed
        if not _is_retryable_upload_error(exc)
    ]
    # (orig_index, last_exc) for files still failing with a transient error.
    pending: list[tuple[int, BaseException]] = [
        (i, exc) for i, exc in failed if _is_retryable_upload_error(exc)
    ]

    deadline = time.monotonic() + _UPLOAD_RETRY_TOTAL_BUDGET_S
    for pass_num in range(_UPLOAD_RETRY_PASSES):
        if not pending:
            break
        if pass_num > 0:
            # Stop before a further pass once the overall time budget is spent —
            # a wedged file (retryable TimeoutError) burns ~3×300s per pass, so
            # the budget, not the pass count, is what bounds the worst case.
            if time.monotonic() >= deadline:
                logger.warning(
                    f"snapshot upload retry budget "
                    f"({_UPLOAD_RETRY_TOTAL_BUDGET_S:.0f}s) spent after pass "
                    f"{pass_num}; {len(pending)} file(s) still failing"
                )
                break
            # Backoff between passes so retries ride out a transient window
            # rather than hammering S3 immediately.
            delay = min(
                _UPLOAD_RETRY_PASS_MAX_DELAY_S,
                _UPLOAD_RETRY_PASS_BASE_DELAY_S * (2 ** (pass_num - 1)),
            )
            await asyncio.sleep(random.uniform(0, delay))
        logger.warning(
            f"{len(pending)}/{len(files_to_upload)} files failed with transient "
            f"errors; retry pass {pass_num + 1}/{_UPLOAD_RETRY_PASSES}"
        )
        retry_results = await asyncio.gather(
            *(
                _upload_single_file(
                    bucket,
                    files_to_upload[i][0],
                    files_to_upload[i][1],
                    slow_read=_is_under_roots(files_to_upload[i][0], slow_read_roots),
                    ceiling=ceiling,
                )
                for i, _ in pending
            ),
            return_exceptions=True,
        )
        next_pending: list[tuple[int, BaseException]] = []
        for (orig_i, _prev), retry_result in zip(pending, retry_results, strict=True):
            if isinstance(retry_result, BaseException):
                if _is_retryable_upload_error(retry_result):
                    next_pending.append((orig_i, retry_result))
                else:
                    still_failed.append((files_to_upload[orig_i][1], retry_result))
            else:
                sizes.append(retry_result)
        pending = next_pending

    # `pending` is non-empty only when the pass count or the time budget ran
    # out first — either way these files never uploaded, so they're hard failures.
    for orig_i, exc in pending:
        still_failed.append((files_to_upload[orig_i][1], exc))

    if still_failed:
        file_list = ", ".join(key for key, _ in still_failed[:5])
        suffix = f" (and {len(still_failed) - 5} more)" if len(still_failed) > 5 else ""
        raise RuntimeError(
            f"{len(still_failed)} file(s) failed after retries: {file_list}{suffix}"
        )


def _collect_subsystem_files(
    subsystems: list[str], prefix: str, exclude_globs: Sequence[str] | None = None
) -> list[tuple[str, str]]:
    """Collect (local_path, s3_key) pairs for all files in the given subsystems."""
    files: list[tuple[str, str]] = []
    for subsystem in subsystems:
        subsystem_path = f"/{subsystem}"
        for path, arcname in iter_paths(
            subsystem_path, subsystem, exclude_globs=exclude_globs
        ):
            s3_key = f"{prefix}/{arcname}"
            files.append((str(path), s3_key))
    return files


# Level 3: this runs on the live sandbox during snapshot finalization, so
# build latency matters more than squeezing out the last few percent.
# threads=-1 uses every core the sandbox has.
_ZSTD_LEVEL = 3
_ZSTD_THREADS = -1


def _normalize_tarinfo(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo:
    """Strip nondeterministic metadata so the archive is reproducible.

    Grading reads entry *content* only; clearing mtime/mode/owner keeps the
    archive stable and avoids leaking local sandbox file attributes.
    """
    tarinfo.mtime = 0
    tarinfo.mode = 0o644
    tarinfo.uid = tarinfo.gid = 0
    tarinfo.uname = tarinfo.gname = ""
    return tarinfo


def _build_snapshot_archive_file(
    files_to_upload: list[tuple[str, str]], prefix: str, archive_path: str
) -> None:
    """Build a tar.zst of all snapshot files at *archive_path* (sync, threaded).

    Entry names are relative to the snapshot prefix (``filesystem/...``,
    ``.apps_data/...``) — the same layout grading's per-file download
    produces, so consumers can use either interchangeably.

    Level 3: this runs on the live sandbox during snapshot finalization, so
    build latency matters more than squeezing out the last few percent.

    ``dereference=True`` archives symlink *targets* as regular files, matching
    the per-file upload path (``iter_paths`` follows symlinks via ``is_file``
    and uploads the target bytes). Without it, symlinks become link entries
    that the grading reader's transcode drops as non-regular members, so the
    tar.zst and per-file fallback would disagree when symlinks exist.
    """
    cctx = zstandard.ZstdCompressor(level=_ZSTD_LEVEL, threads=_ZSTD_THREADS)
    with (
        open(archive_path, "wb") as out_f,
        cctx.stream_writer(out_f) as zst_writer,
        tarfile.open(fileobj=zst_writer, mode="w|", dereference=True) as tar,
    ):
        for local_path, s3_key in files_to_upload:
            arcname = s3_key[len(prefix) :].lstrip("/")
            tar.add(local_path, arcname=arcname, filter=_normalize_tarinfo)


async def _upload_snapshot_zip(
    bucket, files_to_upload: list[tuple[str, str]], prefix: str
) -> None:
    """Build and upload a single tar.zst copy of the snapshot.

    Stored at ``snapshot_zips/{prefix}.tar.zst`` so grading can fetch the
    whole snapshot with one GET. Failures are non-fatal: consumers fall back
    to the per-file prefix download when the archive is absent.
    """
    fd, archive_path = tempfile.mkstemp(suffix=".snapshot.tar.zst")
    os.close(fd)
    try:
        await asyncio.to_thread(
            _build_snapshot_archive_file, files_to_upload, prefix, archive_path
        )
        archive_key = f"snapshot_zips/{prefix}.tar.zst"
        archive_size = await _upload_single_file(bucket, archive_path, archive_key)
        logger.info(
            f"Uploaded snapshot archive ({archive_size} bytes, "
            f"{len(files_to_upload)} files) to {archive_key}"
        )
    except Exception:
        logger.opt(exception=True).warning(
            f"Failed to build/upload snapshot archive for {prefix} — "
            f"consumers will fall back to per-file download"
        )
    finally:
        try:
            os.unlink(archive_path)
        except OSError:
            pass


async def handle_snapshot_s3_files(
    snapshot_id: str | None = None,
    pre_snapshot_hooks: list[LifecycleHook] | None = None,
    s3_credentials: S3Credentials | RefreshableS3Credentials | None = None,
    snapshot_zip_enabled: bool = True,
    exclude_globs: Sequence[str] | None = None,
    mounted_subsystems: Sequence[str] | None = None,
    job_deadline_seconds: float | None = None,
) -> SnapshotFilesResult:
    """Upload all subsystem files individually to S3.

    Entry point for the /data/snapshot/s3?format=files endpoint. Runs any
    pre-snapshot hooks first, then uploads each file from 'filesystem' and
    '.apps_data' subsystems individually to S3, preserving directory structure.
    This format is compatible with grading and snapshot diffing which expect
    individual files.

    Files are uploaded to:
    s3://{bucket}/{prefix}/{snapshot_id}/filesystem/...
    s3://{bucket}/{prefix}/{snapshot_id}/.apps_data/...

    The snapshot includes a unique ID and can be called multiple times
    to create incremental snapshots of the environment state.

    Implementation notes:
    - Uses concurrent uploads (up to 10 parallel) for speed
    - Files < 20 MiB use single PUT; larger files use multipart upload_file
    - Per-file transient errors are retried with jittered backoff
    - Failed files are retried as a batch after the first pass completes

    Args:
        snapshot_id: Optional unique identifier for this snapshot, preallocated
            by caller
        pre_snapshot_hooks: Optional list of hooks to run before creating snapshot
            (e.g., database dumps)
        snapshot_zip_enabled: When True, also build a prebuilt single-ZIP copy
            of the snapshot for one-GET grading downloads (per-world gated)
        exclude_globs: Optional fnmatch patterns tested against each file's
            archive name; matching files are excluded from the snapshot
        mounted_subsystems: Subsystem roots that are LIVE 9p image mounts at
            capture time, so their files get the widened per-attempt upload
            budget. Only the caller knows this — mounting is a Modal
            control-plane call made from the agent process, invisible from in
            here. Omitted means "everything is local disk", which is what every
            unmounted run and every caller older than this field reports.
        job_deadline_seconds: How long the caller polls before abandoning the
            job, used to bound the widened budget so a file's retry sequence
            cannot outlive it. See ``slow_read_ceiling``.

    Returns:
        SnapshotFilesResult containing:
        - snapshot_id: Unique identifier for this snapshot
        - files_uploaded: Number of files uploaded
        - total_bytes: Total size of all files uploaded

    Raises:
        HTTPException: If S3 is not configured, hooks fail, or upload fails
    """
    if snapshot_id is None:
        snapshot_id = f"snap_{uuid().hex}"
    elif not _is_valid_snapshot_id(snapshot_id):
        raise HTTPException(status_code=400, detail="Invalid snapshot ID")
    await get_coordinator().finish_actions()

    # 1. Run pre-snapshot hooks in parallel (e.g., database dumps — services have isolated state)
    files_hook_timings: list[HookTiming] = []
    if pre_snapshot_hooks:
        logger.info(f"Running {len(pre_snapshot_hooks)} pre-snapshot hook(s)")
        try:
            files_hook_timings = await run_lifecycle_hooks(
                pre_snapshot_hooks, phase="pre_snapshot"
            )
            logger.info("All pre-snapshot hooks completed")
        except RuntimeError as e:
            logger.error(f"Pre-snapshot hook failed: {repr(e)}")
            raise HTTPException(status_code=500, detail=str(e)) from e

    prefix = (
        settings.S3_SNAPSHOTS_PREFIX.rstrip("/") + "/"
        if settings.S3_SNAPSHOTS_PREFIX
        else ""
    )
    prefix += snapshot_id

    subsystems = [settings.FILESYSTEM_SUBSYSTEM_NAME, settings.APPS_DATA_SUBSYSTEM_NAME]

    # Intersected with the roots this snapshot actually walks, so a caller naming
    # a root we do not capture (a staging mount under `/.world_src`, say) cannot
    # widen the budget for anything. The scaling is a safety valve for a known
    # slow read, and it should never be reachable by an unrecognised name.
    slow_read_roots = frozenset(mounted_subsystems or ()) & frozenset(subsystems)
    ceiling = slow_read_ceiling(job_deadline_seconds)

    logger.debug(
        f"Starting files snapshot {snapshot_id} for subsystems: {', '.join(subsystems)}"
    )
    logger.debug(f"Target S3 location: s3://{settings.S3_SNAPSHOTS_BUCKET}/{prefix}/")
    if slow_read_roots:
        logger.info(
            f"Snapshot {snapshot_id}: reading {', '.join(sorted(slow_read_roots))} "
            f"over a live image mount; per-file upload budget scaled by "
            f"{_UPLOAD_MOUNT_SLOWDOWN_FACTOR:g}x"
            + (f", capped at {ceiling:.0f}s" if ceiling is not None else "")
        )

    try:
        files_to_upload: list[tuple[str, str]] = _collect_subsystem_files(
            subsystems, prefix, exclude_globs=exclude_globs
        )

        logger.debug(f"Found {len(files_to_upload)} files to upload")

        if not files_to_upload:
            return SnapshotFilesResult(
                snapshot_id=snapshot_id,
                files_uploaded=0,
                total_bytes=0,
                hook_timings=files_hook_timings,
            )

        async with get_s3_client(s3_credentials) as s3:
            bucket = await s3.Bucket(settings.S3_SNAPSHOTS_BUCKET)

            # First pass: upload all files, collecting failures instead of
            # aborting on the first error. Outstanding keys are tracked so the
            # watchdog can name a slow/stalled file while it is still running.
            progress = _UploadProgress()

            async def _tracked(local_path: str, s3_key: str) -> int:
                # Marked outstanding before the concurrency gate so a file
                # still queued is visible; the per-attempt restart inside
                # _upload_single_file supersedes this sink.
                progress.restart(s3_key)
                try:
                    return await _upload_single_file(
                        bucket,
                        local_path,
                        s3_key,
                        progress=progress,
                        slow_read=_is_under_roots(local_path, slow_read_roots),
                        ceiling=ceiling,
                    )
                finally:
                    progress.finish(s3_key)

            watchdog = asyncio.create_task(
                _log_upload_progress(progress, len(files_to_upload))
            )
            try:
                results = await asyncio.gather(
                    *(
                        _tracked(local_path, s3_key)
                        for local_path, s3_key in files_to_upload
                    ),
                    return_exceptions=True,
                )
            finally:
                watchdog.cancel()

            # Separate successes from failures so we can retry just the
            # failed files instead of restarting the entire snapshot.
            sizes: list[int] = []
            failed: list[tuple[int, BaseException]] = []
            for i, result in enumerate(results):
                if isinstance(result, BaseException):
                    local_path, s3_key = files_to_upload[i]
                    logger.warning(
                        f"Upload failed for {s3_key} ({local_path}): {repr(result)}"
                    )
                    failed.append((i, result))
                else:
                    sizes.append(result)

            # Retry failed files once more (per-file retry already ran
            # inside _upload_single_file, so this is a second chance after
            # a brief pause for transient network recovery).
            if failed:
                await _retry_failed_uploads(
                    bucket, files_to_upload, failed, sizes, slow_read_roots, ceiling
                )

            files_uploaded = len(sizes)
            total_bytes = sum(sizes)

            # Emit snapshot size + sandbox peak memory. Peak memory is captured
            # here, at end-of-run, so it reflects the populate / big-DB load
            # that drives the env-sandbox OOM. Tagged by a coarse snapshot-size
            # bucket; never raises (fire-and-forget).
            wbucket = f"snapshot_size_bucket:{snapshot_size_bucket(total_bytes)}"
            distribution(
                "studio.trajectory.snapshot.total_bytes", total_bytes, tags=[wbucket]
            )
            distribution(
                "studio.trajectory.snapshot.file_count", files_uploaded, tags=[wbucket]
            )
            distribution(
                "studio.trajectory.snapshot.peak_memory_bytes",
                peak_memory_bytes(),
                tags=[wbucket],
            )

            # Also upload a single-ZIP copy for one-GET grading downloads.
            # Files are already on local disk, so this costs one zip pass.
            # Gated per-world via `snapshot_zip_enabled`; consumers fall back to
            # the per-file prefix download when the prebuilt ZIP is absent.
            #
            # "Already on local disk" does not hold for a mounted root: the
            # archive is a SECOND full-tree read, so it pays the 9p penalty
            # again. It is built regardless, because the costs are asymmetric —
            # building it spends time bounded by the caller's snapshot polling
            # deadline, while omitting it charges every future grade of this
            # trajectory a prefix LIST plus one GET per object. On the
            # large-snapshot campaign lane a grade spends 112s of 446s pulling
            # ~14.9 GB, and this archive is what holds that to one GET.
            #
            # The upload below keeps the default `slow_read=False`: the archive
            # is read back from a local tempfile whatever tree it was built
            # from, so the widened mount budget does not apply to it.
            if snapshot_zip_enabled:
                await _upload_snapshot_zip(bucket, files_to_upload, prefix)

        logger.info(
            f"Created files snapshot {snapshot_id}: {files_uploaded} files, {total_bytes} bytes"
        )

        return SnapshotFilesResult(
            snapshot_id=snapshot_id,
            files_uploaded=files_uploaded,
            total_bytes=total_bytes,
            hook_timings=files_hook_timings,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating files snapshot {snapshot_id}: {repr(e)}")
        s3_location = (
            f"s3://{settings.S3_SNAPSHOTS_BUCKET}/{prefix}/"
            if settings.S3_SNAPSHOTS_BUCKET
            else "unknown location"
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create files snapshot {snapshot_id} at {s3_location}: {str(e)}",
        ) from e
