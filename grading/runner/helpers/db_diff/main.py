"""DB Diff helper - extracts and diffs databases between snapshots.

Supports SQLite (.db) files, MySQL/MariaDB INSERT-format SQL dumps, and
PostgreSQL COPY-format SQL dumps.  Auto-detects the database type and uses
the appropriate parsing strategy.
"""

import hashlib
import io
import os
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Any

from loguru import logger

from runner.helpers.artifact_state.parsers.sql import parse_sql_dump
from runner.models import AgentTrajectoryOutput

# Maximum number of rows to include in diff output to avoid token limits
MAX_ROWS_PER_TABLE = 100

# System/internal tables to filter out from SQL dumps (Frappe/ERPNext specific)
# Note: SQLInsertParser lowercases table names, so use lowercase here
SQL_DUMP_SYSTEM_TABLES = {
    "__auth",
    "__usersettings",
    "__global_search",
    "tabversion",
    "tabactivity log",
    "tabcomment",
    "tabview log",
    "tabaccess log",
    "tabenergy point log",
    "tabsession default",
}


@dataclass
class DbConnection:
    """Wrapper for SQLite connection with temp file tracking for cleanup."""

    conn: sqlite3.Connection
    temp_path: str

    def close(self) -> None:
        """Close connection and clean up temp file."""
        try:
            self.conn.close()
        finally:
            try:
                os.unlink(self.temp_path)
            except OSError:
                pass  # Best effort cleanup


def _extract_db_from_snapshot(
    snapshot_bytes: io.BytesIO, db_path: str
) -> DbConnection | None:
    """
    Extract a specific database file from a snapshot zip.

    Args:
        snapshot_bytes: Snapshot zip as BytesIO
        db_path: Path to the database file within the zip

    Returns:
        DbConnection wrapper if found, None otherwise
    """
    snapshot_bytes.seek(0)

    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            # Check for exact path first
            if db_path in zf.namelist():
                db_bytes = zf.read(db_path)
            else:
                # Try with leading slash stripped or added
                alt_path = db_path.lstrip("/")
                if alt_path in zf.namelist():
                    db_bytes = zf.read(alt_path)
                else:
                    alt_path = "/" + db_path.lstrip("/")
                    if alt_path in zf.namelist():
                        db_bytes = zf.read(alt_path)
                    else:
                        return None

            # Write to temp file (SQLite needs file path)
            temp_file = tempfile.NamedTemporaryFile(
                suffix=".db", delete=False, mode="wb"
            )
            temp_file.write(db_bytes)
            temp_file.flush()
            temp_path = temp_file.name
            temp_file.close()

            return DbConnection(
                conn=sqlite3.connect(temp_path),
                temp_path=temp_path,
            )
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        logger.warning(f"Failed to extract database {db_path}: {e}")
        return None


def _find_all_dbs_in_snapshot(snapshot_bytes: io.BytesIO) -> list[str]:
    """Find all .db files in a snapshot."""
    snapshot_bytes.seek(0)
    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            return [f for f in zf.namelist() if f.endswith(".db")]
    except zipfile.BadZipFile:
        return []


def _get_table_names(conn: sqlite3.Connection) -> list[str]:
    """Get all table names from a database."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return [row[0] for row in cursor.fetchall()]


def _get_table_schema(conn: sqlite3.Connection, table_name: str) -> list[str]:
    """Get column names for a table."""
    cursor = conn.execute(f'PRAGMA table_info("{table_name}")')
    return [row[1] for row in cursor.fetchall()]


def _get_primary_key(conn: sqlite3.Connection, table_name: str) -> list[str]:
    """Get primary key columns for a table."""
    cursor = conn.execute(f'PRAGMA table_info("{table_name}")')
    pk_cols = [row[1] for row in cursor.fetchall() if row[5] > 0]
    return pk_cols


def _row_hash(row: tuple[Any, ...]) -> str:
    """Create a hash for a row for comparison."""
    return hashlib.md5(str(row).encode()).hexdigest()


def _safe_value(v: Any) -> Any:
    """Convert a value to a JSON-serializable type."""
    if isinstance(v, (bytes, memoryview)):
        return bytes(v).decode("utf-8", errors="replace")
    return v


def _dict_from_row(columns: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
    """Convert a row tuple to a dict with column names."""
    return {col: _safe_value(val) for col, val in zip(columns, row, strict=False)}


def _diff_table(
    initial_conn: DbConnection | None,
    final_conn: DbConnection | None,
    table_name: str,
) -> dict[str, Any]:
    """
    Diff a single table between initial and final states.

    Returns dict with rows_added, rows_deleted, rows_modified.
    """
    result: dict[str, Any] = {
        "rows_added": [],
        "rows_deleted": [],
        "rows_modified": [],
        "error": None,
    }

    # Handle missing connections
    if initial_conn is None and final_conn is None:
        result["error"] = "Table not found in either snapshot"
        return result

    # Get schemas
    initial_cols: list[str] = []
    final_cols: list[str] = []

    if initial_conn:
        try:
            initial_cols = _get_table_schema(initial_conn.conn, table_name)
        except sqlite3.OperationalError:
            pass

    if final_conn:
        try:
            final_cols = _get_table_schema(final_conn.conn, table_name)
        except sqlite3.OperationalError:
            pass

    # Use final columns if available, otherwise initial
    columns = final_cols or initial_cols
    if not columns:
        result["error"] = f"Could not get schema for table {table_name}"
        return result

    # Get primary key for row matching
    pk_cols: list[str] = []
    if final_conn:
        pk_cols = _get_primary_key(final_conn.conn, table_name)
    elif initial_conn:
        pk_cols = _get_primary_key(initial_conn.conn, table_name)

    # Fetch rows
    initial_rows: dict[str, tuple[Any, ...]] = {}
    final_rows: dict[str, tuple[Any, ...]] = {}

    if initial_conn and initial_cols:
        try:
            cursor = initial_conn.conn.execute(
                f'SELECT * FROM "{table_name}" LIMIT {MAX_ROWS_PER_TABLE * 2}'
            )
            # Compute pk_indices once outside the loop
            pk_indices = (
                [initial_cols.index(c) for c in pk_cols if c in initial_cols]
                if pk_cols
                else []
            )
            for row in cursor.fetchall():
                if pk_indices:
                    # Use primary key as row identifier
                    key = str(tuple(row[i] for i in pk_indices))
                else:
                    # Use full row hash (note: duplicate rows will collapse to one entry)
                    key = _row_hash(row)
                initial_rows[key] = row
        except sqlite3.OperationalError as e:
            logger.warning(f"Error reading initial table {table_name}: {e}")

    if final_conn and final_cols:
        try:
            cursor = final_conn.conn.execute(
                f'SELECT * FROM "{table_name}" LIMIT {MAX_ROWS_PER_TABLE * 2}'
            )
            # Compute pk_indices once outside the loop
            pk_indices = (
                [final_cols.index(c) for c in pk_cols if c in final_cols]
                if pk_cols
                else []
            )
            for row in cursor.fetchall():
                if pk_indices:
                    key = str(tuple(row[i] for i in pk_indices))
                else:
                    # Use full row hash (note: duplicate rows will collapse to one entry)
                    key = _row_hash(row)
                final_rows[key] = row
        except sqlite3.OperationalError as e:
            logger.warning(f"Error reading final table {table_name}: {e}")

    # Compute diff
    initial_keys = set(initial_rows.keys())
    final_keys = set(final_rows.keys())

    # Added rows (in final but not initial)
    for key in final_keys - initial_keys:
        if len(result["rows_added"]) < MAX_ROWS_PER_TABLE:
            result["rows_added"].append(_dict_from_row(final_cols, final_rows[key]))

    # Deleted rows (in initial but not final)
    for key in initial_keys - final_keys:
        if len(result["rows_deleted"]) < MAX_ROWS_PER_TABLE:
            result["rows_deleted"].append(
                _dict_from_row(initial_cols, initial_rows[key])
            )

    # Modified rows (same key but different values)
    for key in initial_keys & final_keys:
        initial_row = initial_rows[key]
        final_row = final_rows[key]
        if _row_hash(initial_row) != _row_hash(final_row):
            if len(result["rows_modified"]) < MAX_ROWS_PER_TABLE:
                result["rows_modified"].append(
                    {
                        "before": _dict_from_row(initial_cols, initial_row),
                        "after": _dict_from_row(final_cols, final_row),
                    }
                )

    return result


# =============================================================================
# SQL Dump Parsing Functions (MySQL/MariaDB INSERT + PostgreSQL COPY)
# =============================================================================


def _find_sql_dump_in_snapshot(snapshot_bytes: io.BytesIO) -> str | None:
    """Find and return the first SQL dump from a snapshot zip.

    Scans all entries at any depth for files whose name ends with
    ``_dump.sql`` or is exactly ``database_dump.sql``.  Prefers shallower
    paths; among ties, prefers larger files.

    Returns:
        SQL dump content as string if found, None otherwise.
    """
    snapshot_bytes.seek(0)
    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            # (depth, -size, path) — sort ascending → shallowest + largest first
            candidates: list[tuple[int, int, str]] = []
            for info in zf.infolist():
                basename = info.filename.rstrip("/").rsplit("/", 1)[-1]
                if basename.endswith("_dump.sql") or basename == "database_dump.sql":
                    depth = info.filename.count("/")
                    candidates.append((depth, -info.file_size, info.filename))

            if not candidates:
                return None

            candidates.sort()
            best = candidates[0][2]
            logger.info(f"Found SQL dump in snapshot: {best}")
            return zf.read(best).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        logger.warning(f"Failed to find SQL dump in snapshot: {e}")
        return None


def _sql_row_hash(row: dict[str, Any]) -> str:
    """Create a hash for a SQL dump row dict for comparison."""
    # Sort keys for consistent hashing
    sorted_items = sorted(row.items())
    return hashlib.md5(str(sorted_items).encode()).hexdigest()


def _get_sql_row_key(row: dict[str, Any], pk_columns: list[str] | None = None) -> str:
    """
    Get a unique key for a row, using primary key if available.

    For SQL dumps without schema info, we use the first column as a heuristic PK
    (first column is often the ID), or fall back to full row hash.

    Note: ERPNext uses string IDs like 'SO-00001', so we accept any non-null
    value as a potential primary key, not just integers.
    """
    if pk_columns:
        return str(tuple(row.get(col) for col in pk_columns))

    # Heuristic: use first column as ID (works for col0 or named columns like "id")
    # Python 3.7+ dicts maintain insertion order, so first key = first column
    if row:
        first_key = next(iter(row.keys()), None)
        if first_key:
            val = row[first_key]
            if val is not None:
                return f"pk_{val}"

    # Fallback: full row hash
    return _sql_row_hash(row)


def _diff_sql_table_data(
    initial_rows: list[dict[str, Any]],
    final_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Diff table data between initial and final states for SQL dumps.

    Returns dict matching DB_DIFF output format:
    {
        "rows_added": [...],
        "rows_deleted": [...],
        "rows_modified": [{"before": {...}, "after": {...}}],
        "error": None
    }
    """
    result: dict[str, Any] = {
        "rows_added": [],
        "rows_deleted": [],
        "rows_modified": [],
        "error": None,
    }

    # Build lookup dicts by row key
    initial_by_key: dict[str, dict[str, Any]] = {}
    for row in initial_rows:
        key = _get_sql_row_key(row)
        initial_by_key[key] = row

    final_by_key: dict[str, dict[str, Any]] = {}
    for row in final_rows:
        key = _get_sql_row_key(row)
        final_by_key[key] = row

    initial_keys = set(initial_by_key.keys())
    final_keys = set(final_by_key.keys())

    # Added rows (in final but not initial)
    for key in final_keys - initial_keys:
        if len(result["rows_added"]) < MAX_ROWS_PER_TABLE:
            result["rows_added"].append(final_by_key[key])

    # Deleted rows (in initial but not final)
    for key in initial_keys - final_keys:
        if len(result["rows_deleted"]) < MAX_ROWS_PER_TABLE:
            result["rows_deleted"].append(initial_by_key[key])

    # Modified rows (same key but different values)
    for key in initial_keys & final_keys:
        initial_row = initial_by_key[key]
        final_row = final_by_key[key]
        if _sql_row_hash(initial_row) != _sql_row_hash(final_row):
            if len(result["rows_modified"]) < MAX_ROWS_PER_TABLE:
                result["rows_modified"].append(
                    {
                        "before": initial_row,
                        "after": final_row,
                    }
                )

    return result


async def _diff_sql_dumps(
    initial_snapshot_bytes: io.BytesIO,
    final_snapshot_bytes: io.BytesIO,
) -> dict[str, Any]:
    """
    Parse and diff SQL dump files between initial and final snapshots.

    Supports both MySQL/MariaDB INSERT format and PostgreSQL COPY format.
    Auto-detects the format and selects the appropriate parser.

    This produces output in the SAME FORMAT as the SQLite diff, so the same
    LLM eval can be used for both.

    Args:
        initial_snapshot_bytes: Task snapshot (initial state after populate hook)
        final_snapshot_bytes: Trajectory snapshot (final state after agent)

    Returns:
        Dict with structure matching SQLite diff output
    """
    result: dict[str, Any] = {
        "databases": {},
        "summary": {
            "total_rows_added": 0,
            "total_rows_deleted": 0,
            "total_rows_modified": 0,
            "tables_changed": [],
            "databases_found": [],
        },
    }

    # Find SQL dumps — scans recursively for *_dump.sql / database_dump.sql
    initial_sql = _find_sql_dump_in_snapshot(initial_snapshot_bytes)
    final_sql = _find_sql_dump_in_snapshot(final_snapshot_bytes)

    if not initial_sql and not final_sql:
        logger.warning("No SQL dump files found in either snapshot")
        return result

    logger.info(
        f"Found SQL dumps: initial={bool(initial_sql)}, final={bool(final_sql)}"
    )

    # parse_sql_dump auto-detects MySQL INSERT vs PostgreSQL COPY format
    initial_tables = parse_sql_dump(initial_sql) if initial_sql else {}
    final_tables = parse_sql_dump(final_sql) if final_sql else {}

    logger.info(
        f"Parsed tables: initial={list(initial_tables.keys())}, "
        f"final={list(final_tables.keys())}"
    )

    # Use "sql_dump" as the virtual database path (matches expected structure)
    db_path = "sql_dump"
    result["summary"]["databases_found"] = [db_path]

    # Get all table names from both dumps
    all_tables = set(initial_tables.keys()) | set(final_tables.keys())

    # Filter out system/internal tables
    filtered_tables = all_tables - SQL_DUMP_SYSTEM_TABLES

    db_result: dict[str, Any] = {"tables": {}}

    for table_name in sorted(filtered_tables):
        initial_rows = initial_tables.get(table_name, [])
        final_rows = final_tables.get(table_name, [])

        table_diff = _diff_sql_table_data(initial_rows, final_rows)
        db_result["tables"][table_name] = table_diff

        # Update summary
        added = len(table_diff.get("rows_added", []))
        deleted = len(table_diff.get("rows_deleted", []))
        modified = len(table_diff.get("rows_modified", []))

        result["summary"]["total_rows_added"] += added
        result["summary"]["total_rows_deleted"] += deleted
        result["summary"]["total_rows_modified"] += modified

        if added or deleted or modified:
            result["summary"]["tables_changed"].append(f"{db_path}:{table_name}")

    result["databases"][db_path] = db_result

    logger.info(
        f"SQL dump diff complete: "
        f"{result['summary']['total_rows_added']} added, "
        f"{result['summary']['total_rows_deleted']} deleted, "
        f"{result['summary']['total_rows_modified']} modified"
    )

    return result


async def db_diff_helper(
    initial_snapshot_bytes: io.BytesIO,
    final_snapshot_bytes: io.BytesIO,
    trajectory: AgentTrajectoryOutput,  # unused but required by helper interface
) -> dict[str, Any]:
    """
    Extract and diff databases between initial and final snapshots.

    Auto-detects database type:
    1. First tries to find SQLite .db files
    2. If none found, tries to find MySQL/MariaDB SQL dump files
    3. If neither found, returns empty result

    Compares all databases found and produces a structured diff showing
    rows added, deleted, and modified per table.

    Args:
        initial_snapshot_bytes: Task snapshot (initial state after populate hook)
        final_snapshot_bytes: Trajectory snapshot (final state after agent)
        trajectory: Agent trajectory output (for metadata)

    Returns:
        Dict with structure:
        {
            "databases": {
                "<db_path>": {
                    "tables": {
                        "<table_name>": {
                            "rows_added": [...],
                            "rows_deleted": [...],
                            "rows_modified": [{"before": {...}, "after": {...}}],
                        }
                    }
                }
            },
            "summary": {
                "total_rows_added": N,
                "total_rows_deleted": N,
                "total_rows_modified": N,
                "tables_changed": ["table1", "table2"],
                "databases_found": ["db1.db", "db2.db"],
            }
        }
    """
    result: dict[str, Any] = {
        "databases": {},
        "summary": {
            "total_rows_added": 0,
            "total_rows_deleted": 0,
            "total_rows_modified": 0,
            "tables_changed": [],
            "databases_found": [],
        },
    }

    # Auto-detect database type: SQLite first, then SQL dump

    # Try SQLite .db files first
    initial_dbs = set(_find_all_dbs_in_snapshot(initial_snapshot_bytes))
    final_dbs = set(_find_all_dbs_in_snapshot(final_snapshot_bytes))
    all_dbs = initial_dbs | final_dbs

    if not all_dbs:
        # No SQLite databases found, try SQL dump files
        logger.info("No SQLite databases found, checking for SQL dump files...")
        return await _diff_sql_dumps(initial_snapshot_bytes, final_snapshot_bytes)

    # SQLite databases found - use SQLite parsing
    result["summary"]["databases_found"] = sorted(all_dbs)
    logger.info(f"Found {len(all_dbs)} SQLite database(s) to diff: {all_dbs}")

    for db_path in sorted(all_dbs):
        logger.info(f"Diffing database: {db_path}")

        initial_conn = _extract_db_from_snapshot(initial_snapshot_bytes, db_path)
        final_conn = _extract_db_from_snapshot(final_snapshot_bytes, db_path)

        try:
            db_result: dict[str, Any] = {"tables": {}}

            # Get all tables from both connections
            initial_tables = (
                set(_get_table_names(initial_conn.conn)) if initial_conn else set()
            )
            final_tables = (
                set(_get_table_names(final_conn.conn)) if final_conn else set()
            )
            all_tables = initial_tables | final_tables

            for table_name in sorted(all_tables):
                table_diff = _diff_table(initial_conn, final_conn, table_name)
                db_result["tables"][table_name] = table_diff

                # Update summary
                added = len(table_diff.get("rows_added", []))
                deleted = len(table_diff.get("rows_deleted", []))
                modified = len(table_diff.get("rows_modified", []))

                result["summary"]["total_rows_added"] += added
                result["summary"]["total_rows_deleted"] += deleted
                result["summary"]["total_rows_modified"] += modified

                if added or deleted or modified:
                    result["summary"]["tables_changed"].append(
                        f"{db_path}:{table_name}"
                    )

            result["databases"][db_path] = db_result
        finally:
            # Clean up connections even if an exception occurs
            if initial_conn:
                initial_conn.close()
            if final_conn:
                final_conn.close()

    logger.info(
        f"DB diff complete: "
        f"{result['summary']['total_rows_added']} added, "
        f"{result['summary']['total_rows_deleted']} deleted, "
        f"{result['summary']['total_rows_modified']} modified"
    )

    return result
