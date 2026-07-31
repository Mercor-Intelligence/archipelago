"""
Generic Database Management Tools for MCP Servers.

Automatically provides CSV import/export and state clearing for any MCP server with a database.
These tools are dynamically added by the REST bridge when a database is detected.
"""

import asyncio
import base64
import io
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

# ============================================================================
# Pydantic Models (for UI generation)
# ============================================================================


class CSVImportRequest(BaseModel):
    """Import CSV data into a database table.

    Note: Table must already exist (created via Alembic migrations).
    CSV import only appends data to existing tables.
    """

    table_name: str = Field(..., description="Name of the table to import into")
    csv_content: str = Field(..., description="CSV content (plain text or base64-encoded)")


class CSVImportResponse(BaseModel):
    """Result of CSV import operation."""

    table_name: str = Field(..., description="Table that was imported to")
    rows_imported: int = Field(..., description="Number of rows imported")
    message: str = Field(..., description="Success message")


class CSVExportRequest(BaseModel):
    """Export a database table to CSV."""

    table_name: str = Field(..., description="Name of the table to export")
    include_headers: bool = Field(default=True, description="Include column headers")


class CSVExportResponse(BaseModel):
    """Result of CSV export operation."""

    table_name: str = Field(..., description="Table that was exported")
    row_count: int = Field(..., description="Number of rows exported")
    csv_content: str = Field(..., description="CSV content as string")
    message: str = Field(..., description="Success message")


class ListTablesResponse(BaseModel):
    """List all tables in the database."""

    tables: list[str] = Field(..., description="List of table names")
    count: int = Field(..., description="Number of tables")


class ClearDatabaseRequest(BaseModel):
    """Clear database state (delete all data or drop tables)."""

    mode: str = Field(
        default="truncate",
        description="Clear mode: 'truncate' (delete data, keep tables) or 'drop' (drop all tables)",
    )
    confirm: bool = Field(
        default=False,
        description="Confirmation flag - must be true to proceed",
    )


class ClearDatabaseResponse(BaseModel):
    """Result of database clear operation."""

    mode: str = Field(..., description="Clear mode used")
    tables_affected: list[str] = Field(..., description="Tables that were affected")
    message: str = Field(..., description="Success message")


# ============================================================================
# Helper Functions
# ============================================================================


def detect_csv_encoding(content: str) -> str:
    """Detect if CSV content is base64-encoded."""
    try:
        decoded = base64.b64decode(content)
        decoded.decode("utf-8")
        return "base64"
    except Exception:
        return "plain"


# ============================================================================
# Database Management Tool Implementations
# ============================================================================


async def import_csv_to_db(request: CSVImportRequest, engine: AsyncEngine) -> CSVImportResponse:
    """Import CSV content into database table using pandas."""
    # Validate table name (basic check)
    table_name = request.table_name.strip()
    if not table_name or not table_name.replace("_", "").isalnum():
        raise ValueError(
            f"Invalid table name '{table_name}': use only letters, numbers, and underscores"
        )

    # Decode CSV if base64
    csv_text = request.csv_content
    encoding = detect_csv_encoding(csv_text)
    if encoding == "base64":
        csv_text = base64.b64decode(csv_text).decode("utf-8")

    # Parse CSV with pandas (handles all edge cases automatically)
    try:
        df = pd.read_csv(
            io.StringIO(csv_text),
            skipinitialspace=True,  # Remove leading whitespace
            encoding="utf-8",
        )
    except Exception as e:
        raise ValueError(f"Failed to parse CSV: {e}")

    if df.empty:
        raise ValueError("CSV must have at least one data row")

    # Sanitize column names
    df.columns = [col.lower().replace(" ", "_").replace("-", "_") for col in df.columns]

    # Check if table exists
    async with engine.connect() as conn:
        inspector = await conn.run_sync(lambda c: inspect(c))
        table_exists = await conn.run_sync(lambda c: inspector.has_table(table_name))

    if not table_exists:
        raise ValueError(
            f"Table '{table_name}' does not exist. "
            "Please ensure the database schema is created via Alembic migrations first."
        )

    # Use pandas to_sql to insert data into existing table
    # This runs in a sync thread since pandas doesn't support async
    def sync_import():
        from sqlalchemy import create_engine

        # Create sync engine from async engine URL
        sync_engine = create_engine(
            str(engine.url).replace("+aiosqlite", ""),
            echo=False,
        )

        # Always append - never create or replace tables
        df.to_sql(
            table_name,
            sync_engine,
            if_exists="append",
            index=False,  # Don't write DataFrame index
        )
        sync_engine.dispose()
        return len(df)

    # Run sync operation in thread pool
    inserted = await asyncio.to_thread(sync_import)

    return CSVImportResponse(
        table_name=table_name,
        rows_imported=inserted,
        message=f"Successfully imported {inserted} rows into '{table_name}'",
    )


async def export_db_to_csv(request: CSVExportRequest, engine: AsyncEngine) -> CSVExportResponse:
    """Export database table to CSV using pandas."""
    # Validate table name (basic check)
    table_name = request.table_name.strip()
    if not table_name or not table_name.replace("_", "").isalnum():
        raise ValueError(
            f"Invalid table name '{table_name}': use only letters, numbers, and underscores"
        )

    # Check if table exists
    async with engine.connect() as conn:
        inspector = await conn.run_sync(lambda c: inspect(c))
        if not await conn.run_sync(lambda c: inspector.has_table(table_name)):
            raise ValueError(f"Table '{table_name}' does not exist")

    # Use pandas to read from SQL and convert to CSV
    def sync_export():
        from sqlalchemy import create_engine

        # Create sync engine from async engine URL
        sync_engine = create_engine(
            str(engine.url).replace("+aiosqlite", ""),
            echo=False,
        )

        # Read table into DataFrame
        df = pd.read_sql_table(table_name, sync_engine)
        sync_engine.dispose()

        # Convert to CSV
        csv_content = df.to_csv(index=False, header=request.include_headers)
        return csv_content, len(df)

    # Run sync operation in thread pool
    csv_content, row_count = await asyncio.to_thread(sync_export)

    return CSVExportResponse(
        table_name=table_name,
        row_count=row_count,
        csv_content=csv_content,
        message=f"Successfully exported {row_count} rows from '{table_name}'",
    )


async def list_tables(engine: AsyncEngine) -> ListTablesResponse:
    """List all tables in the database."""
    async with engine.connect() as conn:
        inspector = await conn.run_sync(lambda c: inspect(c))
        tables = await conn.run_sync(lambda c: inspector.get_table_names())

    return ListTablesResponse(tables=sorted(tables), count=len(tables))


async def clear_database(
    request: ClearDatabaseRequest, engine: AsyncEngine
) -> ClearDatabaseResponse:
    """Clear database (truncate or drop tables)."""
    if not request.confirm:
        raise ValueError(
            "Confirmation required: set 'confirm' to true to proceed with database clearing"
        )

    # Get list of tables
    async with engine.connect() as conn:
        inspector = await conn.run_sync(lambda c: inspect(c))
        tables = await conn.run_sync(lambda c: inspector.get_table_names())

    if not tables:
        return ClearDatabaseResponse(
            mode=request.mode, tables_affected=[], message="Database is already empty"
        )

    affected = []
    async with engine.begin() as conn:
        if request.mode == "truncate":
            # Delete all rows but keep tables
            for table_name in tables:
                await conn.execute(text(f"DELETE FROM {table_name}"))
                affected.append(table_name)
            message = f"Deleted all data from {len(affected)} tables"

        elif request.mode == "drop":
            # Drop all tables
            for table_name in tables:
                await conn.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
                affected.append(table_name)
            message = f"Dropped {len(affected)} tables"

        else:
            raise ValueError(f"Invalid mode: {request.mode}. Use 'truncate' or 'drop'")

    return ClearDatabaseResponse(mode=request.mode, tables_affected=affected, message=message)


# ============================================================================
# Tool Registration (called by REST bridge)
# ============================================================================


def get_db_management_tools(engine: AsyncEngine) -> dict[str, dict[str, Any]]:
    """
    Get database management tools for registration with REST bridge.

    Args:
        engine: SQLAlchemy async engine for the database

    Returns:
        Dictionary of tool name -> tool metadata
    """

    async def import_csv_wrapper(request: CSVImportRequest) -> CSVImportResponse:
        return await import_csv_to_db(request, engine)

    async def export_csv_wrapper(request: CSVExportRequest) -> CSVExportResponse:
        return await export_db_to_csv(request, engine)

    async def list_tables_wrapper() -> ListTablesResponse:
        """List all database tables."""
        return await list_tables(engine)

    async def clear_db_wrapper(request: ClearDatabaseRequest) -> ClearDatabaseResponse:
        return await clear_database(request, engine)

    # Return tool definitions
    return {
        "import_csv": {
            "function": import_csv_wrapper,
            "input_model": CSVImportRequest,
            "output_model": CSVImportResponse,
            "description": "Import CSV data into a database table",
        },
        "export_csv": {
            "function": export_csv_wrapper,
            "input_model": CSVExportRequest,
            "output_model": CSVExportResponse,
            "description": "Export a database table to CSV format",
        },
        "list_tables": {
            "function": list_tables_wrapper,
            "input_model": type(None),  # No input
            "output_model": ListTablesResponse,
            "description": "List all tables in the database",
        },
        "clear_database": {
            "function": clear_db_wrapper,
            "input_model": ClearDatabaseRequest,
            "output_model": ClearDatabaseResponse,
            "description": "Clear database state (delete data or drop tables)",
        },
    }
