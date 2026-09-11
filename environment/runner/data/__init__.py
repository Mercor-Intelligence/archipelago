"""Data management endpoints for populating and snapshotting subsystems.

This module provides FastAPI endpoints and handlers for:
- Populating subsystems from direct tar.gz upload or S3-compatible storage
- Creating snapshots of all subsystems (stream to client or upload to S3)

All subsystems must be under 'filesystem' or '.apps_data' to ensure
they are included in snapshots.

Endpoints:
- POST /data/populate - Upload tar.gz archive directly
- POST /data/populate/s3 - Download from S3 sources
- POST /data/snapshot - Stream tar.gz to client
- POST /data/snapshot/s3 - Upload to S3, return pre-signed URL
"""

from .populate import handle_populate, handle_populate_stream
from .populate.models import PopulateRequest, PopulateResult, PopulateStreamResult
from .router import router
from .snapshot import handle_snapshot
from .snapshot.models import SnapshotResult

__all__ = [
    "PopulateRequest",
    "PopulateResult",
    "PopulateStreamResult",
    "SnapshotResult",
    "handle_populate",
    "handle_populate_stream",
    "handle_snapshot",
    "router",
]
