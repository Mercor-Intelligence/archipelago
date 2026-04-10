"""UI-specific FastAPI routes for Looker.

These routes are NOT MCP tools - they are pure FastAPI endpoints for
UI-specific functionality that should not appear in the MCP activity log.

Routes are auto-discovered by mcp_rest_bridge when present.
"""

import asyncio
import io
import os
import uuid
import zipfile
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field

# --- Reload Task Tracking ---
# Track background reload tasks for polling
_reload_tasks: dict[str, dict] = {}  # task_id -> {"status": str, "message": str, ...}

# RLS API configuration
RLS_API_URL = os.environ.get("RLS_API_URL", "https://api.studio.mercor.com")


def _get_rls_headers() -> dict[str, str]:
    """Get headers for RLS API requests."""
    company_id = os.environ.get("RLS_COMPANY_ID", "")
    campaign_id = os.environ.get("RLS_CAMPAIGN_ID", "")

    headers: dict[str, str] = {}
    if company_id:
        headers["X-Company-Id"] = company_id
    if campaign_id:
        headers["X-Campaign-Id"] = campaign_id
    return headers


# Create router with /api/rls prefix
router = APIRouter(prefix="/api/rls", tags=["RLS World Data"])


# --- List Worlds ---


class WorldInfo(BaseModel):
    """Information about a world."""

    world_id: str = Field(..., description="Unique world identifier")
    world_name: str = Field(..., description="World display name")
    domain: str | None = Field(None, description="World domain (e.g., 'Data Science')")
    description: str | None = Field(None, description="World description")


class ListWorldsResponse(BaseModel):
    """Response containing list of worlds."""

    success: bool = Field(..., description="Whether the request was successful")
    message: str = Field(..., description="Human-readable status message")
    worlds: list[WorldInfo] | None = Field(None, description="List of worlds matching the filter")
    total_count: int | None = Field(None, description="Total number of worlds before filtering")
    filtered_count: int | None = Field(None, description="Number of worlds after filtering")


@router.get("/campaigns/{campaign_id}/worlds", response_model=ListWorldsResponse)
async def list_worlds(
    campaign_id: str,
    domain_filter: str | None = None,
) -> ListWorldsResponse:
    """List worlds in a campaign, optionally filtered by domain."""
    try:
        headers = _get_rls_headers()
        headers["X-Campaign-Id"] = campaign_id

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{RLS_API_URL}/worlds/",
                params={"campaign_id": campaign_id},
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

            all_worlds = data.get("worlds", [])
            total_count = len(all_worlds)

            if domain_filter:
                filter_lower = domain_filter.lower().strip()
                filtered = [
                    w for w in all_worlds if (w.get("domain") or "").strip().lower() == filter_lower
                ]
            else:
                filtered = all_worlds

            worlds = [
                WorldInfo(
                    world_id=w.get("world_id", ""),
                    world_name=w.get("world_name", "Unknown"),
                    domain=w.get("domain"),
                    description=w.get("world_description"),
                )
                for w in filtered
            ]

            filter_msg = ""
            if domain_filter:
                filter_msg = f" (filtered by domain '{domain_filter}')"
            return ListWorldsResponse(
                success=True,
                message=f"Found {len(worlds)} world(s){filter_msg}",
                worlds=worlds,
                total_count=total_count,
                filtered_count=len(worlds),
            )
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"API error: {e.response.status_code} - {e.response.text}",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list worlds: {e}")


# --- Preview World Data ---


class PreviewFile(BaseModel):
    """Information about a file available for import."""

    filename: str = Field(..., description="Name of the CSV file")
    size_bytes: int = Field(..., description="File size in bytes")
    path: str = Field(..., description="Full path in the zip")


class PreviewWorldDataResponse(BaseModel):
    """Response containing preview of files available for import."""

    success: bool = Field(..., description="Whether the preview was successful")
    message: str = Field(..., description="Human-readable status message")
    files: list[PreviewFile] | None = Field(
        None, description="List of CSV files available for import"
    )
    total_size_bytes: int | None = Field(None, description="Total size of all files")


@router.get(
    "/campaigns/{campaign_id}/worlds/{world_id}/preview",
    response_model=PreviewWorldDataResponse,
)
async def preview_world_data(campaign_id: str, world_id: str) -> PreviewWorldDataResponse:
    """Preview Looker data files in a world without importing."""
    try:
        headers = _get_rls_headers()
        headers["X-Campaign-Id"] = campaign_id

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"{RLS_API_URL}/worlds/{world_id}/download-zip",
                headers=headers,
            )

            if response.status_code == 404:
                try:
                    detail = response.json().get("detail", {})
                    if isinstance(detail, dict) and detail.get("status") == "processing":
                        return PreviewWorldDataResponse(
                            success=False,
                            message="World zip is being prepared. "
                            "Please try again in a few moments.",
                        )
                except Exception:
                    pass
                raise HTTPException(status_code=404, detail=f"World not found: {world_id}")

            response.raise_for_status()
            data = response.json()

            download_url = data.get("url")
            if not download_url:
                raise HTTPException(status_code=500, detail="No download URL returned from API")

            logger.info("Downloading world zip for preview...")
            zip_response = await client.get(download_url, timeout=300.0)
            zip_response.raise_for_status()
            zip_content = zip_response.content

        with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
            looker_files = [
                name
                for name in zf.namelist()
                if name.startswith(".apps_data/looker/") and name.lower().endswith(".csv")
            ]

            if not looker_files:
                return PreviewWorldDataResponse(
                    success=True,
                    message="No Looker data files found in this world",
                    files=[],
                    total_size_bytes=0,
                )

            files = []
            total_size = 0
            for csv_path in looker_files:
                info = zf.getinfo(csv_path)
                files.append(
                    PreviewFile(
                        filename=Path(csv_path).name,
                        size_bytes=info.file_size,
                        path=csv_path,
                    )
                )
                total_size += info.file_size

            return PreviewWorldDataResponse(
                success=True,
                message=f"Found {len(files)} CSV file(s) available for import",
                files=files,
                total_size_bytes=total_size,
            )

    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"API error: {e.response.status_code} - {e.response.text}",
        )
    except Exception as e:
        logger.exception("Failed to preview world data")
        raise HTTPException(status_code=500, detail=f"Failed to preview world data: {e}")


# --- Import World Data ---


class ImportedFile(BaseModel):
    """Information about an imported file."""

    filename: str = Field(..., description="Name of the imported file")
    view_name: str = Field(..., description="LookML view name created")
    row_count: int = Field(..., description="Number of data rows")
    fields: list[str] = Field(..., description="List of field names")


class ImportWorldDataResponse(BaseModel):
    """Response from world data import operation."""

    success: bool = Field(..., description="Whether the import was successful")
    message: str = Field(..., description="Human-readable status message")
    world_name: str | None = Field(None, description="Name of the imported world")
    imported_files: list[ImportedFile] | None = Field(
        None, description="List of imported CSV files"
    )
    model_name: str | None = Field(None, description="LookML model name (always 'user_data')")
    reload_task_id: str | None = Field(
        None, description="Task ID for polling reload status (if reload is in progress)"
    )


async def _do_reload_in_background(task_id: str, local_api_url: str):
    """Background task to reload the data layer.

    Updates _reload_tasks with status as it progresses.
    """
    global _reload_tasks

    try:
        logger.info(f"[{task_id}] Starting background reload...")
        async with httpx.AsyncClient(timeout=600.0) as client:  # 10 min timeout
            reload_response = await client.post(
                f"{local_api_url}/tools/reload_data",
                json={},
            )

            if reload_response.status_code != 200:
                logger.error(f"[{task_id}] Reload failed: HTTP {reload_response.status_code}")
                _reload_tasks[task_id] = {
                    "status": "failed",
                    "message": f"HTTP {reload_response.status_code}",
                }
                return

            result = reload_response.json()
            if not result.get("success"):
                logger.error(f"[{task_id}] Reload failed: {result.get('message')}")
                _reload_tasks[task_id] = {
                    "status": "failed",
                    "message": result.get("message", "Unknown error"),
                }
                return

            logger.info(
                f"[{task_id}] Reload complete: {result.get('model_count')} models, "
                f"{result.get('explore_count')} explores"
            )
            _reload_tasks[task_id] = {
                "status": "completed",
                "message": "Data layer reloaded successfully",
                "model_count": result.get("model_count"),
                "explore_count": result.get("explore_count"),
            }

    except Exception as e:
        logger.exception(f"[{task_id}] Background reload failed")
        _reload_tasks[task_id] = {
            "status": "failed",
            "message": str(e),
        }


@router.post(
    "/campaigns/{campaign_id}/worlds/{world_id}/import",
    response_model=ImportWorldDataResponse,
)
async def import_world_data(
    campaign_id: str, world_id: str, request: Request
) -> ImportWorldDataResponse:
    """Import Looker data from a world.

    Downloads the world zip file, extracts Looker-specific CSVs from
    .apps_data/looker/, and imports them into the Looker MCP server.

    Clears any previously imported data before importing new data.

    Strategy:
    1. Clear previous user data (CSVs + DuckDB tables)
    2. Write all CSVs to STATE_LOCATION
    3. Load all CSVs into DuckDB (DuckDB is the single source of truth)
    4. Fire-and-forget reload_data (returns task_id for polling)

    The reload happens in the background. Use GET /api/rls/reload-status/{task_id}
    to poll for completion.
    """
    from data_layer import clear_user_data, get_runtime_duckdb_path, get_user_csv_dir
    from scripts.build_duckdb import load_csv_to_table

    global _reload_tasks

    try:
        # Clear previous user data before importing new world
        logger.info("Clearing previous user data before import...")
        clear_user_data()

        headers = _get_rls_headers()
        headers["X-Campaign-Id"] = campaign_id

        # Get the local API URL from the incoming request's server info
        server_host, server_port = request.scope["server"]
        local_api_url = f"http://{server_host}:{server_port}"

        # Get the shared storage location for CSV files
        user_csv_dir = get_user_csv_dir()
        if not user_csv_dir:
            raise HTTPException(
                status_code=500,
                detail="STATE_LOCATION not configured - cannot import world data",
            )

        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.get(
                f"{RLS_API_URL}/worlds/{world_id}/download-zip",
                headers=headers,
            )

            if response.status_code == 404:
                try:
                    detail = response.json().get("detail", {})
                    if isinstance(detail, dict) and detail.get("status") == "processing":
                        return ImportWorldDataResponse(
                            success=False,
                            message="World zip is being prepared. "
                            "Please try again in a few moments.",
                        )
                except Exception:
                    pass
                raise HTTPException(status_code=404, detail=f"World not found: {world_id}")

            response.raise_for_status()
            data = response.json()

            download_url = data.get("url")
            if not download_url:
                raise HTTPException(status_code=500, detail="No download URL returned from API")

            logger.info("Downloading world zip from S3...")
            zip_response = await client.get(download_url, timeout=300.0)
            zip_response.raise_for_status()
            zip_content = zip_response.content

            imported_files = []
            world_name = None

            with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
                looker_files = [
                    name
                    for name in zf.namelist()
                    if name.startswith(".apps_data/looker/") and name.lower().endswith(".csv")
                ]

                if not looker_files:
                    return ImportWorldDataResponse(
                        success=False,
                        message="No Looker data found in world (.apps_data/looker/*.csv)",
                    )

                logger.info(f"Found {len(looker_files)} Looker CSV(s) in world")

                # Step 1: Write all CSVs to STATE_LOCATION
                csv_paths_to_load = []
                for zip_csv_path in looker_files:
                    csv_content = zf.read(zip_csv_path).decode("utf-8")
                    filename = Path(zip_csv_path).name

                    # Write CSV directly to shared STATE_LOCATION
                    local_csv_path = user_csv_dir / filename
                    local_csv_path.write_text(csv_content)
                    logger.info(f"Wrote {filename} to {local_csv_path}")
                    csv_paths_to_load.append(local_csv_path)

                    # Count rows for response
                    lines = csv_content.strip().split("\n")
                    row_count = len(lines) - 1 if len(lines) > 1 else 0

                    # Parse header for field names
                    import csv as csv_module

                    reader = csv_module.reader(io.StringIO(lines[0] if lines else ""))
                    fields = next(reader, [])

                    view_name = local_csv_path.stem
                    imported_files.append(
                        ImportedFile(
                            filename=filename,
                            view_name=view_name,
                            row_count=row_count,
                            fields=fields,
                        )
                    )

                # Step 2: Load all CSVs into DuckDB (DuckDB is the single source of truth)
                import duckdb

                db_path = get_runtime_duckdb_path()
                conn = duckdb.connect(str(db_path))
                try:
                    for csv_path in csv_paths_to_load:
                        load_csv_to_table(conn, csv_path)
                        logger.info(f"Loaded {csv_path.name} into DuckDB")
                finally:
                    conn.close()
                logger.info(f"Loaded {len(csv_paths_to_load)} CSV(s) into DuckDB")

        if not imported_files:
            return ImportWorldDataResponse(
                success=False,
                message="Failed to import any CSV files from the world",
            )

        # Step 3: Fire-and-forget reload in background (builds in-memory state from DuckDB)
        task_id = str(uuid.uuid4())
        _reload_tasks[task_id] = {"status": "pending", "message": "Reload starting..."}

        logger.info(f"Starting background reload with task_id={task_id}")
        asyncio.create_task(_do_reload_in_background(task_id, local_api_url))

        return ImportWorldDataResponse(
            success=True,
            message=f"Imported {len(imported_files)} file(s). Data reload in progress.",
            world_name=world_name,
            imported_files=imported_files,
            model_name="user_data",
            reload_task_id=task_id,
        )

    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"API error: {e.response.status_code} - {e.response.text}",
        )
    except Exception as e:
        logger.exception("Failed to import world data")
        raise HTTPException(status_code=500, detail=f"Failed to import world data: {e}")


# --- Reload Status Polling ---


class ReloadStatusResponse(BaseModel):
    """Response for reload status polling."""

    status: str = Field(..., description="Status: pending, completed, or failed")
    message: str = Field(..., description="Human-readable status message")
    model_count: int | None = Field(None, description="Number of models loaded (if completed)")
    explore_count: int | None = Field(None, description="Number of explores loaded (if completed)")


@router.get("/reload-status/{task_id}", response_model=ReloadStatusResponse)
async def get_reload_status(task_id: str) -> ReloadStatusResponse:
    """Poll the status of a background reload task.

    Returns the current status of the reload operation started by import_world_data.
    Poll this endpoint until status is 'completed' or 'failed'.
    """
    global _reload_tasks

    task = _reload_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

    return ReloadStatusResponse(
        status=task.get("status", "unknown"),
        message=task.get("message", ""),
        model_count=task.get("model_count"),
        explore_count=task.get("explore_count"),
    )


def get_router() -> APIRouter:
    """Return the router for registration with FastAPI app.

    This function is called by mcp_rest_bridge to auto-discover UI routes.
    """
    return router
