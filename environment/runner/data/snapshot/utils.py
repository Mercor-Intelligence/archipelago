"""Utility functions for snapshotting subsystems to S3."""

from contextlib import asynccontextmanager
from pathlib import Path

from loguru import logger

from runner.utils.s3 import get_s3_client
from runner.utils.settings import get_settings

from .streaming import S3StreamUploader

# Get settings at module level
settings = get_settings()


def iter_paths(root_dir: str, arc_prefix: str):
    """Iterate over files in a directory and yield (path, arcname) tuples.

    Recursively walks through a directory tree and yields file paths along
    with their archive names for inclusion in a tar archive. Only files
    are yielded (directories are skipped since tarfile automatically creates
    directory entries when adding files with nested paths).

    Args:
        root_dir: Root directory to scan (e.g., '/filesystem')
        arc_prefix: Prefix to prepend to archive names (e.g., 'filesystem')

    Yields:
        Tuple of (absolute_file_path, archive_name) for each file found.
        Archive names preserve relative directory structure under the prefix.
    """
    base = Path(root_dir)
    if not base.exists():
        logger.debug(f"Skipping missing directory: {root_dir}")
        return
    for path in base.rglob("*"):
        if path.is_file():  # Only yield files, not directories
            arcname = f"{arc_prefix}/{path.relative_to(base)}"
            yield path, arcname


@asynccontextmanager
async def s3_stream_uploader(object_key: str):
    """Create a streaming uploader context manager for S3 multipart upload.

    Creates a streaming uploader that can be used as a file-like object
    with tarfile. The uploader handles multipart upload automatically for
    large files, allowing TB-scale snapshots without memory issues.

    Args:
        object_key: The object key (filename) for the snapshot (e.g., 'snap_abc123.tar.gz')

    Yields:
        S3StreamUploader instance that can be used with tarfile.open()
    """
    bucket = settings.S3_SNAPSHOTS_BUCKET
    key = (
        settings.S3_SNAPSHOTS_PREFIX.rstrip("/") + "/"
        if settings.S3_SNAPSHOTS_PREFIX
        else ""
    )
    key += object_key

    async with get_s3_client() as s3:
        uploader = S3StreamUploader(s3, bucket, key)
        async with uploader:
            yield uploader


async def generate_presigned_url(
    object_key: str, expiration_seconds: int = 604800
) -> str:
    """Generate a pre-signed URL for the uploaded snapshot.

    Args:
        object_key: The object key (filename) for the snapshot
        expiration_seconds: Pre-signed URL expiration in seconds. Default is 604800 (7 days).

    Returns:
        Pre-signed URL string
    """
    key = (
        settings.S3_SNAPSHOTS_PREFIX.rstrip("/") + "/"
        if settings.S3_SNAPSHOTS_PREFIX
        else ""
    )
    key += object_key

    async with get_s3_client() as s3:
        bucket_res = await s3.Bucket(settings.S3_SNAPSHOTS_BUCKET)
        obj = await bucket_res.Object(key)
        presigned_url = await obj.meta.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.S3_SNAPSHOTS_BUCKET, "Key": key},
            ExpiresIn=expiration_seconds,
        )
        return presigned_url
