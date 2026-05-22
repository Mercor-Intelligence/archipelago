"""Pydantic models for snapshot operations.

This module defines request and response models for the snapshot endpoint.
"""

from pydantic import BaseModel, Field

from ..populate.models import LifecycleHook


class SnapshotStreamRequest(BaseModel):
    """Request for direct snapshot streaming.

    Optionally includes pre-snapshot hooks that run before the archive is created.
    This allows services to dump their state (e.g., database dumps) to .apps_data
    before snapshotting.

    Used by the /data/snapshot endpoint (direct tar.gz streaming).
    """

    pre_snapshot_hooks: list[LifecycleHook] = Field(
        default_factory=list,
        description="Commands to run before creating the snapshot (e.g., database dumps).",
    )


class SnapshotRequest(BaseModel):
    """Request to create a snapshot and upload to S3.

    Optionally includes pre-snapshot hooks that run before the archive is created.
    This allows services to dump their state (e.g., database dumps) to .apps_data
    before snapshotting.

    Used by the /data/snapshot/s3 endpoint.
    """

    format: str = Field(
        default="files",
        description="Output format: 'tar.gz' (single archive) or 'files' (individual files)",
    )
    pre_snapshot_hooks: list[LifecycleHook] = Field(
        default_factory=list,
        description="Commands to run before creating the snapshot (e.g., database dumps).",
    )


class SnapshotResult(BaseModel):
    """Result of snapshot operation (tar.gz format).

    Returned by the /data/snapshot/s3 endpoint after successfully creating a
    tar.gz archive of all subsystems and uploading it to S3.
    """

    snapshot_id: str = Field(
        ..., description="Unique identifier for this snapshot (format: 'snap_<hex>')"
    )
    s3_uri: str = Field(
        ...,
        description="Full S3 URI of the uploaded snapshot archive (format: 's3://bucket/key')",
    )
    presigned_url: str = Field(
        ...,
        description=(
            "Pre-signed URL for downloading the snapshot archive. Expires in 7 days (604800 seconds)."
        ),
    )
    size_bytes: int = Field(
        ..., description="Size of the snapshot tar.gz archive in bytes"
    )


class SnapshotFilesResult(BaseModel):
    """Result of snapshot operation (individual files format).

    Returned by the /data/snapshot/s3?format=files endpoint after uploading
    individual files to S3. This format is compatible with grading and diffing.
    """

    snapshot_id: str = Field(
        ..., description="Unique identifier for this snapshot (format: 'snap_<hex>')"
    )
    files_uploaded: int = Field(..., description="Number of files uploaded to S3")
    total_bytes: int = Field(
        ..., description="Total size of all files uploaded in bytes"
    )
