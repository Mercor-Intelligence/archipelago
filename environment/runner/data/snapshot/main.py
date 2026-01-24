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
import tarfile
from collections.abc import Iterator
from uuid import uuid4 as uuid

import aiofiles
from fastapi import HTTPException
from loguru import logger

from runner.utils.decorators import with_concurrency_limit
from runner.utils.s3 import get_s3_client
from runner.utils.settings import get_settings

from ..populate.main import run_lifecycle_hook
from ..populate.models import LifecycleHook
from .models import SnapshotFilesResult, SnapshotResult
from .streaming import create_tar_gz_stream
from .utils import generate_presigned_url, iter_paths, s3_stream_uploader

settings = get_settings()


async def handle_snapshot(
    pre_snapshot_hooks: list[LifecycleHook] | None = None,
) -> tuple[Iterator[bytes], str]:
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
        Tuple of (generator yielding bytes chunks, filename)

    Raises:
        HTTPException: If hooks fail or snapshot creation fails
    """
    snapshot_id = f"snap_{uuid().hex}"
    filename = f"{snapshot_id}.tar.gz"

    # Run pre-snapshot hooks (e.g., database dumps)
    if pre_snapshot_hooks:
        logger.info(f"Running {len(pre_snapshot_hooks)} pre-snapshot hook(s)")
        try:
            for hook in pre_snapshot_hooks:
                await run_lifecycle_hook(hook)
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
        return create_tar_gz_stream(subsystems, snapshot_id, iter_paths), filename
    except Exception as e:
        logger.error(f"Error creating snapshot {snapshot_id}: {repr(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create snapshot {snapshot_id}: {str(e)}",
        ) from e


async def handle_snapshot_s3(
    pre_snapshot_hooks: list[LifecycleHook] | None = None,
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

    snapshot_id = f"snap_{uuid().hex}"

    # 1. Run pre-snapshot hooks (e.g., database dumps)
    if pre_snapshot_hooks:
        logger.info(f"Running {len(pre_snapshot_hooks)} pre-snapshot hook(s)")
        try:
            for hook in pre_snapshot_hooks:
                await run_lifecycle_hook(hook)
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
        async with s3_stream_uploader(object_key) as uploader:
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
        presigned_url = await generate_presigned_url(object_key)

        s3_uri = f"s3://{settings.S3_SNAPSHOTS_BUCKET}/{key}"

        logger.info(
            f"Created snapshot {snapshot_id} ({size_bytes} bytes) with {len(subsystems)} subsystem(s): {', '.join(subsystems)}"
        )

        return SnapshotResult(
            snapshot_id=snapshot_id,
            s3_uri=s3_uri,
            presigned_url=presigned_url,
            size_bytes=size_bytes,
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


@with_concurrency_limit(max_concurrency=20)
async def _upload_single_file(s3_bucket, local_path: str, s3_key: str) -> int:
    """Upload a single file to S3 and return its size.

    This function is decorated with concurrency limiting (max 20 concurrent uploads)
    to avoid overwhelming S3.

    Args:
        s3_bucket: S3 bucket resource
        local_path: Local file path to upload
        s3_key: S3 key (destination path)

    Returns:
        Size of the uploaded file in bytes
    """
    async with aiofiles.open(local_path, "rb") as f:
        content = await f.read()

    s3_object = await s3_bucket.Object(s3_key)
    await s3_object.put(Body=content)
    return len(content)


async def handle_snapshot_s3_files(
    pre_snapshot_hooks: list[LifecycleHook] | None = None,
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
    - Uses concurrent uploads (up to 20 parallel) for speed
    - Uses aiofiles for non-blocking file I/O
    - Files are read entirely into memory before upload (S3 put requires content-length)
    - For very large files (GB+), consider using S3 multipart upload

    Args:
        pre_snapshot_hooks: Optional list of hooks to run before creating snapshot
            (e.g., database dumps)

    Returns:
        SnapshotFilesResult containing:
        - snapshot_id: Unique identifier for this snapshot
        - files_uploaded: Number of files uploaded
        - total_bytes: Total size of all files uploaded

    Raises:
        HTTPException: If S3 is not configured, hooks fail, or upload fails
    """
    snapshot_id = f"snap_{uuid().hex}"

    # 1. Run pre-snapshot hooks (e.g., database dumps)
    if pre_snapshot_hooks:
        logger.info(f"Running {len(pre_snapshot_hooks)} pre-snapshot hook(s)")
        try:
            for hook in pre_snapshot_hooks:
                await run_lifecycle_hook(hook)
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
        files_to_upload: list[tuple[str, str]] = []  # (local_path, s3_key)
        for subsystem in subsystems:
            subsystem_path = f"/{subsystem}"
            for path, arcname in iter_paths(subsystem_path, subsystem):
                s3_key = f"{prefix}/{arcname}"
                files_to_upload.append((str(path), s3_key))

        logger.debug(f"Found {len(files_to_upload)} files to upload")

        if not files_to_upload:
            return SnapshotFilesResult(
                snapshot_id=snapshot_id,
                files_uploaded=0,
                total_bytes=0,
            )

        async with get_s3_client() as s3:
            bucket = await s3.Bucket(settings.S3_SNAPSHOTS_BUCKET)

            tasks = [
                _upload_single_file(bucket, local_path, s3_key)
                for local_path, s3_key in files_to_upload
            ]
            results = await asyncio.gather(*tasks)

            files_uploaded = len(results)
            total_bytes = sum(results)

        logger.info(
            f"Created files snapshot {snapshot_id}: {files_uploaded} files, {total_bytes} bytes"
        )

        return SnapshotFilesResult(
            snapshot_id=snapshot_id,
            files_uploaded=files_uploaded,
            total_bytes=total_bytes,
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
