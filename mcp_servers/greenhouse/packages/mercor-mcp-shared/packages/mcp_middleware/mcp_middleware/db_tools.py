"""
Generic Database Management Tools for MCP Servers.

Automatically provides CSV import/export and state clearing for any MCP server with a database.
These tools are dynamically added by the REST bridge when a database is detected.
"""

import base64
import io
import json
from collections.abc import Callable
from pathlib import Path
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

    Note: Table must already exist (created via init_db on startup).
    CSV import only appends data to existing tables.
    """

    table_name: str = Field(
        ...,
        description="Name of the table to import into",
        json_schema_extra={
            "x-populate-from": "list_tables",
            "x-populate-field": "tables",
            # For object arrays, use x-populate-value and x-populate-display
            # For string arrays (like list_tables), these are not needed
        },
    )
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


class ListTablesRequest(BaseModel):
    """Request to list all tables in the database."""

    pass  # No parameters needed


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


# Cache for loaded YAML configs (server_name -> config)
_nested_import_cache: dict[str, dict] = {}


def _load_nested_import_config(server_name: str) -> dict[str, dict]:
    """Load nested import config from per-server csv_import_config.yaml.

    Each MCP server can define nested column mappings so that CSV imports
    automatically split JSON array columns into child tables.

    Args:
        server_name: Name of the MCP server (e.g., "greenhouse").
                     Empty string disables nested import lookup.

    Returns:
        Dict mapping parent table names to their nested column configs,
        or empty dict if no config found.
    """
    if not server_name:
        return {}
    if server_name in _nested_import_cache:
        return _nested_import_cache[server_name]

    possible_paths = [
        Path("csv_import_config.yaml"),  # CWD (deployment)
        Path(f"mcp_servers/{server_name}/csv_import_config.yaml"),  # Project root
    ]

    for path in possible_paths:
        if path.exists():
            try:
                import yaml

                with open(path) as f:
                    data = yaml.safe_load(f) or {}
                nested_imports = data.get("nested_imports", {})
                _nested_import_cache[server_name] = nested_imports
                return nested_imports
            except Exception:
                break

    _nested_import_cache[server_name] = {}
    return {}


# ============================================================================
# Database Management Tool Implementations
# ============================================================================


async def import_csv_to_db(
    request: CSVImportRequest, engine: AsyncEngine, server_name: str = ""
) -> CSVImportResponse:
    """Import CSV content into database table using pandas.

    If server_name is provided and the server has a csv_import_config.yaml,
    JSON array columns are automatically parsed and inserted into child tables.
    """
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
            "Please ensure the database schema is created via init_db() on startup."
        )

    # Check for nested JSON columns that need special handling
    all_nested_config = _load_nested_import_config(server_name)
    nested_config = all_nested_config.get(table_name, {})
    nested_columns = [col for col in df.columns if col in nested_config]

    # Extract nested data before removing columns from main dataframe
    nested_data: dict[str, list[dict]] = {}
    if nested_columns:
        if "id" not in df.columns:
            cols_str = ", ".join(nested_columns)
            raise ValueError(
                f"CSV contains nested JSON columns ({cols_str}) but no 'id' column. "
                "The 'id' column is required to link child records to parent records. "
                "Either add an 'id' column or remove the nested JSON columns."
            )
        for col in nested_columns:
            nested_data[col] = []
            config = nested_config[col]
            for _, row in df.iterrows():
                parent_id = row["id"]
                json_value = row[col]
                if pd.notna(json_value) and json_value:
                    try:
                        if isinstance(json_value, str):
                            parsed = json.loads(json_value)
                        else:
                            parsed = json_value
                        if isinstance(parsed, list):
                            for item in parsed:
                                if config.get("is_tag_list") and isinstance(item, str):
                                    nested_data[col].append(
                                        {"parent_id": parent_id, "tag_name": item}
                                    )
                                elif isinstance(item, dict):
                                    item_copy = dict(item)
                                    item_copy["parent_id"] = parent_id
                                    nested_data[col].append(item_copy)
                    except (json.JSONDecodeError, TypeError):
                        pass  # Skip invalid JSON

    # Remove nested columns from main dataframe
    df_main = df.drop(columns=nested_columns, errors="ignore") if nested_columns else df

    # Check database dialect for SQL syntax differences
    is_sqlite = "sqlite" in str(engine.url)

    # Use run_sync to execute on the same connection (important for in-memory SQLite)
    async with engine.connect() as conn:

        def sync_import(sync_conn):
            # Always append - never create or replace tables
            df_main.to_sql(
                table_name,
                sync_conn,
                if_exists="append",
                index=False,  # Don't write DataFrame index
            )

            # Insert nested child table data
            child_rows = 0
            for col, items in nested_data.items():
                if not items:
                    continue
                config = nested_config[col]
                # Validate required config keys
                for required_key in ("child_table", "fk_column"):
                    if required_key not in config:
                        raise ValueError(
                            f"Missing required key '{required_key}' in nested config "
                            f"for column '{col}'. Expected keys: child_table, fk_column. "
                            f"Got: {list(config.keys())}"
                        )
                # value_columns is required when is_tag_list is not set
                if not config.get("is_tag_list") and "value_columns" not in config:
                    raise ValueError(
                        f"Missing required key 'value_columns' in nested config "
                        f"for column '{col}'. Either set 'is_tag_list: true' or provide "
                        f"'value_columns'. Got: {list(config.keys())}"
                    )
                child_table = config["child_table"]
                fk_column = config["fk_column"]

                for item in items:
                    parent_id = item.get("parent_id")
                    if pd.isna(parent_id):
                        continue  # Skip rows with missing parent ID
                    if config.get("is_tag_list"):
                        tag_name = item.get("tag_name")
                        if tag_name:
                            # Use dialect-appropriate INSERT OR IGNORE syntax
                            if is_sqlite:
                                insert_tag_sql = "INSERT OR IGNORE INTO tags (name) VALUES (:name)"
                            else:
                                # PostgreSQL uses ON CONFLICT DO NOTHING
                                insert_tag_sql = (
                                    "INSERT INTO tags (name) VALUES (:name) ON CONFLICT DO NOTHING"
                                )
                            sync_conn.execute(text(insert_tag_sql), {"name": tag_name})
                            result = sync_conn.execute(
                                text("SELECT id FROM tags WHERE name = :name"),
                                {"name": tag_name},
                            )
                            tag_row = result.fetchone()
                            if tag_row:
                                tag_id = tag_row[0]
                                if is_sqlite:
                                    sql = (
                                        f"INSERT OR IGNORE INTO {child_table} "
                                        f"({fk_column}, tag_id) VALUES (:parent_id, :tag_id)"
                                    )
                                else:
                                    sql = (
                                        f"INSERT INTO {child_table} "
                                        f"({fk_column}, tag_id) VALUES (:parent_id, :tag_id) "
                                        f"ON CONFLICT DO NOTHING"
                                    )
                                sync_conn.execute(
                                    text(sql), {"parent_id": parent_id, "tag_id": tag_id}
                                )
                                child_rows += 1
                    else:
                        columns = [fk_column] + config["value_columns"]
                        values = {fk_column: parent_id}
                        for vc in config["value_columns"]:
                            values[vc] = item.get(vc)
                        placeholders = ", ".join(f":{c}" for c in columns)
                        col_names = ", ".join(columns)
                        sql = f"INSERT INTO {child_table} ({col_names}) VALUES ({placeholders})"
                        sync_conn.execute(text(sql), values)
                        child_rows += 1

            return len(df_main), child_rows

        main_rows, child_rows = await conn.run_sync(sync_import)
        await conn.commit()

    if child_rows > 0:
        message = (
            f"Successfully imported {main_rows} rows into '{table_name}' "
            f"and {child_rows} child records"
        )
    else:
        message = f"Successfully imported {main_rows} rows into '{table_name}'"

    return CSVImportResponse(
        table_name=table_name,
        rows_imported=main_rows,
        message=message,
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
    # Use run_sync to execute on the same connection (important for in-memory SQLite)
    async with engine.connect() as conn:

        def sync_export(sync_conn):
            # Read table into DataFrame
            df = pd.read_sql_table(table_name, sync_conn)
            # Convert to CSV
            csv_content = df.to_csv(index=False, header=request.include_headers)
            return csv_content, len(df)

        csv_content, row_count = await conn.run_sync(sync_export)

    return CSVExportResponse(
        table_name=table_name,
        row_count=row_count,
        csv_content=csv_content,
        message=f"Successfully exported {row_count} rows from '{table_name}'",
    )


async def list_tables(request: ListTablesRequest, engine: AsyncEngine) -> ListTablesResponse:
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
    # Check if this is SQLite (needs special handling for triggers)
    is_sqlite = "sqlite" in str(engine.url)

    if is_sqlite:
        # SQLite: PRAGMA is per-connection, so we must use the same connection throughout.
        # Use run_sync to execute PRAGMA on the underlying driver connection.
        async with engine.connect() as conn:
            # Disable foreign keys via run_sync (works with aiosqlite)
            await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = OFF"))
            try:
                if request.mode == "truncate":
                    # SQLite: Disable triggers temporarily to allow DELETE on immutable tables
                    # Some tables (audit_log, timeline_events) have triggers preventing DELETE
                    result = await conn.execute(
                        text("SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger'")
                    )
                    triggers = result.fetchall()

                    for trigger_name, _, _ in triggers:
                        await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name}"))

                    # Delete all rows
                    for table_name in tables:
                        await conn.execute(text(f"DELETE FROM {table_name}"))
                        affected.append(table_name)

                    # Recreate triggers
                    for _, _, trigger_sql in triggers:
                        if trigger_sql:
                            await conn.execute(text(trigger_sql))

                    message = f"Deleted all data from {len(affected)} tables"

                elif request.mode == "drop":
                    # Drop all tables
                    for table_name in tables:
                        await conn.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
                        affected.append(table_name)

                    message = f"Dropped {len(affected)} tables"
                else:
                    raise ValueError(f"Invalid mode: {request.mode}. Use 'truncate' or 'drop'")

                await conn.commit()
            finally:
                # Re-enable foreign keys
                await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = ON"))
    else:
        async with engine.begin() as conn:
            if request.mode == "truncate":
                # Non-SQLite: Simple delete
                for table_name in tables:
                    await conn.execute(text(f"DELETE FROM {table_name}"))
                    affected.append(table_name)
                message = f"Deleted all data from {len(affected)} tables"
            elif request.mode == "drop":
                for table_name in tables:
                    await conn.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
                    affected.append(table_name)
                message = f"Dropped {len(affected)} tables"
            else:
                raise ValueError(f"Invalid mode: {request.mode}. Use 'truncate' or 'drop'")

    return ClearDatabaseResponse(mode=request.mode, tables_affected=affected, message=message)


# ============================================================================
# Tool Registration for MCP Servers
# ============================================================================


def create_database_tools(
    mcp, engine: AsyncEngine | str, public_tool=None, server_name: str = ""
) -> tuple[int, Callable[[], AsyncEngine]]:
    """
    Register database management tools with an MCP server.

    Args:
        mcp: FastMCP instance to register tools with
        engine: SQLAlchemy async engine, or module path string (e.g., "db.session")
                If a string is provided, the engine is looked up dynamically on each
                tool call to support module reloading (e.g., in tests).
        public_tool: Optional decorator from mcp_auth. If not provided, tools are
                     registered without auth decoration.
        server_name: Optional server name for nested CSV import config lookup.
                     If provided and a csv_import_config.yaml exists for the server,
                     CSV imports will automatically parse JSON columns into child tables.

    Returns:
        Tuple of (number of tools registered, get_engine function).
        The get_engine function returns the current engine instance.
    """
    # Default to no-op decorator if public_tool not provided
    if public_tool is None:
        public_tool = lambda fn: fn  # noqa: E731

    # Create engine getter - if string, look up dynamically; if engine, return directly
    if isinstance(engine, str):
        engine_module_path = engine

        def get_engine() -> AsyncEngine:
            """Get engine dynamically to support module reloading."""
            import importlib
            import sys

            # Reload if already imported to get latest version
            if engine_module_path in sys.modules:
                module = sys.modules[engine_module_path]
            else:
                module = importlib.import_module(engine_module_path)
            return getattr(module, "engine")
    else:
        # Engine provided directly - just return it
        def get_engine() -> AsyncEngine:
            return engine

    @mcp.tool(name="import_csv")
    @public_tool
    async def import_csv_tool(request: CSVImportRequest) -> CSVImportResponse:
        """Import CSV data into a database table."""
        return await import_csv_to_db(request, get_engine(), server_name=server_name)

    @mcp.tool(name="export_csv")
    @public_tool
    async def export_csv_tool(request: CSVExportRequest) -> CSVExportResponse:
        """Export a database table to CSV format."""
        return await export_db_to_csv(request, get_engine())

    @mcp.tool(name="list_tables")
    @public_tool
    async def list_tables_tool(request: ListTablesRequest) -> ListTablesResponse:
        """List all tables in the database."""
        return await list_tables(request, get_engine())

    @mcp.tool(name="clear_database")
    @public_tool
    async def clear_database_tool(request: ClearDatabaseRequest) -> ClearDatabaseResponse:
        """Clear database state (delete data or drop tables)."""
        return await clear_database(request, get_engine())

    return 4, get_engine  # Number of tools registered, engine getter function


# ============================================================================
# Legacy Tool Registration (for REST bridge - deprecated)
# ============================================================================


def get_db_management_tools(
    get_engine: Callable[[], AsyncEngine],
    server_name: str = "",
) -> dict[str, dict[str, Any]]:
    """
    Get database management tools for registration with REST bridge.

    Args:
        get_engine: Callable that returns the current SQLAlchemy async engine.
                    Using a callable allows the tools to get the current engine
                    at request time, rather than capturing a stale reference.
        server_name: Optional server name for nested CSV import config lookup.

    Returns:
        Dictionary of tool name -> tool metadata
    """

    async def import_csv_wrapper(request: CSVImportRequest) -> CSVImportResponse:
        return await import_csv_to_db(request, get_engine(), server_name=server_name)

    async def export_csv_wrapper(request: CSVExportRequest) -> CSVExportResponse:
        return await export_db_to_csv(request, get_engine())

    async def list_tables_wrapper(request: ListTablesRequest) -> ListTablesResponse:
        """List all database tables."""
        return await list_tables(request, get_engine())

    async def clear_db_wrapper(request: ClearDatabaseRequest) -> ClearDatabaseResponse:
        return await clear_database(request, get_engine())

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
            "input_model": ListTablesRequest,
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
