"""S3 utilities for downloading snapshots stored as individual files.

Playground snapshots are stored as individual files under an S3 prefix:
    s3://{bucket}/playgrounds/{snapshot_id}/filesystem/...
    s3://{bucket}/playgrounds/{snapshot_id}/.apps_data/...

This module provides utilities to download all files under a prefix and
package them into a ZIP file in memory for processing.
"""

import io
import os
import shutil
import tempfile
import zipfile
from typing import IO, Any
from urllib.parse import urlparse

from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

_IN_MEMORY_THRESHOLD = 10 * 1024 * 1024  # 10 MiB — stream to disk above this

try:
    from modal_helpers import _get_s3_session
except ModuleNotFoundError:
    import aioboto3

    logger.info("modal_helpers not available, using default aioboto3.Session()")

    def _get_s3_session() -> aioboto3.Session:
        return aioboto3.Session()


# Chunk size for streaming S3 downloads (1MB)
S3_CHUNK_SIZE = 1 * 1024 * 1024

# Default AWS region
AWS_DEFAULT_REGION = "us-west-2"


def parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    """Parse an S3 URI into bucket and prefix.

    Args:
        s3_uri: S3 URI in format s3://bucket/prefix/ or s3://bucket/prefix

    Returns:
        Tuple of (bucket, prefix) with trailing slash stripped from prefix

    Raises:
        ValueError: If URI is not a valid S3 URI
    """
    if not s3_uri.startswith("s3://"):
        raise ValueError(f"Invalid S3 URI (must start with s3://): {s3_uri}")

    parsed = urlparse(s3_uri)
    bucket = parsed.netloc
    prefix = parsed.path.lstrip("/").rstrip("/")

    if not bucket:
        raise ValueError(f"Invalid S3 URI (missing bucket): {s3_uri}")

    return bucket, prefix


async def _try_download_prebuilt_zip(
    session: Any, config: AioConfig, bucket: str, prefix: str
) -> IO[bytes] | None:
    """Download the prebuilt snapshot ZIP for *prefix*, if it exists.

    Snapshots are immutable and their ZIP is written once at snapshot time,
    so an existing ZIP is always current. Returns an open seekable handle to
    an (unlinked) temp file, or None when absent so the caller falls back to
    the per-file prefix download.
    """
    zip_key = f"snapshot_zips/{prefix}.zip"
    async with session.resource(
        "s3", config=config, region_name=AWS_DEFAULT_REGION
    ) as s3:
        s3_object = await s3.Object(bucket, zip_key)
        try:
            response = await s3_object.get()
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise

        content_length = response.get("ContentLength", 0) or 0
        logger.info(
            f"Using prebuilt snapshot zip ({content_length:,} bytes): "
            f"s3://{bucket}/{zip_key}"
        )
        body = response["Body"]
        fd, tmp_path = tempfile.mkstemp(suffix=".snapshot.zip")
        try:
            with open(fd, "wb") as tmp_f:
                while True:
                    chunk = await body.read(S3_CHUNK_SIZE)
                    if not chunk:
                        break
                    tmp_f.write(chunk)
            # Validate the central directory before trusting it — a
            # truncated/corrupt prebuilt zip should fall back to the
            # per-file path, not fail the grade. Opening ZipFile reads
            # only the EOCD/central directory (cheap), not every entry.
            try:
                with zipfile.ZipFile(tmp_path):
                    pass
            except (zipfile.BadZipFile, OSError):
                logger.warning(
                    f"Prebuilt snapshot zip is corrupt, ignoring: "
                    f"s3://{bucket}/{zip_key} — falling back to per-file download"
                )
                os.unlink(tmp_path)
                return None
            handle = open(tmp_path, "rb")
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        # Unlink immediately — POSIX keeps the data alive until the handle
        # closes.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return handle


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
async def download_s3_prefix_as_zip(
    s3_uri: str,
) -> IO[bytes]:
    """Download all files under an S3 prefix and return as a ZIP.

    This function handles S3 URIs that point to a "directory" (prefix) containing
    multiple files, such as playground snapshots stored as individual files.

    Prefers the prebuilt snapshot ZIP at ``snapshot_zips/{prefix}.zip`` (one
    GET) when it exists; falls back to listing the prefix and downloading
    each object individually.

    Args:
        s3_uri: S3 URI pointing to a prefix, e.g., s3://bucket/playgrounds/snap_123/

    Returns:
        Seekable binary file object containing a ZIP archive of all files
        under the prefix

    Raises:
        ValueError: If URI is invalid or no files found under prefix
    """
    bucket, prefix = parse_s3_uri(s3_uri)

    # Ensure prefix ends with "/" to avoid matching sibling prefixes.
    # Without this, prefix "snap_test_4" would also match "snap_test_40/file.txt"
    prefix_with_slash = prefix + "/" if prefix and not prefix.endswith("/") else prefix

    logger.debug(
        f"Downloading S3 prefix as ZIP: bucket={bucket}, prefix={prefix_with_slash}"
    )

    session = _get_s3_session()
    config = AioConfig(signature_version="s3v4")

    # Prefer the prebuilt snapshot ZIP — one GET instead of LIST + N GETs.
    # Any failure falls back to the per-file path instead of failing.
    try:
        prebuilt = await _try_download_prebuilt_zip(session, config, bucket, prefix)
    except Exception:
        logger.opt(exception=True).warning(
            f"Prebuilt zip path failed for {prefix} — falling back to per-file download"
        )
        prebuilt = None
    if prebuilt is not None:
        return prebuilt

    # Use SpooledTemporaryFile to avoid unbounded memory usage — the zip
    # stays in RAM up to 256 MiB, then spills to disk automatically.
    _SPILL_THRESHOLD = 256 * 1024 * 1024
    zip_spool: tempfile.SpooledTemporaryFile[bytes] = tempfile.SpooledTemporaryFile(
        max_size=_SPILL_THRESHOLD, mode="w+b"
    )

    async with session.resource(
        "s3", config=config, region_name=AWS_DEFAULT_REGION
    ) as s3:
        s3_bucket = await s3.Bucket(bucket)

        file_count = 0
        with zipfile.ZipFile(
            zip_spool, "w", zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zip_file:
            async for obj in s3_bucket.objects.filter(Prefix=prefix_with_slash):
                # Get relative path (remove prefix including trailing slash)
                relative_path = obj.key[len(prefix_with_slash) :]
                if not relative_path:
                    # Skip the prefix itself (empty relative path)
                    continue

                # Download object to a temp file, decompress if gzipped,
                # then stream into the zip entry. This avoids holding the
                # full object in memory — critical for multi-GB SQL dumps.
                response = await obj.get()
                body = response["Body"]
                content_length = response.get("ContentLength", 0) or 0

                # Only use the in-memory path when the size is known and
                # positive — a missing/zero ContentLength could hide an
                # arbitrarily large body (we read to EOF either way), so
                # route unknowns to the disk-backed branch.
                if 0 < content_length < _IN_MEMORY_THRESHOLD:
                    # Small objects: in-memory is fine.
                    raw_chunks: list[bytes] = []
                    while True:
                        chunk = await body.read(S3_CHUNK_SIZE)
                        if not chunk:
                            break
                        raw_chunks.append(chunk)
                    with zip_file.open(relative_path, "w") as zip_entry:
                        zip_entry.write(b"".join(raw_chunks))
                else:
                    # Large objects: stream through disk to keep memory
                    # bounded, then stream into the zip entry in chunks.
                    fd, tmp_path = tempfile.mkstemp(suffix=".s3tmp")
                    try:
                        with open(fd, "wb") as tmp_f:
                            while True:
                                chunk = await body.read(S3_CHUNK_SIZE)
                                if not chunk:
                                    break
                                tmp_f.write(chunk)
                        with (
                            open(tmp_path, "rb") as src,
                            zip_file.open(relative_path, "w") as zip_entry,
                        ):
                            shutil.copyfileobj(src, zip_entry, S3_CHUNK_SIZE)
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass

                file_count += 1

        logger.debug(f"Downloaded {file_count} files from s3://{bucket}/{prefix}")

    if file_count == 0:
        logger.warning(f"No files found under S3 prefix: s3://{bucket}/{prefix}")
        # Return empty but valid ZIP
        zip_spool = tempfile.SpooledTemporaryFile(max_size=_SPILL_THRESHOLD, mode="w+b")
        with zipfile.ZipFile(zip_spool, "w", zipfile.ZIP_DEFLATED):
            pass

    zip_spool.seek(0)
    logger.info(
        f"Created ZIP from S3 prefix ({file_count} files): s3://{bucket}/{prefix}"
    )

    return zip_spool


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
async def download_s3_file(s3_uri: str) -> IO[bytes]:
    """Download a single S3 file and return its content.

    Unlike download_s3_prefix_as_zip, this downloads an exact S3 key (not a prefix).
    Use this when the S3 URI points to a single file (e.g., a pre-packaged ZIP).

    Args:
        s3_uri: S3 URI pointing to a single file, e.g., s3://bucket/path/file.zip

    Returns:
        Seekable binary file object containing the file content. Small files
        are in-memory (BytesIO); large files are disk-backed to keep memory
        bounded.

    Raises:
        ValueError: If URI is invalid
        ClientError: If the file doesn't exist or access is denied
    """
    bucket, key = parse_s3_uri(s3_uri)

    logger.debug(f"Downloading S3 file: bucket={bucket}, key={key}")

    session = _get_s3_session()
    config = AioConfig(signature_version="s3v4")

    async with session.resource(
        "s3", config=config, region_name=AWS_DEFAULT_REGION
    ) as s3:
        s3_object = await s3.Object(bucket, key)
        response = await s3_object.get()
        body = response["Body"]
        content_length = response.get("ContentLength", 0) or 0

        # Only use the in-memory path when the size is known and positive
        # — a missing/zero ContentLength could hide an arbitrarily large
        # body (we read to EOF either way), so route unknowns to the
        # disk-backed branch.
        if 0 < content_length < _IN_MEMORY_THRESHOLD:
            # Small files: in-memory is fine.
            chunks: list[bytes] = []
            while True:
                chunk = await body.read(S3_CHUNK_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
            buffer: IO[bytes] = io.BytesIO(b"".join(chunks))
        else:
            # Large files: stream to a temp file and return a handle so
            # memory stays bounded. The path is unlinked immediately; POSIX
            # keeps the data alive until the returned handle is closed.
            fd, tmp_path = tempfile.mkstemp(suffix=".s3tmp")
            try:
                with open(fd, "wb") as tmp_f:
                    while True:
                        chunk = await body.read(S3_CHUNK_SIZE)
                        if not chunk:
                            break
                        tmp_f.write(chunk)
                buffer = open(tmp_path, "rb")
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    buffer.seek(0, io.SEEK_END)
    file_size = buffer.tell()
    buffer.seek(0)
    logger.info(f"Downloaded S3 file ({file_size:,} bytes): s3://{bucket}/{key}")

    return buffer


def is_s3_prefix_uri(s3_uri: str) -> bool:
    """Check if an S3 URI appears to be a prefix (directory) rather than a file.

    Heuristic: URIs ending with / or without a file extension are likely prefixes.

    Args:
        s3_uri: S3 URI to check

    Returns:
        True if the URI looks like a prefix, False if it looks like a single file
    """
    if s3_uri.endswith("/"):
        return True

    # Check if the last path component has an extension
    parsed = urlparse(s3_uri)
    path = parsed.path.rstrip("/")
    if "/" in path:
        filename = path.rsplit("/", 1)[1]
    else:
        filename = path

    # If no extension, likely a prefix
    return "." not in filename
