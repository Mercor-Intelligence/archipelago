"""Utility functions for populating subsystems from S3."""

import asyncio
import os
import random
import time
import traceback
from typing import Any

from aiohttp import ClientError as AiohttpClientError
from aiohttp import ClientPayloadError, ServerDisconnectedError
from botocore.exceptions import ClientError
from fastapi import HTTPException
from loguru import logger

from runner.utils.decorators import with_concurrency_limit, with_retry
from runner.utils.metrics import distribution, snapshot_size_bucket
from runner.utils.s3 import S3Credentials, get_s3_client
from runner.utils.s3_transfer import get_s5cmd_downloader, key_is_eligible

from .models import PopulateResult, PopulateSource


def _s5cmd_aws_env(credentials: S3Credentials | None) -> dict[str, str] | None:
    """AWS env vars to hand the s5cmd subprocess, or None to inherit the chain.

    s5cmd is a separate process and can't read aioboto3's in-process session, so
    explicit short-lived creds from the populate request are injected via env.
    """
    if credentials is None:
        return None
    env = {
        "AWS_ACCESS_KEY_ID": credentials.access_key_id,
        "AWS_SECRET_ACCESS_KEY": credentials.secret_access_key.get_secret_value(),
        "AWS_REGION": credentials.region,
        "AWS_DEFAULT_REGION": credentials.region,
    }
    if credentials.session_token is not None:
        env["AWS_SESSION_TOKEN"] = credentials.session_token.get_secret_value()
    return env


def _emit_populate_download_metrics(
    backend: str, elapsed: float, total_bytes: int, file_count: int
) -> None:
    """Emit per-source populate download metrics, tagged by backend for the A/B."""
    tags = [
        f"backend:{backend}",
        f"snapshot_size_bucket:{snapshot_size_bucket(total_bytes)}",
    ]
    distribution("studio.trajectory.populate_download_seconds", elapsed, tags=tags)
    distribution(
        "studio.trajectory.populate_download_files", float(file_count), tags=tags
    )
    if total_bytes > 0:
        distribution(
            "studio.trajectory.populate_download_bytes", float(total_bytes), tags=tags
        )


# Objects are downloaded with parallel byte-range GET requests against the
# low-level async client and reassembled on disk.  Running the ranges
# concurrently speeds up large objects (e.g. MySQL CSVs) versus a single
# sequential stream, and per-chunk retry stops one failed range from
# restarting the whole transfer.
_RANGE_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB per range request
_RANGE_MAX_CONCURRENCY = 10  # parallel range requests per file
_RANGE_CHUNK_MAX_RETRIES = 5  # retries per individual chunk

# Exception types that are retryable for S3 downloads.
_RETRYABLE_EXCEPTIONS = (
    ClientError,
    AiohttpClientError,
    ClientPayloadError,
    ServerDisconnectedError,
    ConnectionResetError,
    TimeoutError,
)


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


async def _download_chunk_with_retry(
    s3_client: Any,
    bucket_name: str,
    object_key: str,
    target_path: str,
    start: int,
    end: int,
) -> None:
    """Download a single byte range of an S3 object, retrying on transient errors.

    Each chunk is retried independently so a failure at one offset doesn't
    discard progress on other chunks.
    """
    chunk_label = f"bytes {start}-{end} of {object_key}"
    for attempt in range(1, _RANGE_CHUNK_MAX_RETRIES + 1):
        try:
            resp = await s3_client.get_object(
                Bucket=bucket_name,
                Key=object_key,
                Range=f"bytes={start}-{end}",
            )
            data = await resp["Body"].read()

            # Write at the correct offset — file is pre-allocated so r+b works.
            with open(target_path, "r+b") as f:
                f.seek(start)
                f.write(data)
            return
        except _RETRYABLE_EXCEPTIONS as e:
            if attempt >= _RANGE_CHUNK_MAX_RETRIES:
                logger.error(
                    f"Chunk {chunk_label} failed after {_RANGE_CHUNK_MAX_RETRIES} attempts: {repr(e)}"
                )
                raise
            backoff = 1.5 * (2 ** (attempt - 1))
            jitter = random.uniform(0, 1.0)
            logger.warning(
                f"Chunk {chunk_label} attempt {attempt}/{_RANGE_CHUNK_MAX_RETRIES} "
                f"failed: {repr(e)}, retrying in {backoff + jitter:.1f}s"
            )
            await asyncio.sleep(backoff + jitter)


async def _download_with_ranges(
    s3_client: Any,
    bucket_name: str,
    object_key: str,
    target_path: str,
    total_size: int,
) -> None:
    """Download an S3 object using byte-range requests with per-chunk retry.

    Splits the object into ``_RANGE_CHUNK_SIZE`` pieces, downloads them in
    parallel (bounded by ``_RANGE_MAX_CONCURRENCY``), and writes each piece at
    the correct file offset.  Individual chunk failures are retried without
    discarding progress on other chunks.
    """
    # Pre-allocate target file.
    with open(target_path, "wb") as f:
        f.truncate(total_size)

    # Build (start, end) pairs for each chunk.
    ranges: list[tuple[int, int]] = []
    offset = 0
    while offset < total_size:
        end = min(offset + _RANGE_CHUNK_SIZE - 1, total_size - 1)
        ranges.append((offset, end))
        offset = end + 1

    logger.debug(
        f"Range-downloading {object_key} ({total_size / (1024 * 1024):.1f} MiB) "
        f"in {len(ranges)} chunks"
    )

    semaphore = asyncio.Semaphore(_RANGE_MAX_CONCURRENCY)

    async def _bounded_chunk(start: int, end: int) -> None:
        async with semaphore:
            await _download_chunk_with_retry(
                s3_client,
                bucket_name,
                object_key,
                target_path,
                start,
                end,
            )

    # Use explicit tasks so we can cancel orphans on failure.  Plain
    # asyncio.gather() does NOT cancel siblings when one task raises, which
    # would leave background tasks writing to a file that the outer retry is
    # about to truncate and re-download.
    tasks = [asyncio.create_task(_bounded_chunk(s, e)) for s, e in ranges]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


@with_concurrency_limit(max_concurrency=100)
@with_retry(
    max_retries=5,
    retry_on=_RETRYABLE_EXCEPTIONS,
)
async def _download_single_object(
    obj_summary: Any,
    key: str,
    subsystem_root: str,
    s3_client: Any,
    bucket_name: str,
) -> None:
    """Download a single S3 object to disk.

    Uses manual byte-range requests against the low-level async client
    (``_download_with_ranges``) so that individual chunk failures don't restart
    the entire download.  Small objects resolve to a single range.

    This function is decorated with concurrency limiting (max 100 concurrent downloads)
    and retry logic for transient S3 errors.

    Args:
        obj_summary: S3 object summary from bucket.objects.filter()
        key: S3 prefix/key used to calculate relative path
        subsystem_root: Root directory for the subsystem
        s3_client: Low-level aiobotocore S3 client (for range-based downloads)
        bucket_name: S3 bucket name string (for range-based downloads)

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

    file_size = getattr(obj_summary, "size", None)
    if asyncio.iscoroutine(file_size):
        file_size = await file_size
    file_size = file_size or 0

    logger.debug(
        f"Downloading {obj_summary.key} -> {target_path}"
        f" ({file_size / (1024 * 1024):.1f} MiB)"
    )

    await _download_with_ranges(
        s3_client=s3_client,
        bucket_name=bucket_name,
        object_key=obj_summary.key,
        target_path=target_path,
        total_size=file_size,
    )
    logger.debug(f"Successfully downloaded {obj_summary.key}")


async def download_objects(
    bucket: str,
    key: str,
    subsystem: str,
    s3_credentials: S3Credentials | None = None,
    backend: str = "boto3",
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
    subsystem_root = f"/{subsystem}"
    os.makedirs(subsystem_root, exist_ok=True)

    logger.debug(
        f"Downloading objects from s3://{bucket}/{key} to subsystem '{subsystem}'"
    )

    start_time = time.perf_counter()

    async with get_s3_client(credentials=s3_credentials) as s3res:
        bucket_res = await s3res.Bucket(bucket)
        # Low-level client for range-based downloads of large objects.
        s3_client = s3res.meta.client
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

            logger.debug(f"Found {len(objects_to_download)} object(s) to download")

            # Best-effort total size from the list response (no extra HEADs);
            # only summed when sizes are already loaded as ints.
            total_bytes = sum(
                s
                for o in objects_to_download
                if isinstance((s := getattr(o, "size", None)), int)
            )

            # s5cmd is eligible only for a true prefix download (every object is
            # a child of `key/`), an allowlisted snapshot key, and the binary
            # present. The single-object case (rel -> basename) stays on boto3,
            # whose layout the `cp prefix/*` wildcard wouldn't reproduce.
            norm_key = key.rstrip("/")
            downloader = get_s5cmd_downloader(backend)
            prefix_child = all(
                o.key.startswith(f"{norm_key}/") for o in objects_to_download
            )
            used_backend = "boto3"

            if downloader is not None and prefix_child and key_is_eligible(norm_key):
                try:
                    await downloader.download_prefix(
                        bucket,
                        norm_key,
                        subsystem_root,
                        extra_env=_s5cmd_aws_env(s3_credentials),
                    )
                    used_backend = "s5cmd"
                except Exception:
                    logger.opt(exception=True).warning(
                        f"s5cmd populate failed for s3://{bucket}/{norm_key}; "
                        f"falling back to boto3 per-object download"
                    )

            if used_backend == "boto3":
                # Per-object byte-range download (concurrency-limited + retried
                # via the decorators on _download_single_object). fail-fast.
                download_tasks = [
                    _download_single_object(
                        obj_summary=obj_summary,
                        key=key,
                        subsystem_root=subsystem_root,
                        s3_client=s3_client,
                        bucket_name=bucket,
                    )
                    for obj_summary in objects_to_download
                ]
                await asyncio.gather(*download_tasks)

            objects_downloaded = len(objects_to_download)
            elapsed = time.perf_counter() - start_time
            logger.info(
                f"Downloaded {objects_downloaded} object(s) from s3://{bucket}/{key} "
                f"to {subsystem_root} in {elapsed:.2f}s via {used_backend}"
            )

            _emit_populate_download_metrics(
                used_backend, elapsed, total_bytes, objects_downloaded
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
    s3_credentials: S3Credentials | None = None,
    backend: str = "boto3",
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
        s3_credentials: Optional S3 credentials to use for the populate operation

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
            s3_credentials=s3_credentials,
            backend=backend,
        )

        total_objects += objects_count

    return PopulateResult(objects_added=total_objects)
