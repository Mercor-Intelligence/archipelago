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
from collections.abc import Iterator
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
from runner.utils.s3 import S3Credentials, get_s3_client
from runner.utils.settings import get_settings

from ..populate.main import run_lifecycle_hooks
from ..populate.models import HookTiming, LifecycleHook
from .models import SnapshotFilesResult, SnapshotResult
from .streaming import create_tar_gz_stream
from .utils import generate_presigned_url, iter_paths, s3_stream_uploader

settings = get_settings()


def _is_valid_snapshot_id(s: str) -> bool:
    ALLOWED = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    )
    return bool(s) and all(c in ALLOWED for c in s)


async def handle_snapshot(
    pre_snapshot_hooks: list[LifecycleHook] | None = None,
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
            stream_hook_timings = await run_lifecycle_hooks(pre_snapshot_hooks)
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
            create_tar_gz_stream(subsystems, snapshot_id, iter_paths),
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
    s3_credentials: S3Credentials | None = None,
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
            snapshot_hook_timings = await run_lifecycle_hooks(pre_snapshot_hooks)
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
                    for path, arcname in iter_paths(subsystem_path, subsystem):
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
_UPLOAD_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
    ConnectionResetError,
    TimeoutError,
)
# S3 error codes worth retrying — mirrors the set in rl-studio's helpers.py.
_UPLOAD_RETRYABLE_S3_CODES = frozenset(
    {
        "IncompleteBody",
        "RequestTimeout",
        "ServiceUnavailable",
        "SlowDown",
        "InternalError",
        "ThrottlingException",
    }
)


def _is_retryable_upload_error(exc: BaseException) -> bool:
    """Return True if the exception is a transient S3/network error."""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return code in _UPLOAD_RETRYABLE_S3_CODES
    return isinstance(exc, _UPLOAD_RETRYABLE_EXCEPTIONS)


@with_concurrency_limit(max_concurrency=8)
async def _upload_single_file(s3_bucket, local_path: str, s3_key: str) -> int:
    """Upload a single file to S3 and return its size.

    Files >= 20 MiB use path-based ``upload_file`` which streams from disk
    via seek-based chunking and retries failed parts automatically.  Smaller
    files use a single PUT.

    Concurrency is capped at 8 files with 10 multipart parts each to balance
    throughput against memory: 8 × 10 × 20 MiB ≈ 1.6 GB peak, within the
    same budget that previously caused OOM at 20 concurrent uploads.

    Transient S3/network errors are retried up to ``_UPLOAD_MAX_RETRIES`` times
    with jittered exponential backoff.

    Args:
        s3_bucket: S3 bucket resource
        local_path: Local file path to upload
        s3_key: S3 key (destination path)

    Returns:
        Size of the uploaded file in bytes
    """
    from boto3.s3.transfer import TransferConfig

    file_size = os.path.getsize(local_path)

    for attempt in range(_UPLOAD_MAX_RETRIES):
        try:
            s3_object = await s3_bucket.Object(s3_key)

            # Stream files >= 20 MiB via upload_file (path-based) to avoid
            # reading them into memory.  upload_file uses seek-based chunking
            # so failed multipart parts can be retried from the correct offset,
            # unlike upload_fileobj which streams forward-only.
            if file_size >= 20 * 1024 * 1024:  # 20 MiB
                transfer_config = TransferConfig(
                    multipart_threshold=20 * 1024 * 1024,  # 20 MiB
                    multipart_chunksize=20 * 1024 * 1024,
                    max_concurrency=10,
                )
                await s3_object.upload_file(local_path, Config=transfer_config)
            else:
                async with aiofiles.open(local_path, "rb") as f:
                    content = await f.read()
                await s3_object.put(Body=content)

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


async def _retry_failed_uploads(
    bucket,
    files_to_upload: list[tuple[str, str]],
    failed: list[tuple[int, BaseException]],
    sizes: list[int],
) -> None:
    """Retry files that failed during the first upload pass.

    Only transient errors are retried — permanent failures (e.g.
    ``AccessDenied``, ``FileNotFoundError``) are reported immediately.
    Mutates *sizes* in-place, appending successful retry results.
    Raises RuntimeError if any files still fail after retry.
    """
    retryable = [(i, exc) for i, exc in failed if _is_retryable_upload_error(exc)]
    permanent = [(i, exc) for i, exc in failed if not _is_retryable_upload_error(exc)]

    still_failed: list[tuple[str, BaseException]] = []
    for orig_i, exc in permanent:
        _local_path, s3_key = files_to_upload[orig_i]
        still_failed.append((s3_key, exc))

    if retryable:
        logger.warning(
            f"{len(retryable)}/{len(files_to_upload)} files failed "
            f"with transient errors; retrying"
        )
        retry_tasks = [
            _upload_single_file(
                bucket,
                files_to_upload[i][0],
                files_to_upload[i][1],
            )
            for i, _ in retryable
        ]
        retry_results = await asyncio.gather(*retry_tasks, return_exceptions=True)

        for (orig_i, _), retry_result in zip(retryable, retry_results, strict=True):
            if isinstance(retry_result, BaseException):
                _local_path, s3_key = files_to_upload[orig_i]
                still_failed.append((s3_key, retry_result))
            else:
                sizes.append(retry_result)

    if still_failed:
        file_list = ", ".join(key for key, _ in still_failed[:5])
        suffix = f" (and {len(still_failed) - 5} more)" if len(still_failed) > 5 else ""
        raise RuntimeError(
            f"{len(still_failed)} file(s) failed after retry: {file_list}{suffix}"
        )


def _collect_subsystem_files(
    subsystems: list[str], prefix: str
) -> list[tuple[str, str]]:
    """Collect (local_path, s3_key) pairs for all files in the given subsystems."""
    files: list[tuple[str, str]] = []
    for subsystem in subsystems:
        subsystem_path = f"/{subsystem}"
        for path, arcname in iter_paths(subsystem_path, subsystem):
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
    s3_credentials: S3Credentials | None = None,
    snapshot_zip_enabled: bool = True,
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
            files_hook_timings = await run_lifecycle_hooks(pre_snapshot_hooks)
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

    logger.debug(
        f"Starting files snapshot {snapshot_id} for subsystems: {', '.join(subsystems)}"
    )
    logger.debug(f"Target S3 location: s3://{settings.S3_SNAPSHOTS_BUCKET}/{prefix}/")

    try:
        files_to_upload: list[tuple[str, str]] = _collect_subsystem_files(
            subsystems, prefix
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
            # aborting on the first error.
            tasks = [
                _upload_single_file(bucket, local_path, s3_key)
                for local_path, s3_key in files_to_upload
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

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
                await _retry_failed_uploads(bucket, files_to_upload, failed, sizes)

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
