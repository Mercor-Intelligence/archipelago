"""S3 utilities for downloading snapshots stored as individual files.

Playground snapshots are stored as individual files under an S3 prefix:
    s3://{bucket}/playgrounds/{snapshot_id}/filesystem/...
    s3://{bucket}/playgrounds/{snapshot_id}/.apps_data/...

This module provides utilities to download all files under a prefix and
package them into a ZIP file in memory for processing.
"""

import io
import zipfile
from urllib.parse import urlparse

from aiobotocore.config import AioConfig
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from modal_helpers import _get_s3_session

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


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
async def download_s3_prefix_as_zip(s3_uri: str) -> io.BytesIO:
    """Download all files under an S3 prefix and return as a ZIP in memory.

    This function handles S3 URIs that point to a "directory" (prefix) containing
    multiple files, such as playground snapshots stored as individual files.

    Args:
        s3_uri: S3 URI pointing to a prefix, e.g., s3://bucket/playgrounds/snap_123/

    Returns:
        BytesIO containing a ZIP archive of all files under the prefix

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

    zip_buffer = io.BytesIO()

    async with session.resource(
        "s3", config=config, region_name=AWS_DEFAULT_REGION
    ) as s3:
        s3_bucket = await s3.Bucket(bucket)

        file_count = 0
        with zipfile.ZipFile(
            zip_buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zip_file:
            async for obj in s3_bucket.objects.filter(Prefix=prefix_with_slash):
                # Get relative path (remove prefix including trailing slash)
                relative_path = obj.key[len(prefix_with_slash) :]
                if not relative_path:
                    # Skip the prefix itself (empty relative path)
                    continue

                # Download object content
                response = await obj.get()
                body = response["Body"]

                # Stream into zip file
                with zip_file.open(relative_path, "w") as zip_entry:
                    while True:
                        chunk = await body.read(S3_CHUNK_SIZE)
                        if not chunk:
                            break
                        zip_entry.write(chunk)

                file_count += 1

        logger.debug(f"Downloaded {file_count} files from s3://{bucket}/{prefix}")

    if file_count == 0:
        logger.warning(f"No files found under S3 prefix: s3://{bucket}/{prefix}")
        # Return empty but valid ZIP
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED):
            pass

    zip_buffer.seek(0)
    zip_size = len(zip_buffer.getvalue())
    logger.info(
        f"Created ZIP from S3 prefix ({file_count} files, {zip_size:,} bytes): "
        f"s3://{bucket}/{prefix}"
    )

    return zip_buffer


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
async def download_s3_file(s3_uri: str) -> io.BytesIO:
    """Download a single S3 file and return its content.

    Unlike download_s3_prefix_as_zip, this downloads an exact S3 key (not a prefix).
    Use this when the S3 URI points to a single file (e.g., a pre-packaged ZIP).

    Args:
        s3_uri: S3 URI pointing to a single file, e.g., s3://bucket/path/file.zip

    Returns:
        BytesIO containing the file content

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

        # Stream content into buffer
        buffer = io.BytesIO()
        while True:
            chunk = await body.read(S3_CHUNK_SIZE)
            if not chunk:
                break
            buffer.write(chunk)

    buffer.seek(0)
    file_size = len(buffer.getvalue())
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
