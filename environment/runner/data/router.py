"""FastAPI router for data management endpoints.

This module defines the FastAPI router that handles all /data/* endpoints:
- /data/populate - Direct tar.gz upload to populate subsystems
- /data/populate/s3 - Populate from S3 sources
- /data/snapshot - Stream tar.gz snapshot to client
- /data/snapshot/s3 - Upload snapshot to S3

The router is mounted at the /data prefix in the main FastAPI application.
"""

import json

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import ValidationError

from .populate import handle_populate, handle_populate_stream
from .populate.main import run_lifecycle_hook
from .populate.models import (
    LifecycleHook,
    PopulateRequest,
    PopulateResult,
    PopulateStreamResult,
)
from .snapshot import handle_snapshot, handle_snapshot_s3, handle_snapshot_s3_files
from .snapshot.models import (
    SnapshotFilesResult,
    SnapshotRequest,
    SnapshotResult,
    SnapshotStreamRequest,
)

router = APIRouter()


@router.post("/populate", response_model=PopulateStreamResult)
async def populate(
    archive: UploadFile = File(..., description="tar.gz archive to extract"),
    subsystem: str = Query(
        default="filesystem",
        description="Target subsystem: 'filesystem', '.apps_data', or nested path",
    ),
    post_populate_hooks: str | None = Form(
        default=None,
        description="JSON array of lifecycle hooks to run after extraction. Each hook: {name, command, env?}",
    ),
) -> PopulateStreamResult:
    """
    Upload a tar.gz archive to populate a subsystem.

    The archive is streamed to disk and extracted incrementally (constant memory).
    Can be called multiple times — files with same paths are overwritten.

    Args:
        archive: tar.gz file to extract
        subsystem: Target subsystem ("filesystem", ".apps_data", or nested path)
        post_populate_hooks: Optional JSON array of hooks to run after extraction

    Returns:
        PopulateStreamResult with objects_added, subsystem, and extracted_bytes
    """
    logger.debug(f"Direct populate request: subsystem={subsystem}")

    # Parse hooks from JSON string if provided
    hooks: list[LifecycleHook] = []
    if post_populate_hooks:
        try:
            hooks_data = json.loads(post_populate_hooks)
            hooks = [LifecycleHook(**h) for h in hooks_data]
            logger.debug(f"Parsed {len(hooks)} post-populate hook(s)")
        except (json.JSONDecodeError, TypeError, ValidationError) as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid post_populate_hooks JSON: {e}",
            ) from e

    async def stream_chunks():
        while chunk := await archive.read(65536):
            yield chunk

    try:
        result = await handle_populate_stream(stream_chunks(), subsystem)
        logger.info(
            f"Populated {result.objects_added} objects ({result.extracted_bytes / 1e6:.1f} MB) to {subsystem}"
        )

        # Run post-populate hooks
        if hooks:
            logger.info(f"Running {len(hooks)} post-populate hook(s)")
            for hook in hooks:
                await run_lifecycle_hook(hook)
            logger.info("All post-populate hooks completed")

        return result
    except ValueError as e:
        logger.error(f"Invalid populate request: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        # Hook failure
        logger.error(f"Post-populate hook failed: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Populate failed: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/populate/s3", response_model=PopulateResult)
async def populate_s3(request: PopulateRequest) -> PopulateResult:
    """
    Populate subsystems with data from S3-compatible storage.

    This endpoint can be called multiple times during the environment's lifetime.
    Each call adds new objects and overwrites existing ones with the same destination path.

    Overwrite semantics:
    - Within a single call: Later sources in the list overwrite earlier ones if they
      have the same destination path.
    - Between calls: New calls overwrite existing objects if they have the same
      destination path. Objects that don't conflict are preserved.

    Args:
        request: PopulateRequest with sources (each has url and subsystem)

    Returns:
        PopulateResult with objects_added count
    """
    logger.debug(f"S3 populate request: {len(request.sources)} source(s)")
    for i, source in enumerate(request.sources):
        logger.debug(
            f"  Source {i + 1}: {source.url} -> subsystem '{source.subsystem}'"
        )

    try:
        result = await handle_populate(request)
        logger.info(f"Populated {result.objects_added} objects from S3")
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error populating data from S3: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


# ============ SNAPSHOT ENDPOINTS ============


@router.post("/snapshot")
async def snapshot(request: SnapshotStreamRequest | None = None):
    """
    Create a snapshot of all subsystems and stream it back as a tar.gz file.

    This endpoint can be called multiple times during the environment's lifetime.
    Each call creates a new snapshot with a unique ID in the filename.

    Optionally accepts a request body with pre_snapshot_hooks to run before
    creating the archive (e.g., database dumps).

    Args:
        request: Optional request body with pre_snapshot_hooks

    Returns:
        StreamingResponse with the tar.gz archive file
    """
    hooks_count = len(request.pre_snapshot_hooks) if request else 0
    logger.debug(f"Snapshot request received (hooks={hooks_count})")
    try:
        hooks = request.pre_snapshot_hooks if request else None
        stream, filename = await handle_snapshot(pre_snapshot_hooks=hooks)
        logger.debug(f"Snapshot stream created: {filename}")
        return StreamingResponse(
            stream,
            media_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating snapshot: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/snapshot/s3")
async def snapshot_s3(
    request: SnapshotRequest,
) -> SnapshotResult | SnapshotFilesResult:
    """
    Create a snapshot of all subsystems and upload to S3.

    This endpoint can be called multiple times during the environment's lifetime.
    Each call creates a new snapshot with a unique ID.

    Snapshots are stored in the S3_SNAPSHOTS_BUCKET bucket with the prefix S3_SNAPSHOTS_PREFIX.

    Args:
        request: SnapshotRequest with format and optional pre_snapshot_hooks

    Returns:
        SnapshotResult (for tar.gz) or SnapshotFilesResult (for files)
    """
    logger.debug(
        f"Snapshot S3 request received (format={request.format}, hooks={len(request.pre_snapshot_hooks)})"
    )
    try:
        hooks = request.pre_snapshot_hooks or None
        if request.format == "files":
            result = await handle_snapshot_s3_files(pre_snapshot_hooks=hooks)
            logger.debug(
                f"Snapshot S3 files completed: {result.snapshot_id} ({result.files_uploaded} files, {result.total_bytes} bytes)"
            )
            return result
        else:
            result = await handle_snapshot_s3(pre_snapshot_hooks=hooks)
            logger.debug(
                f"Snapshot S3 completed: {result.snapshot_id} ({result.size_bytes} bytes) -> {result.s3_uri}"
            )
            return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating snapshot S3: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
