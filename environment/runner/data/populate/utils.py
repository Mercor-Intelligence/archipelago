"""Utility functions for populating subsystems from S3."""

import asyncio
import os
import time
import traceback
from typing import Any

from aiohttp import ClientError as AiohttpClientError
from aiohttp import ClientPayloadError, ServerDisconnectedError
from botocore.exceptions import ClientError
from fastapi import HTTPException
from loguru import logger

from runner.utils.decorators import with_concurrency_limit, with_retry
from runner.utils.s3 import get_s3_client

from .models import PopulateResult, PopulateSource


def parse_s3_url(url: str) -> tuple[str, str]:
    """Parse S3 URL into bucket and key components.

    Supports standard AWS S3 URL format: s3://bucket/key

    Args:
        url: S3 URL string in standard format (s3://bucket/key)

    Returns:
        Tuple of (bucket, key) where both are stripped of whitespace

    Raises:
        ValueError: If URL format is invalid, bucket is empty, or key is empty
    """
    original_url = url
    url = url.strip()

    # Must start with s3:// prefix
    if not url.startswith("s3://"):
        raise ValueError(
            f"Invalid S3 URL format '{original_url}'. Expected 's3://bucket/key'"
        )

    url = url[5:]  # Remove "s3://"

    # Split on first '/' to separate bucket and key
    if "/" not in url:
        raise ValueError(
            f"Invalid S3 URL format '{original_url}'. Expected 's3://bucket/key'"
        )

    bucket, key = url.split("/", 1)

    # Validate bucket and key are not empty
    if not bucket or not bucket.strip():
        raise ValueError(f"Bucket name cannot be empty in URL: '{original_url}'")

    if not key or not key.strip():
        raise ValueError(f"Key cannot be empty in URL: '{original_url}'")

    return bucket.strip(), key.strip()


async def _download_file_chunked(body: Any, target_path: str, chunk_size: int) -> None:
    """Download a file from S3 body stream to disk in chunks.

    Helper function to download file data from an S3 object body stream
    and write it to a local file path in chunks. Handles cleanup of the
    body stream automatically.

    Args:
        body: S3 object body stream (from response["Body"])
        target_path: Local file path where data should be written
        chunk_size: Size of chunks to read (bytes)

    Raises:
        OSError: If file cannot be written to disk
    """
    with open(target_path, "wb") as dst:
        while True:
            buf = await body.read(chunk_size)
            if not buf:
                break
            _ = dst.write(buf)


def validate_path_safety(rel_path: str, subsystem_root: str) -> str:
    """Validate that a relative path is safe and prevent directory traversal.

    Ensures that the relative path cannot escape the subsystem root directory
    using path traversal techniques (e.g., '../' sequences).

    Args:
        rel_path: Relative path from the S3 prefix to the target file
        subsystem_root: Absolute root directory path for the subsystem (e.g., '/filesystem')

    Returns:
        Absolute target path where the file should be written

    Raises:
        ValueError: If path contains directory traversal attempts (e.g., '..') or
            would escape the subsystem root directory
    """
    # Normalize the path
    normalized = os.path.normpath(rel_path)
    # Check for directory traversal - must check path components, not substring
    # This allows filenames containing ".." (e.g., "file..pdf") while blocking
    # actual traversal attempts (e.g., "../foo" or "foo/../bar")
    path_parts = normalized.split(os.sep)
    if any(part == ".." for part in path_parts) or normalized.startswith("/"):
        raise ValueError(f"Unsafe path detected: {rel_path}")
    # Build absolute path
    target_path = os.path.join(subsystem_root, normalized)
    # Ensure it's still within subsystem root
    abs_subsystem_root = os.path.abspath(subsystem_root)
    abs_target = os.path.abspath(target_path)
    if not abs_target.startswith(abs_subsystem_root):
        raise ValueError(f"Path traversal detected: {rel_path}")
    return target_path


@with_concurrency_limit(max_concurrency=100)
@with_retry(
    max_retries=3,
    retry_on=(
        ClientError,
        AiohttpClientError,
        ClientPayloadError,
        ServerDisconnectedError,
        ConnectionResetError,
        TimeoutError,
    ),
)
async def _download_single_object(
    obj_summary: Any,
    key: str,
    subsystem_root: str,
    chunk_size: int,
) -> None:
    """Download a single S3 object to disk.

    This function is decorated with concurrency limiting (max 100 concurrent downloads)
    and retry logic for transient S3 errors.

    Args:
        obj_summary: S3 object summary from bucket.objects.filter()
        key: S3 prefix/key used to calculate relative path
        subsystem_root: Root directory for the subsystem
        chunk_size: Size of chunks to read (bytes)

    Raises:
        ValueError: If path is unsafe or invalid
        ClientError: If S3 operation fails after retries
        OSError: If file cannot be written to disk
    """
    logger.debug(f"Processing object: {obj_summary.key}")
    # Calculate relative path from prefix
    rel = obj_summary.key[len(key) :].lstrip("/")
    if not rel:
        # If rel is empty, this means the key exactly matches the object key
        # (single object case). Use basename as the relative path.
        rel = os.path.basename(key) or key
        if not rel:
            logger.warning(f"Skipping object with empty basename: {obj_summary.key}")
            return

    # Validate and build safe path
    target_path = validate_path_safety(rel, subsystem_root)

    os.makedirs(os.path.dirname(target_path), exist_ok=True)

    response = await obj_summary.get()
    body = response["Body"]
    logger.debug(f"Downloading {obj_summary.key} -> {target_path}")
    await _download_file_chunked(body, target_path, chunk_size)
    logger.debug(f"Successfully downloaded {obj_summary.key}")


async def download_objects(
    bucket: str,
    key: str,
    subsystem: str,
) -> int:
    """Download objects from S3 and place them in the subsystem directory.

    Handles two cases:
    1. Single object: If the key points to a single object, downloads it directly
       to the subsystem root with its original filename.
    2. Prefix: If the key is a prefix (directory), downloads all objects under
       that prefix, preserving the relative directory structure.

    Objects are downloaded in parallel (up to 100 concurrent downloads) with
    automatic retry on transient S3 errors. If any object fails after retries,
    the entire operation fails.

    Files are written directly to disk without intermediate storage. Existing
    files with the same path are overwritten.

    Args:
        bucket: S3 bucket name
        key: S3 object key (can be a single object or a prefix)
        subsystem: Subsystem name where files should be placed (e.g., 'filesystem')

    Returns:
        Number of objects successfully downloaded

    Raises:
        HTTPException: If S3 operations fail, bucket/key is invalid, no objects
            are found at the specified location, or any object download fails
    """
    chunk_size = 1 * 1024 * 1024  # 1 MiB
    subsystem_root = f"/{subsystem}"
    os.makedirs(subsystem_root, exist_ok=True)

    logger.debug(
        f"Downloading objects from s3://{bucket}/{key} to subsystem '{subsystem}'"
    )

    start_time = time.perf_counter()

    async with get_s3_client() as s3res:
        bucket_res = await s3res.Bucket(bucket)
        logger.debug(f"Connected to S3 bucket: {bucket}")

        try:
            objects_to_download = []
            async for obj_summary in bucket_res.objects.filter(Prefix=key):
                objects_to_download.append(obj_summary)

            if not objects_to_download:
                logger.warning(
                    f"No objects found at s3://{bucket}/{key} for subsystem '{subsystem}'"
                )
                return 0

            logger.debug(
                f"Found {len(objects_to_download)} object(s) to download, starting parallel downloads"
            )

            # Download all objects in parallel (with concurrency limit and retry via decorators)
            # return_exceptions=False means we'll raise on first error (fail fast)
            download_tasks = [
                _download_single_object(
                    obj_summary=obj_summary,
                    key=key,
                    subsystem_root=subsystem_root,
                    chunk_size=chunk_size,
                )
                for obj_summary in objects_to_download
            ]

            await asyncio.gather(*download_tasks)

            objects_downloaded = len(objects_to_download)
            logger.info(
                f"Downloaded {objects_downloaded} object(s) from s3://{bucket}/{key} to {subsystem_root} in {time.perf_counter() - start_time:.2f} seconds"
            )

            return objects_downloaded

        except HTTPException:
            raise
        except ClientError as e:
            raise HTTPException(
                status_code=500,
                detail=f"S3 error accessing s3://{bucket}/{key}: {str(e)}",
            ) from e
        except Exception as e:
            logger.error(
                f"Unexpected error downloading from s3://{bucket}/{key}: {repr(e)}\n{traceback.format_exc()}"
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to download from s3://{bucket}/{key}: {str(e)}",
            ) from e


async def populate_data(
    sources: list[PopulateSource],
) -> PopulateResult:
    """Populate subsystems from S3 sources with overwrite semantics.

    Processes multiple S3 sources in order, downloading objects and placing
    them into their specified subsystem directories. Later sources overwrite
    earlier ones if they have the same destination path.

    Overwrite behavior:
    - Within a single call: Sources processed later in the list overwrite
      earlier sources if they target the same file path.
    - Between calls: New calls overwrite existing files if they have the same
      path. Files that don't conflict are preserved.

    Args:
        sources: List of PopulateSource objects, each specifying an S3 URL
            and target subsystem

    Returns:
        PopulateResult containing the total number of objects added across
        all sources

    Raises:
        HTTPException: If any source fails to download or parse
    """
    total_objects = 0

    for source in sources:
        try:
            bucket, key = parse_s3_url(source.url)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        objects_count = await download_objects(
            bucket=bucket,
            key=key,
            subsystem=source.subsystem,
        )

        total_objects += objects_count

    return PopulateResult(objects_added=total_objects)
