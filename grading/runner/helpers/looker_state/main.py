"""Looker State Helper - Extracts Looker databases from snapshot.

Looker uses DuckDB for offline mode. The data can come from:
1. Pre-built `offline.duckdb` file
2. CSV files in `.apps_data/looker/` that get loaded into DuckDB

This helper finds and loads these data sources for verifier queries.

Note on Resource Management:
---------------------------
DuckDB connections are kept open for SQL queries. The connections and temp files
persist until grading completes (runs in ephemeral containers). Call
cleanup_looker_state() if explicit cleanup is needed.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from runner.models import AgentTrajectoryOutput

# Try to import duckdb at runtime, fall back to sqlite3 if not available
_has_duckdb: bool = False
duckdb_module: Any = None
try:
    import duckdb as duckdb_module

    _has_duckdb = True
except ImportError:
    logger.warning("duckdb not installed, falling back to sqlite3 for CSV loading")

HAS_DUCKDB: bool = _has_duckdb

LOOKER_PATHS = [
    ".apps_data/looker/",
    "looker/",
    # Note: Do NOT include ".apps_data/" alone as it would match other app databases
]


def _find_duckdb_files(zip_file: zipfile.ZipFile) -> list[str]:
    """Find DuckDB database files in snapshot."""
    all_files = zip_file.namelist()
    found: set[str] = set()

    # Look for .duckdb files in known Looker paths
    for prefix in LOOKER_PATHS:
        found.update(
            f for f in all_files if f.startswith(prefix) and f.endswith(".duckdb")
        )

    # Look for any .duckdb with looker in path
    found.update(
        f for f in all_files if "looker" in f.lower() and f.endswith(".duckdb")
    )

    # Also check for offline.duckdb specifically
    found.update(f for f in all_files if f.endswith("offline.duckdb"))

    return list(found)


def _find_csv_files(zip_file: zipfile.ZipFile) -> list[str]:
    """Find CSV files in Looker data directories."""
    all_files = zip_file.namelist()
    found: set[str] = set()

    for prefix in LOOKER_PATHS:
        found.update(
            f for f in all_files if f.startswith(prefix) and f.endswith(".csv")
        )

    # Also look for looker-specific CSV locations
    found.update(f for f in all_files if "looker" in f.lower() and f.endswith(".csv"))

    return list(found)


def _find_sqlite_files(zip_file: zipfile.ZipFile) -> list[str]:
    """Find SQLite files in Looker data directories (legacy fallback)."""
    all_files = zip_file.namelist()
    found: set[str] = set()

    for prefix in LOOKER_PATHS:
        found.update(f for f in all_files if f.startswith(prefix) and f.endswith(".db"))

    found.update(f for f in all_files if "looker" in f.lower() and f.endswith(".db"))

    return list(found)


def _db_alias(path: str) -> str:
    """Generate alias from path (e.g., '.apps_data/looker/offline.duckdb' → 'looker_offline')."""
    return (
        path.replace(".apps_data/", "")
        .replace("/", "_")
        .replace(".duckdb", "")
        .replace(".db", "")
        .strip("_")
    )


def _load_csv_to_duckdb(conn: Any, csv_content: bytes, table_name: str) -> bool:
    """Load CSV content into DuckDB table."""
    try:
        # Parse CSV
        reader = csv.reader(io.StringIO(csv_content.decode("utf-8")))
        raw_headers = next(reader)

        # Normalize column names (remove table prefix like 'orders.order_id' -> 'order_id')
        headers = []
        for h in raw_headers:
            if "." in h:
                headers.append(h.split(".")[-1])
            else:
                headers.append(h)

        rows = list(reader)
        if not rows:
            logger.debug(f"Skipping empty CSV: {table_name}")
            return False

        # Infer column types from data
        col_types = []
        for i, _header in enumerate(headers):
            values = [row[i] for row in rows if i < len(row) and row[i].strip()]
            col_types.append(_infer_type(values))

        # Create table
        columns_def = ", ".join(
            f'"{h}" {t}' for h, t in zip(headers, col_types, strict=False)
        )
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{table_name}" ({columns_def})')

        # Insert data
        placeholders = ", ".join("?" * len(headers))
        for row in rows:
            converted = []
            for val, col_type in zip(row, col_types, strict=False):
                if not val.strip():
                    converted.append(None)
                elif col_type == "BIGINT":
                    converted.append(int(val))
                elif col_type == "DOUBLE":
                    converted.append(float(val))
                else:
                    converted.append(val)
            conn.execute(
                f'INSERT INTO "{table_name}" VALUES ({placeholders})', converted
            )

        logger.debug(f"Loaded {len(rows)} rows into {table_name}")
        return True

    except Exception as e:
        logger.warning(f"Failed to load CSV {table_name}: {e}")
        return False


def _infer_type(values: list[str]) -> str:
    """Infer DuckDB column type from sample values."""
    if not values:
        return "VARCHAR"

    # Try integer
    try:
        for v in values[:100]:
            int(v)
        return "BIGINT"
    except ValueError:
        pass

    # Try float
    try:
        for v in values[:100]:
            float(v)
        return "DOUBLE"
    except ValueError:
        pass

    return "VARCHAR"


def _get_tables(conn: Any, is_duckdb: bool) -> list[str]:
    """Get list of tables in database."""
    try:
        if is_duckdb:
            result = conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' OR table_schema = 'public'"
            ).fetchall()
        else:
            result = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        return [row[0] for row in result]
    except Exception as e:
        logger.warning(f"Could not list tables: {e}")
        return []


def _get_table_schema(
    conn: Any, table_name: str, is_duckdb: bool
) -> list[dict[str, Any]]:
    """Get schema for a table."""
    try:
        if is_duckdb:
            result = conn.execute(f'DESCRIBE "{table_name}"').fetchall()
            return [
                {"name": r[0], "type": r[1], "null": r[2], "key": r[3], "default": r[4]}
                for r in result
            ]
        else:
            cursor = conn.execute(f"PRAGMA table_info({table_name})")
            return [
                {
                    "cid": r[0],
                    "name": r[1],
                    "type": r[2],
                    "notnull": r[3],
                    "default": r[4],
                    "pk": r[5],
                }
                for r in cursor.fetchall()
            ]
    except Exception as e:
        logger.warning(f"Could not get schema for {table_name}: {e}")
        return []


def _get_row_count(conn: Any, table_name: str) -> int:
    """Get row count for a table."""
    try:
        result = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()
        return result[0] if result else 0
    except Exception as e:
        logger.warning(f"Could not count rows in {table_name}: {e}")
        return 0


def cleanup_looker_state(state: dict[str, Any]) -> None:
    """Clean up Looker state resources (connections and temp files)."""
    for alias, db_info in state.get("databases", {}).items():
        conn = db_info.get("connection")
        if conn:
            try:
                conn.close()
                logger.debug(f"Closed connection for database '{alias}'")
            except Exception as e:
                logger.warning(f"Failed to close connection for '{alias}': {e}")

        temp_path = db_info.get("temp_path")
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
                logger.debug(f"Deleted temp file for database '{alias}'")
            except OSError as e:
                logger.warning(f"Failed to delete temp file {temp_path}: {e}")


def _load_json_from_zip(
    zip_file: zipfile.ZipFile, filename: str
) -> dict[str, Any] | list[Any] | None:
    """Load a JSON file from the snapshot if it exists."""
    # Look for the file in known Looker paths
    for prefix in LOOKER_PATHS:
        path = f"{prefix}{filename}"
        if path in zip_file.namelist():
            try:
                content = zip_file.read(path)
                return json.loads(content.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.warning(f"Failed to parse {path}: {e}")
    return None


async def looker_state_helper(
    initial_snapshot_bytes: io.BytesIO,  # noqa: ARG001
    final_snapshot_bytes: io.BytesIO,
    trajectory: AgentTrajectoryOutput,  # noqa: ARG001
) -> dict[str, Any]:
    """Extract Looker state from final snapshot.

    Returns a dict with:
    - databases: Dict of alias -> {connection, path, temp_path, is_duckdb}
    - tables: Dict of alias -> list of table names
    - schemas: Dict of alias -> {table_name: schema}
    - row_counts: Dict of alias -> {table_name: count}
    - looks: Dict of look_id -> look data (from looks.json)
    - dashboards: Dict of dashboard_id -> dashboard data (from dashboards.json)
    - queries: Dict of query_id -> query data (from queries.json)
    - tiles: Dict of dashboard_id -> list of tiles (from tiles.json)
    """
    final_snapshot_bytes.seek(0)
    result: dict[str, Any] = {
        "databases": {},
        "tables": {},
        "schemas": {},
        "row_counts": {},
        # Non-database state (from JSON files)
        "looks": {},
        "dashboards": {},
        "queries": {},
        "tiles": {},
    }

    try:
        with zipfile.ZipFile(final_snapshot_bytes, "r") as final_zip:
            # Priority 1: Load DuckDB files (preferred)
            duckdb_paths = _find_duckdb_files(final_zip)
            for db_path in duckdb_paths:
                if not HAS_DUCKDB:
                    logger.warning(
                        f"Skipping DuckDB file {db_path}: duckdb not installed"
                    )
                    continue

                alias = _db_alias(db_path)
                if alias in result["databases"]:
                    logger.warning(
                        f"Alias collision: '{alias}' exists, skipping {db_path}"
                    )
                    continue

                try:
                    db_bytes = final_zip.read(db_path)
                    temp_file = tempfile.NamedTemporaryFile(
                        suffix=".duckdb", delete=False, mode="wb"
                    )
                    temp_file.write(db_bytes)
                    temp_file.flush()
                    temp_file.close()

                    conn = duckdb_module.connect(temp_file.name, read_only=True)
                    result["databases"][alias] = {
                        "connection": conn,
                        "path": db_path,
                        "temp_path": temp_file.name,
                        "is_duckdb": True,
                    }

                    tables = _get_tables(conn, is_duckdb=True)
                    result["tables"][alias] = tables
                    result["schemas"][alias] = {
                        t: _get_table_schema(conn, t, is_duckdb=True) for t in tables
                    }
                    result["row_counts"][alias] = {
                        t: _get_row_count(conn, t) for t in tables
                    }

                    logger.info(
                        f"Loaded DuckDB '{alias}' from {db_path} ({len(tables)} tables)"
                    )

                except Exception as e:
                    logger.warning(f"Failed to load DuckDB {db_path}: {e}")

            # Priority 2: Load CSV files into in-memory DuckDB
            csv_paths = _find_csv_files(final_zip)
            if csv_paths and HAS_DUCKDB:
                alias = "csv_data"
                if alias not in result["databases"]:
                    try:
                        # Create in-memory DuckDB for CSV data
                        temp_file = tempfile.NamedTemporaryFile(
                            suffix=".duckdb", delete=False, mode="wb"
                        )
                        temp_file.close()
                        conn = duckdb_module.connect(temp_file.name)

                        tables_loaded = []
                        for csv_path in csv_paths:
                            table_name = Path(csv_path).stem
                            csv_content = final_zip.read(csv_path)
                            if _load_csv_to_duckdb(conn, csv_content, table_name):
                                tables_loaded.append(table_name)

                        if tables_loaded:
                            result["databases"][alias] = {
                                "connection": conn,
                                "path": "csv_files",
                                "temp_path": temp_file.name,
                                "is_duckdb": True,
                            }
                            result["tables"][alias] = tables_loaded
                            result["schemas"][alias] = {
                                t: _get_table_schema(conn, t, is_duckdb=True)
                                for t in tables_loaded
                            }
                            result["row_counts"][alias] = {
                                t: _get_row_count(conn, t) for t in tables_loaded
                            }
                            logger.info(
                                f"Loaded {len(tables_loaded)} CSV files into '{alias}'"
                            )
                        else:
                            conn.close()
                            os.unlink(temp_file.name)

                    except Exception as e:
                        logger.warning(f"Failed to create CSV database: {e}")

            # Priority 3: Fall back to SQLite files (legacy)
            if not result["databases"]:
                sqlite_paths = _find_sqlite_files(final_zip)
                for db_path in sqlite_paths:
                    alias = _db_alias(db_path)
                    if alias in result["databases"]:
                        continue

                    try:
                        db_bytes = final_zip.read(db_path)
                        temp_file = tempfile.NamedTemporaryFile(
                            suffix=".db", delete=False, mode="wb"
                        )
                        temp_file.write(db_bytes)
                        temp_file.flush()
                        temp_file.close()

                        conn = sqlite3.connect(temp_file.name)
                        conn.row_factory = sqlite3.Row
                        result["databases"][alias] = {
                            "connection": conn,
                            "path": db_path,
                            "temp_path": temp_file.name,
                            "is_duckdb": False,
                        }

                        tables = _get_tables(conn, is_duckdb=False)
                        result["tables"][alias] = tables
                        result["schemas"][alias] = {
                            t: _get_table_schema(conn, t, is_duckdb=False)
                            for t in tables
                        }
                        result["row_counts"][alias] = {
                            t: _get_row_count(conn, t) for t in tables
                        }

                        logger.info(
                            f"Loaded SQLite '{alias}' from {db_path} ({len(tables)} tables)"
                        )

                    except Exception as e:
                        logger.warning(f"Failed to load SQLite {db_path}: {e}")

            if not result["databases"]:
                logger.warning("No Looker data sources found in snapshot")

            total_tables = sum(len(t) for t in result["tables"].values())
            logger.info(
                f"Loaded Looker state: {len(result['databases'])} databases, {total_tables} tables"
            )

            # Load non-database state from JSON files
            result["looks"] = _load_json_from_zip(final_zip, "looks.json") or {}
            result["dashboards"] = (
                _load_json_from_zip(final_zip, "dashboards.json") or {}
            )
            result["queries"] = _load_json_from_zip(final_zip, "queries.json") or {}
            result["tiles"] = _load_json_from_zip(final_zip, "tiles.json") or {}

            json_counts = (
                f"{len(result['looks'])} looks, "
                f"{len(result['dashboards'])} dashboards, "
                f"{len(result['queries'])} queries, "
                f"{len(result['tiles'])} dashboards with tiles"
            )
            logger.info(f"Loaded Looker JSON state: {json_counts}")

    except zipfile.BadZipFile as e:
        logger.error(f"Invalid snapshot zip: {e}")
    except Exception as e:
        logger.error(f"Error extracting Looker state: {e}")

    final_snapshot_bytes.seek(0)
    return result
