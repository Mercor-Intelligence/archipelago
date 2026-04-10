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
    """Export a database table to CSV with optional filtering."""

    table_name: str = Field(..., description="Name of the table to export")
    include_headers: bool = Field(default=True, description="Include column headers")
    filters: dict[str, Any] | None = Field(
        default=None,
        description="Column filters to apply (e.g., {'org_id': 'ORG001', 'status': 'open'})",
    )
    limit: int | None = Field(
        default=None,
        description="Maximum number of rows to return (default: no limit, max: 10000)",
    )
    format: str = Field(
        default="csv",
        description="Output format: 'csv' or 'json' (default: csv)",
    )


class CSVExportResponse(BaseModel):
    """Result of CSV export operation."""

    table_name: str = Field(..., description="Table that was exported")
    row_count: int = Field(..., description="Number of rows exported")
    csv_content: str | None = Field(None, description="CSV content as string (when format=csv)")
    rows: list[dict[str, Any]] | None = Field(
        None, description="Rows as JSON array (when format=json)"
    )
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
# Schema and Validation Models (for MCP tools)
# ============================================================================


class GetSchemaRequest(BaseModel):
    """Request to get the database schema."""

    table_name: str | None = Field(
        default=None,
        description="Get schema for a specific table. If not provided, returns all.",
    )


class ColumnSchema(BaseModel):
    """Schema information for a single column."""

    name: str = Field(..., description="Column name")
    type: str = Field(..., description="Python type name (str, int, float, bool, datetime, dict)")
    nullable: bool = Field(..., description="Whether the column allows NULL values")
    is_primary_key: bool = Field(default=False, description="Whether this is a primary key")
    is_foreign_key: bool = Field(default=False, description="Whether this is a foreign key")
    fk_target: str | None = Field(None, description="Foreign key target (table.column) if FK")
    required: bool = Field(default=False, description="Whether value is required")
    enum_values: list[str] | None = Field(None, description="Valid enum values if restricted")


class TableSchema(BaseModel):
    """Schema information for a single table."""

    name: str = Field(..., description="Table name")
    columns: list[ColumnSchema] = Field(..., description="List of columns")
    primary_keys: list[str] = Field(..., description="Primary key column names")
    foreign_keys: dict[str, str] = Field(
        default_factory=dict, description="Map of FK column to target (table.column)"
    )
    required_columns: list[str] = Field(
        default_factory=list, description="Columns that require a value"
    )


class GetSchemaResponse(BaseModel):
    """Database schema response."""

    tables: list[TableSchema] = Field(..., description="List of table schemas")
    import_order: list[str] = Field(
        ..., description="Tables in topological order (parent tables first for FK dependencies)"
    )
    table_count: int = Field(..., description="Total number of tables")


class ValidationError(BaseModel):
    """A single validation error."""

    row: int | None = Field(None, description="Row number (starting from 2, after header)")
    column: str | None = Field(None, description="Column name where error occurred")
    error_type: str = Field(
        ...,
        description="Error category: MISSING_REQUIRED, NULL_VALUE, TYPE_ERROR, INVALID_ENUM, etc.",
    )
    message: str = Field(..., description="Human-readable error description")


class ValidateCSVRequest(BaseModel):
    """Request to validate CSV content against the database schema."""

    table_name: str = Field(
        ...,
        description="Name of the table to validate against",
        json_schema_extra={
            "x-populate-from": "list_tables",
            "x-populate-field": "tables",
        },
    )
    csv_content: str = Field(..., description="CSV content (plain text or base64-encoded)")


class ValidateCSVResponse(BaseModel):
    """Result of CSV validation."""

    success: bool = Field(..., description="Whether validation passed with no errors")
    table_name: str = Field(..., description="Table that was validated against")
    row_count: int = Field(..., description="Number of data rows in the CSV")
    errors: list[ValidationError] = Field(
        default_factory=list, description="List of validation errors"
    )
    sample_rows: list[dict[str, Any]] = Field(
        default_factory=list, description="First 3 rows of data for preview"
    )
    message: str = Field(..., description="Summary message")


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
    """Export database table to CSV or JSON with optional filtering."""
    # Validate table name (basic check)
    table_name = request.table_name.strip()
    if not table_name or not table_name.replace("_", "").isalnum():
        raise ValueError(
            f"Invalid table name '{table_name}': use only letters, numbers, and underscores"
        )

    # Validate format
    if request.format not in ("csv", "json"):
        raise ValueError(f"Invalid format '{request.format}': use 'csv' or 'json'")

    # Validate limit
    if request.limit is not None:
        if request.limit < 1 or request.limit > 10000:
            raise ValueError("Limit must be between 1 and 10000")

    # Check if table exists and validate filter columns
    async with engine.connect() as conn:
        inspector = await conn.run_sync(lambda c: inspect(c))
        if not await conn.run_sync(lambda c: inspector.has_table(table_name)):
            raise ValueError(f"Table '{table_name}' does not exist")

        # Validate filter column names against table schema
        if request.filters:
            columns = await conn.run_sync(lambda c: inspector.get_columns(table_name))
            column_names = {col["name"] for col in columns}
            for filter_col in request.filters.keys():
                if filter_col not in column_names:
                    raise ValueError(
                        f"Invalid filter column '{filter_col}' for table '{table_name}'. "
                        f"Available columns: {sorted(column_names)}"
                    )

    # Use pandas to read from SQL and convert to desired format
    import asyncio

    def sync_export():
        from sqlalchemy import create_engine

        # Create sync engine from async engine URL
        sync_engine = create_engine(
            str(engine.url).replace("+aiosqlite", ""),
            echo=False,
        )

        try:
            # Build query with filters
            query = f"SELECT * FROM {table_name}"
            params = {}

            if request.filters:
                where_clauses = []
                for col, val in request.filters.items():
                    # Use parameterized queries to prevent SQL injection
                    param_name = f"filter_{col}"
                    where_clauses.append(f"{col} = :{param_name}")
                    params[param_name] = val
                query += " WHERE " + " AND ".join(where_clauses)

            if request.limit:
                query += f" LIMIT {request.limit}"

            # Read with filters applied (using parameterized query)
            df = pd.read_sql_query(text(query), sync_engine, params=params)

            # Convert to requested format
            if request.format == "json":
                # Convert DataFrame to list of dicts for JSON output
                rows = df.to_dict(orient="records")
                return None, rows, len(df)
            else:
                # Convert to CSV
                csv_content = df.to_csv(index=False, header=request.include_headers)
                return csv_content, None, len(df)
        finally:
            sync_engine.dispose()

    # Run sync operation in thread pool
    csv_content, rows, row_count = await asyncio.to_thread(sync_export)

    filter_msg = f" with filters {request.filters}" if request.filters else ""
    return CSVExportResponse(
        table_name=table_name,
        row_count=row_count,
        csv_content=csv_content,
        rows=rows,
        message=f"Successfully exported {row_count} rows from '{table_name}'{filter_msg}",
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


async def get_schema(
    request: GetSchemaRequest, engine: AsyncEngine, base: type | None = None
) -> GetSchemaResponse:
    """Get the database schema for tables.

    If base (SQLAlchemy declarative base) is provided, uses full introspection
    with FK relationships and topological ordering. Otherwise, uses basic
    inspection of the database.
    """
    async with engine.connect() as conn:
        inspector = await conn.run_sync(lambda c: inspect(c))
        table_names = await conn.run_sync(lambda c: inspector.get_table_names())

        # Filter to specific table if requested
        if request.table_name:
            if request.table_name not in table_names:
                raise ValueError(f"Table '{request.table_name}' does not exist")
            table_names = [request.table_name]

        tables: list[TableSchema] = []
        # Build dependency graph for topological sort
        dependencies: dict[str, set[str]] = {t: set() for t in table_names}

        for table_name in sorted(table_names):
            columns_info = await conn.run_sync(lambda c, t=table_name: inspector.get_columns(t))
            pk_info = await conn.run_sync(lambda c, t=table_name: inspector.get_pk_constraint(t))
            fk_info = await conn.run_sync(lambda c, t=table_name: inspector.get_foreign_keys(t))

            primary_keys = pk_info.get("constrained_columns", []) if pk_info else []
            foreign_keys: dict[str, str] = {}

            for fk in fk_info:
                for i, col in enumerate(fk.get("constrained_columns", [])):
                    ref_table = fk.get("referred_table", "")
                    ref_cols = fk.get("referred_columns", [])
                    ref_col = ref_cols[i] if i < len(ref_cols) else "id"
                    foreign_keys[col] = f"{ref_table}.{ref_col}"
                    # Track dependency for topological sort
                    if ref_table in dependencies and ref_table != table_name:
                        dependencies[table_name].add(ref_table)

            columns: list[ColumnSchema] = []
            required_columns: list[str] = []

            for col in columns_info:
                col_name = col["name"]
                col_type = col.get("type")

                # Map SQLAlchemy type to Python type name
                type_name = "str"
                if col_type:
                    type_str = str(col_type).upper()
                    if "INT" in type_str:
                        type_name = "int"
                    elif "FLOAT" in type_str or "NUMERIC" in type_str or "DECIMAL" in type_str:
                        type_name = "float"
                    elif "BOOL" in type_str:
                        type_name = "bool"
                    elif "DATE" in type_str or "TIME" in type_str:
                        type_name = "datetime"
                    elif "JSON" in type_str:
                        type_name = "dict"

                nullable = col.get("nullable", True)
                has_default = col.get("default") is not None
                is_pk = col_name in primary_keys
                is_fk = col_name in foreign_keys
                required = not nullable and not has_default and not is_pk

                if required:
                    required_columns.append(col_name)

                columns.append(
                    ColumnSchema(
                        name=col_name,
                        type=type_name,
                        nullable=nullable,
                        is_primary_key=is_pk,
                        is_foreign_key=is_fk,
                        fk_target=foreign_keys.get(col_name),
                        required=required,
                        enum_values=None,  # Would need ORM model for this
                    )
                )

            tables.append(
                TableSchema(
                    name=table_name,
                    columns=columns,
                    primary_keys=primary_keys,
                    foreign_keys=foreign_keys,
                    required_columns=required_columns,
                )
            )

    # Topological sort using Kahn's algorithm
    in_degree: dict[str, int] = {t: 0 for t in table_names}
    for table, deps in dependencies.items():
        in_degree[table] = len(deps)

    # Start with tables that have no dependencies
    queue = [t for t, deg in in_degree.items() if deg == 0]
    import_order: list[str] = []

    while queue:
        node = queue.pop(0)
        import_order.append(node)
        # Reduce in-degree for tables that depend on this one
        for table, deps in dependencies.items():
            if node in deps:
                in_degree[table] -= 1
                if in_degree[table] == 0:
                    queue.append(table)

    # Add any remaining tables (circular dependencies)
    remaining = set(table_names) - set(import_order)
    import_order.extend(sorted(remaining))

    return GetSchemaResponse(
        tables=tables,
        import_order=import_order,
        table_count=len(tables),
    )


async def validate_csv_content(
    request: ValidateCSVRequest, engine: AsyncEngine
) -> ValidateCSVResponse:
    """Validate CSV content against the database schema without importing."""
    from datetime import datetime as dt

    # Get schema info for the table
    schema_request = GetSchemaRequest(table_name=request.table_name)
    schema_response = await get_schema(schema_request, engine)

    if not schema_response.tables:
        raise ValueError(f"Table '{request.table_name}' does not exist")

    table_schema = schema_response.tables[0]

    # Decode CSV if base64
    csv_text = request.csv_content
    encoding = detect_csv_encoding(csv_text)
    if encoding == "base64":
        import base64 as b64

        csv_text = b64.b64decode(csv_text).decode("utf-8")

    # Parse CSV
    try:
        df = pd.read_csv(
            io.StringIO(csv_text),
            skipinitialspace=True,
            encoding="utf-8",
        )
    except Exception as e:
        return ValidateCSVResponse(
            success=False,
            table_name=request.table_name,
            row_count=0,
            errors=[
                ValidationError(
                    row=None,
                    column=None,
                    error_type="PARSE_ERROR",
                    message=f"Failed to parse CSV: {e}",
                )
            ],
            sample_rows=[],
            message=f"Failed to parse CSV: {e}",
        )

    if df.empty:
        return ValidateCSVResponse(
            success=False,
            table_name=request.table_name,
            row_count=0,
            errors=[
                ValidationError(
                    row=None,
                    column=None,
                    error_type="EMPTY_CSV",
                    message="CSV must have at least one data row",
                )
            ],
            sample_rows=[],
            message="CSV must have at least one data row",
        )

    # Normalize column names
    df.columns = [col.lower().replace(" ", "_").replace("-", "_") for col in df.columns]

    # Build column lookup from schema
    schema_columns = {col.name: col for col in table_schema.columns}
    errors: list[ValidationError] = []

    # Check for missing required columns
    csv_columns = set(df.columns)
    for col in table_schema.required_columns:
        if col not in csv_columns:
            errors.append(
                ValidationError(
                    row=None,
                    column=col,
                    error_type="MISSING_REQUIRED",
                    message=f"Missing required column: {col}",
                )
            )

    # Validate each row
    for row_num, row in df.iterrows():
        actual_row_num = int(row_num) + 2  # Account for 0-indexing and header row

        for col_name in df.columns:
            if col_name not in schema_columns:
                continue  # Skip columns not in schema

            col_schema = schema_columns[col_name]
            value = row[col_name]

            # Check for null values
            if pd.isna(value) or (isinstance(value, str) and value.strip() == ""):
                if col_schema.required:
                    errors.append(
                        ValidationError(
                            row=actual_row_num,
                            column=col_name,
                            error_type="NULL_VALUE",
                            message=f"Required column '{col_name}' is empty",
                        )
                    )
                continue

            # Type validation
            value_str = str(value).strip()
            type_error = None

            if col_schema.type == "int":
                try:
                    # Use float() first to handle pandas float-promotion (e.g., "1.0")
                    int(float(value_str))
                except ValueError:
                    type_error = f"Cannot convert '{value_str}' to int"
            elif col_schema.type == "float":
                try:
                    float(value_str.replace(",", "").replace("$", ""))
                except ValueError:
                    type_error = f"Cannot convert '{value_str}' to float"
            elif col_schema.type == "bool":
                if value_str.lower() not in ("true", "false", "1", "0", "yes", "no"):
                    type_error = f"Invalid boolean: '{value_str}'"
            elif col_schema.type == "datetime":
                parsed = False
                norm_value = value_str
                if norm_value.endswith("Z"):
                    norm_value = norm_value[:-1] + "+00:00"
                # Strip timezone offset (+HH:MM or -HH:MM) and fractional seconds
                # Handle both positive and negative UTC offsets
                base_value = norm_value
                for sep in ["+", "-"]:
                    # Find last occurrence (timezone is at end, not date separator)
                    idx = base_value.rfind(sep)
                    # Only strip if it looks like a timezone (after T and has :)
                    if idx > 10 and ":" in base_value[idx:]:
                        base_value = base_value[:idx]
                        break
                # Strip fractional seconds
                base_value = base_value.split(".")[0]
                for fmt in ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y"]:
                    try:
                        dt.strptime(base_value, fmt)
                        parsed = True
                        break
                    except ValueError:
                        continue
                if not parsed:
                    type_error = f"Invalid datetime: '{value_str}'"

            if type_error:
                errors.append(
                    ValidationError(
                        row=actual_row_num,
                        column=col_name,
                        error_type="TYPE_ERROR",
                        message=type_error,
                    )
                )

            # Enum validation
            if col_schema.enum_values and value_str not in col_schema.enum_values:
                msg = f"Invalid '{value_str}'. Must be: {col_schema.enum_values}"
                errors.append(
                    ValidationError(
                        row=actual_row_num,
                        column=col_name,
                        error_type="INVALID_ENUM",
                        message=msg,
                    )
                )

    # Get sample rows for preview
    sample_rows: list[dict[str, Any]] = []
    for i, row in df.head(3).iterrows():
        sample_rows.append({k: ("" if pd.isna(v) else str(v)) for k, v in row.items()})

    success = len(errors) == 0
    if success:
        message = f"Validation passed: {len(df)} rows ready for import"
    else:
        message = f"Validation failed: {len(errors)} error(s) found"

    return ValidateCSVResponse(
        success=success,
        table_name=request.table_name,
        row_count=len(df),
        errors=errors,
        sample_rows=sample_rows,
        message=message,
    )


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
    async def import_csv_tool(request: CSVImportRequest) -> dict:
        """Import CSV data into a database table."""
        result = await import_csv_to_db(request, get_engine(), server_name=server_name)
        return result.model_dump(by_alias=True)

    @mcp.tool(name="export_csv")
    @public_tool
    async def export_csv_tool(request: CSVExportRequest) -> dict:
        """Export a database table to CSV or JSON format with optional filtering."""
        result = await export_db_to_csv(request, get_engine())
        return result.model_dump(by_alias=True)

    @mcp.tool(name="list_tables")
    @public_tool
    async def list_tables_tool(request: ListTablesRequest) -> dict:
        """List all tables in the database."""
        result = await list_tables(request, get_engine())
        return result.model_dump(by_alias=True)

    @mcp.tool(name="clear_database")
    @public_tool
    async def clear_database_tool(request: ClearDatabaseRequest) -> dict:
        """Clear database state (delete data or drop tables)."""
        result = await clear_database(request, get_engine())
        return result.model_dump(by_alias=True)

    @mcp.tool(name="get_schema")
    @public_tool
    async def get_schema_tool(request: GetSchemaRequest) -> dict:
        """Get database schema including tables, columns, types, and FK relationships."""
        result = await get_schema(request, get_engine())
        return result.model_dump(by_alias=True)

    @mcp.tool(name="validate_csv")
    @public_tool
    async def validate_csv_tool(request: ValidateCSVRequest) -> dict:
        """Validate CSV against schema. Returns errors and sample rows for preview."""
        result = await validate_csv_content(request, get_engine())
        return result.model_dump(by_alias=True)

    return 6, get_engine  # Number of tools registered, engine getter function


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

    async def get_schema_wrapper(request: GetSchemaRequest) -> GetSchemaResponse:
        return await get_schema(request, get_engine())

    async def validate_csv_wrapper(request: ValidateCSVRequest) -> ValidateCSVResponse:
        return await validate_csv_content(request, get_engine())

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
        "get_schema": {
            "function": get_schema_wrapper,
            "input_model": GetSchemaRequest,
            "output_model": GetSchemaResponse,
            "description": "Get database schema including tables, columns, types, and foreign keys",
        },
        "validate_csv": {
            "function": validate_csv_wrapper,
            "input_model": ValidateCSVRequest,
            "output_model": ValidateCSVResponse,
            "description": "Validate CSV content against database schema without importing",
        },
    }
