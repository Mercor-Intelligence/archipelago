"""Pydantic models for snapshot operations.

This module defines request and response models for the snapshot endpoint.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ...utils.s3 import S3Credentials
from ..populate.models import HookTiming, LifecycleHook


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
    exclude_globs: list[str] = Field(
        default_factory=list,
        description=(
            "fnmatch patterns tested against each file's archive name "
            "(e.g. '.apps_data/svc/studio.fts.db'); matching files are "
            "excluded from the snapshot. fnmatch's '*' crosses '/'."
        ),
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
    snapshot_id: str | None = Field(
        default=None,
        description="Optional unique identifier for this snapshot, preallocated by caller.",
    )
    s3_credentials: S3Credentials | None = Field(
        default=None,
        description="Optional credentials to use for the snapshot operation.",
    )
    credentials_expire_at: datetime | None = Field(
        default=None,
        description=(
            "When `s3_credentials` lapse. Supplied only by callers that also "
            "push refreshes to /snapshot/s3/credentials/{job_id}; without it "
            "the credentials are frozen into the session as before."
        ),
    )
    snapshot_zip_enabled: bool = Field(
        default=True,
        description=(
            "When True, also build a prebuilt single-ZIP copy of the snapshot "
            "for one-GET grading downloads. Gated per-world via the world's "
            "`snapshot_zip_enabled` setting. Defaults True to preserve the prior "
            "always-on behavior when an older caller omits the field."
        ),
    )
    exclude_globs: list[str] = Field(
        default_factory=list,
        description=(
            "fnmatch patterns tested against each file's archive name "
            "(e.g. '.apps_data/svc/studio.fts.db'); matching files are "
            "excluded from the snapshot. fnmatch's '*' crosses '/'."
        ),
    )
    mounted_subsystems: list[str] = Field(
        default_factory=list,
        description=(
            "Subsystem roots ('filesystem', '.apps_data') that are LIVE Modal "
            "image mounts at capture time. Their files are read over 9p rather "
            "than local disk, so the per-file upload budget is scaled for them. "
            "Only the agent knows this — mounting is a control-plane call it "
            "makes, invisible from inside the sandbox. Empty means everything "
            "is local disk, which is both the unmounted case and what a caller "
            "older than this field sends."
        ),
    )
    job_deadline_seconds: float | None = Field(
        default=None,
        description=(
            "How long the caller will poll this job before abandoning it. Used "
            "only to bound the widened `mounted_subsystems` budget, so a file's "
            "retry sequence cannot outlive the deadline that will cut it off. "
            "Travels with `mounted_subsystems` and is meaningless without it."
        ),
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
    hook_timings: list[HookTiming] = Field(
        default_factory=list,
        description="Per-hook execution timing for pre-snapshot hooks",
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
    hook_timings: list[HookTiming] = Field(
        default_factory=list,
        description="Per-hook execution timing for pre-snapshot hooks",
    )


class SnapshotJobStarted(BaseModel):
    """Response for ``POST /data/snapshot/s3/start``.

    Carries the id the caller polls via ``/data/snapshot/s3/status/{job_id}``.
    """

    job_id: str = Field(
        ...,
        description="Opaque id to poll for this async snapshot's status",
    )


class SnapshotCredentialsRefresh(BaseModel):
    """Body for ``POST /data/snapshot/s3/credentials/{job_id}``.

    The scoped STS lease the sandbox uploads with is capped at 3600s, but a
    multi-GiB harvest runs longer than that. The caller — which is already
    polling this job to completion — re-mints against the same write prefix and
    pushes the result here, and the in-flight upload signs its remaining parts
    with it. See ``runner.utils.s3.RefreshableS3Credentials``.
    """

    s3_credentials: S3Credentials = Field(
        ..., description="Freshly minted credentials for this job's write prefix."
    )
    expires_at: datetime = Field(
        ..., description="When the supplied credentials lapse."
    )


class SnapshotJobStatus(BaseModel):
    """Response for ``GET /data/snapshot/s3/status/{job_id}``."""

    status: Literal["running", "done", "error"] = Field(
        ..., description="Current state of the background snapshot job"
    )
    result: SnapshotResult | SnapshotFilesResult | None = Field(
        default=None, description="Snapshot result, set once status == 'done'"
    )
    error: str | None = Field(
        default=None, description="Failure detail, set once status == 'error'"
    )
