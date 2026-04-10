"""
REST bridge hooks for the Tableau MCP server.

These endpoints provide debugging/inspection capabilities specific to the Tableau server.
They are automatically loaded by the REST bridge when starting the server.
"""

import logging
import os
import sys
from datetime import datetime
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException
from loguru import logger as _loguru_logger
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Suppress DEBUG/INFO logging in the REST bridge process.
#
# The shared REST bridge logs every tool-call result at DEBUG level
# (including 100 KB+ base64 chart images).  Over time this fills the
# container log driver buffer, stalls I/O, and eventually crashes the
# server with a 500 error.
#
# Because this file is imported *inside* the REST bridge process (via
# load_server_hooks), reconfiguring loguru here takes effect globally
# for the bridge — without touching the shared code.
# ---------------------------------------------------------------------------
_BRIDGE_LOG_LEVEL = os.environ.get("LOG_LEVEL", "WARNING").upper()
_loguru_logger.remove()  # drop the default DEBUG sink
_loguru_logger.add(sys.stderr, level=_BRIDGE_LOG_LEVEL)
logging.basicConfig(
    level=getattr(logging, _BRIDGE_LOG_LEVEL, logging.WARNING),
    stream=sys.stderr,
    force=True,
)

# =============================================================================
# Response Models
# =============================================================================


class FileInfo(BaseModel):
    """Information about a file in the sandbox."""

    path: str = Field(..., description="File path")
    size: int = Field(..., description="File size in bytes")
    modified_at: str | None = Field(None, description="Last modified timestamp (ISO format)")


class ListFilesResponse(BaseModel):
    """List of files in the sandbox."""

    files: list[FileInfo] = Field(..., description="List of files")
    total_files: int = Field(..., description="Total number of files")
    total_size: int = Field(..., description="Total size of all files in bytes")


class TableData(BaseModel):
    """Data from a single database table."""

    table_name: str = Field(..., description="Name of the table")
    columns: list[str] = Field(..., description="Column names")
    rows: list[list[Any]] = Field(..., description="Row data as list of lists")
    row_count: int = Field(..., description="Number of rows")


class PreviewStateResponse(BaseModel):
    """Full preview of database state including all table data."""

    tables: list[TableData] = Field(..., description="Data from all tables")
    total_tables: int = Field(..., description="Total number of tables")
    total_rows: int = Field(..., description="Total number of rows across all tables")


# =============================================================================
# Helper Functions
# =============================================================================


def _get_sync_url(engine) -> str:
    """Convert async engine URL to sync URL."""
    return (
        str(engine.url).replace("+aiosqlite", "").replace("+asyncpg", "").replace("+aiomysql", "")
    )


# =============================================================================
# REST Bridge Hook Registration
# =============================================================================


def register_endpoints(app: FastAPI, module_path: str, engine=None):
    """Register Tableau-specific REST endpoints.

    Args:
        app: The FastAPI application instance
        module_path: The MCP server module path
        engine: Deprecated - engine is now fetched from app.state.engine at request time
    """

    @app.get("/tools/list_files", response_model=ListFilesResponse)
    async def list_files():
        """List files in the sandbox (database file info)."""
        # Get current engine from app.state at request time to handle /clear replacement
        current_engine = app.state.engine
        if not current_engine:
            raise HTTPException(status_code=503, detail="Database not initialized")

        files = []
        total_size = 0

        db_url = str(current_engine.url)

        # Handle SQLite database paths
        if "sqlite" in db_url:
            # Extract path from sqlite:///path or sqlite+aiosqlite:///path
            if ":///" in db_url:
                db_path = db_url.split("///")[-1]
            else:
                db_path = db_url.split("://")[-1]

            # Remove query parameters if any
            if "?" in db_path:
                db_path = db_path.split("?")[0]

            if db_path and os.path.exists(db_path):
                stat = os.stat(db_path)
                modified_at = datetime.fromtimestamp(stat.st_mtime).isoformat()
                files.append(
                    FileInfo(
                        path=os.path.basename(db_path),
                        size=stat.st_size,
                        modified_at=modified_at,
                    )
                )
                total_size = stat.st_size

        return ListFilesResponse(
            files=files,
            total_files=len(files),
            total_size=total_size,
        )

    @app.get("/tools/preview_state", response_model=PreviewStateResponse)
    async def preview_state():
        """Preview full database state including all table data."""
        # Get current engine from app.state at request time to handle /clear replacement
        current_engine = app.state.engine
        if not current_engine:
            raise HTTPException(status_code=503, detail="Database not initialized")

        from sqlalchemy import create_engine, inspect

        sync_url = _get_sync_url(current_engine)
        sync_engine = create_engine(sync_url, echo=False)

        try:
            inspector = inspect(sync_engine)
            table_names = inspector.get_table_names()
        finally:
            sync_engine.dispose()

        tables_data = []
        total_rows = 0

        sync_engine = create_engine(sync_url, echo=False)
        try:
            for table_name in sorted(table_names):
                try:
                    df = pd.read_sql_table(table_name, sync_engine)
                    columns = list(df.columns)
                    rows = df.astype(str).values.tolist()
                    row_count = len(df)
                    total_rows += row_count

                    tables_data.append(
                        TableData(
                            table_name=table_name,
                            columns=columns,
                            rows=rows,
                            row_count=row_count,
                        )
                    )
                except Exception as e:
                    # Use row_count=0 for errors - the error row is metadata, not data
                    # This keeps total_rows accurate as sum of actual data rows
                    tables_data.append(
                        TableData(
                            table_name=table_name,
                            columns=["error"],
                            rows=[[str(e)]],
                            row_count=0,
                        )
                    )
        finally:
            sync_engine.dispose()

        return PreviewStateResponse(
            tables=tables_data,
            total_tables=len(tables_data),
            total_rows=total_rows,
        )
