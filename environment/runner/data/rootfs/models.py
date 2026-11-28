"""Pydantic models for rootfs layer capture operations.

This module defines request and response models for the rootfs baseline and
capture endpoints.
"""

from pydantic import BaseModel, Field

from ...utils.s3 import S3Credentials


class RootfsBaselineResult(BaseModel):
    """Result of marking the rootfs capture baseline.

    Returned by the /data/rootfs/baseline endpoint after touching the marker
    file whose ctime later bounds what /data/rootfs/capture includes.
    """

    marker_path: str = Field(..., description="Absolute path of the marker file")
    marked_at: float = Field(
        ..., description="ctime (epoch seconds) of the marker file"
    )


class RootfsCaptureRequest(BaseModel):
    """Request to capture rootfs changes as an OCI layer tar and upload to S3.

    Used by the /data/rootfs/capture endpoint. The capture includes files whose
    status changed (ctime) after the baseline marker was touched, excluding
    virtual filesystems and the snapshot subsystems (which are captured by the
    regular snapshot flow).
    """

    snapshot_id: str = Field(
        ...,
        description=(
            "Snapshot identifier the layer belongs to; the layer is stored "
            "under the same S3 prefix as the snapshot's files."
        ),
    )
    s3_credentials: S3Credentials | None = Field(
        default=None,
        description="Optional credentials to use for the upload.",
    )


class RootfsCaptureResult(BaseModel):
    """Result of a rootfs layer capture.

    Returned by the /data/rootfs/capture endpoint after streaming the layer
    tar.gz to S3.
    """

    s3_uri: str = Field(
        ...,
        description="Full S3 URI of the uploaded layer archive (format: 's3://bucket/key')",
    )
    size_bytes: int = Field(
        ..., description="Compressed size of the layer tar.gz in bytes"
    )
    duration_s: float = Field(
        ..., description="Wall-clock duration of the capture in seconds"
    )
