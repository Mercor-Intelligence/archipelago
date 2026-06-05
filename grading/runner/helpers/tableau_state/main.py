"""Tableau State Helper - Extracts Tableau data from snapshot database.

Data Flow:
---------
1. Tableau stores data in SQLite at {APPS_DATA_ROOT}/tableau/data.db
2. Snapshot includes the database at .apps_data/tableau/data.db
3. This helper extracts the db, queries tables, returns normalized data

Database Schema (SQLite):
------------------------
Key tables and their primary fields:
- users: id, name, email, site_role, site_id, created_at, updated_at
- workbooks: id, name, site_id, project_id, created_at, updated_at
- sites: id, name, content_url, created_at, updated_at
- projects: id, name, site_id, description, created_at, updated_at
- datasources: id, name, site_id, project_id, created_at, updated_at
- views: id, name, workbook_id, created_at, updated_at
"""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import zipfile
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from runner.models import AgentTrajectoryOutput

TABLEAU_DB_PATHS = [
    ".apps_data/tableau/data.db",
    "tableau/data.db",
    "tableau_data.db",
    # Note: Do NOT include generic "data.db" - it would match other apps' databases
]

# Schema definitions matching mercor-tableau/mcp_servers/tableau/db/models.py
TABLE_SCHEMAS: dict[str, list[str]] = {
    "sites": [
        "id",
        "name",
        "content_url",
        "created_at",
        "updated_at",
    ],
    "users": [
        "id",
        "site_id",
        "name",
        "email",
        "site_role",
        "created_at",
        "updated_at",
    ],
    "groups": [
        "id",
        "name",
        "description",
        "created_at",
        "updated_at",
    ],
    "group_users": [
        "id",
        "group_id",
        "user_id",
        "created_at",
    ],
    "projects": [
        "id",
        "site_id",
        "name",
        "description",
        "parent_project_id",
        "owner_id",
        "created_at",
        "updated_at",
    ],
    "workbooks": [
        "id",
        "site_id",
        "name",
        "project_id",
        "owner_id",
        "file_reference",
        "description",
        "created_at",
        "updated_at",
    ],
    "datasources": [
        "id",
        "site_id",
        "name",
        "project_id",
        "owner_id",
        "connection_type",
        "description",
        "created_at",
        "updated_at",
    ],
    "workbook_datasources": [
        "id",
        "workbook_id",
        "datasource_id",
        "created_at",
    ],
    "views": [
        "id",
        "site_id",
        "workbook_id",
        "name",
        "content_url",
        "sheet_type",
        "sample_data_json",
        "preview_image_path",
        "created_at",
        "updated_at",
    ],
    "permissions": [
        "id",
        "resource_type",
        "resource_id",
        "grantee_type",
        "grantee_id",
        "capability",
        "mode",
        "created_at",
    ],
}


def _find_tableau_db(zip_file: zipfile.ZipFile) -> str | None:
    """Find Tableau database in snapshot."""
    all_files = zip_file.namelist()

    for path in TABLEAU_DB_PATHS:
        if path in all_files:
            return path
        matching = [f for f in all_files if f.endswith(path)]
        if matching:
            return matching[0]

    # Fallback: any .db file with tableau in path
    tableau_dbs = [f for f in all_files if "tableau" in f.lower() and f.endswith(".db")]
    return tableau_dbs[0] if tableau_dbs else None


def _extract_table(
    conn: sqlite3.Connection, table_name: str, fields: list[str]
) -> list[dict[str, Any]]:
    """Extract rows from a table, selecting only specified fields."""
    try:
        cursor = conn.execute(f"SELECT * FROM {table_name}")  # noqa: S608
        columns = [desc[0] for desc in cursor.description]
        return [
            {
                field: dict(zip(columns, row, strict=False)).get(field)
                for field in fields
            }
            for row in cursor.fetchall()
        ]
    except sqlite3.OperationalError as e:
        logger.warning(f"Could not extract {table_name}: {e}")
        return []


async def tableau_state_helper(
    initial_snapshot_bytes: io.BytesIO,  # noqa: ARG001
    final_snapshot_bytes: io.BytesIO,
    trajectory: AgentTrajectoryOutput,  # noqa: ARG001
) -> dict[str, list[dict[str, Any]]]:
    """Extract Tableau state from final snapshot database."""
    final_snapshot_bytes.seek(0)
    result: dict[str, list[dict[str, Any]]] = {name: [] for name in TABLE_SCHEMAS}

    try:
        with zipfile.ZipFile(final_snapshot_bytes, "r") as final_zip:
            db_path = _find_tableau_db(final_zip)

            if not db_path:
                logger.warning("No Tableau database found in snapshot")
                final_snapshot_bytes.seek(0)
                return result

            db_bytes = final_zip.read(db_path)

            # Write to temp file (SQLite needs file path)
            temp_file = tempfile.NamedTemporaryFile(
                suffix=".db", delete=False, mode="wb"
            )
            temp_file_path = temp_file.name

            try:
                temp_file.write(db_bytes)
                temp_file.flush()
                temp_file.close()

                conn = sqlite3.connect(temp_file_path)
                conn.row_factory = sqlite3.Row

                try:
                    for table_name, fields in TABLE_SCHEMAS.items():
                        result[table_name] = _extract_table(conn, table_name, fields)

                    logger.info(
                        "Loaded Tableau state: "
                        + ", ".join(f"{len(v)} {k}" for k, v in result.items())
                    )
                finally:
                    conn.close()
            finally:
                # Clean up temp file
                try:
                    os.unlink(temp_file_path)
                except OSError as e:
                    logger.warning(f"Failed to delete temp file {temp_file_path}: {e}")

    except zipfile.BadZipFile as e:
        logger.error(f"Invalid snapshot zip: {e}")
    except Exception as e:
        logger.error(f"Error extracting Tableau state: {e}")

    final_snapshot_bytes.seek(0)
    return result
