"""Administrative MCP tools for the Greenhouse server.

Provides tools for database management and snapshot export for verifiers.
"""

import json
import os
import uuid
import zipfile
from datetime import UTC, datetime

from auth.permissions import Permission as Perm
from db.models.users import User
from db.session import DATABASE_PATH, IN_MEMORY, AsyncSessionLocal, engine, get_session
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from loguru import logger
from mcp_auth import require_scopes
from mcp_middleware import get_server_config
from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field
from schemas.admin import GreenhouseResetStateInput, GreenhouseResetStateResponse
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

# Path to the schema.sql file
SCHEMA_SQL_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")

TABLE_CLEAR_ORDER = [
    "scorecard_attributes",
    "scorecard_questions",
    "scorecards",
    "application_answers",
    "activities",
    "notes",
    "emails",
    "job_openings",
    "applications",
    "candidate_phone_numbers",
    "candidate_email_addresses",
    "candidate_addresses",
    "candidate_website_addresses",
    "candidate_social_media_addresses",
    "candidate_educations",
    "candidate_employments",
    "candidate_attachments",
    "candidate_tags",
    "tags",
    "candidates",
    "interview_step_default_interviewers",
    "interview_kit_questions",
    "interview_steps",
    "job_stages",
    "hiring_team",
    "job_departments",
    "job_offices",
    "job_post_question_options",
    "job_post_questions",
    "job_posts",
    "prospect_pool_stages",
    "prospect_pools",
    "degrees",
    "disciplines",
    "schools",
    "sources",
    "source_types",
    "rejection_reasons",
    "jobs",
    "user_departments",
    "user_offices",
    "offices",
    "departments",
    "user_emails",
    "users",
]

USER_RELATED_TABLES = {
    "user_emails",
    "user_departments",
    "user_offices",
    "departments",
    "offices",
}

# Default persona users - matches users.json employeeIds
PERSONA_USERS = [
    {"id": 1, "first_name": "Rachel", "last_name": "Recruiter", "email": "recruiter@example.com"},
    {
        "id": 2,
        "first_name": "Chris",
        "last_name": "Coordinator",
        "email": "coordinator@example.com",
    },
    {
        "id": 3,
        "first_name": "Hannah",
        "last_name": "HiringManager",
        "email": "hiring_manager@example.com",
    },
    {"id": 4, "first_name": "Alex", "last_name": "Analyst", "email": "hr_analyst@example.com"},
]


async def _seed_default_users() -> int:
    """Seed default persona users if the users table is empty.

    Returns the number of users created.
    """
    async with AsyncSessionLocal() as session:
        # Check if any users exist
        result = await session.execute(select(User).limit(1))
        if result.scalar_one_or_none() is not None:
            return 0  # Users exist, don't seed

        # Create default persona users
        now = datetime.now(UTC).isoformat()
        for user_data in PERSONA_USERS:
            user = User(
                id=user_data["id"],
                first_name=user_data["first_name"],
                last_name=user_data["last_name"],
                primary_email_address=user_data["email"],
                disabled=False,
                site_admin=False,
                created_at=now,
                updated_at=now,
            )
            session.add(user)
        await session.commit()
        return len(PERSONA_USERS)


async def _truncate_table(connection, table_name: str) -> None:
    """Delete all rows from a table and reset its autoincrement counter."""
    await connection.execute(text(f"DELETE FROM {table_name}"))
    try:
        await connection.execute(
            text("DELETE FROM sqlite_sequence WHERE name = :table_name"),
            {"table_name": table_name},
        )
    except OperationalError as exc:
        if "sqlite_sequence" not in str(exc):
            raise


@require_scopes(Perm.RESET_STATE.value)
async def greenhouse_reset_state(
    params: GreenhouseResetStateInput,
) -> GreenhouseResetStateResponse:
    """Clear user-created data while optionally preserving persona accounts."""
    if not params.confirm:
        raise ToolError("Confirmation required: set confirm=True to reset the database.")

    tables_to_clear: list[str] = []
    for table_name in TABLE_CLEAR_ORDER:
        if table_name == "users" and not params.clear_users:
            continue
        if not params.clear_users and table_name in USER_RELATED_TABLES:
            continue
        tables_to_clear.append(table_name)

    tables_cleared: list[str] = []
    async with engine.begin() as conn:
        for table_name in tables_to_clear:
            await _truncate_table(conn, table_name)
            tables_cleared.append(table_name)

    # If preserving users but table is empty, seed default personas
    users_seeded = 0
    if not params.clear_users:
        users_seeded = await _seed_default_users()

    timestamp = datetime.now(UTC).isoformat()
    logger.info(
        "greenhouse_reset_state executed",
        timestamp=timestamp,
        tables=tables_cleared,
        clear_users=params.clear_users,
        users_seeded=users_seeded,
    )

    message = "Database reset to empty state"
    if users_seeded > 0:
        message += f" (seeded {users_seeded} default persona users)"

    return GreenhouseResetStateResponse(tables_cleared=tables_cleared, message=message)


# =============================================================================
# Snapshot Export Tool
# =============================================================================


class ExportSnapshotInput(BaseModel):
    """Input for snapshot export tool."""

    include_schema: bool = Field(True, description="Include schema DDL in export")


class ExportSnapshotOutput(BaseModel):
    """Output from snapshot export tool."""

    snapshot_path: str = Field(..., description="Path to the exported ZIP file")
    tables_exported: list[str] = Field(..., description="List of exported table names")
    record_counts: dict[str, int] = Field(..., description="Record count per table")
    timestamp: str = Field(..., description="ISO 8601 timestamp of export")


@require_scopes(Perm.EXPORT_SNAPSHOT.value)
async def greenhouse_export_snapshot(input: ExportSnapshotInput) -> ExportSnapshotOutput:
    """Export the current database state to a ZIP file for verifier analysis."""
    if IN_MEMORY:
        raise ToolError(
            "Cannot export snapshot from in-memory database. "
            "Set GREENHOUSE_DB_PATH to a file path for persistence."
        )

    timestamp = datetime.now(UTC)
    timestamp_str = timestamp.strftime("%Y%m%d_%H%M%S")
    # Add UUID suffix to prevent filename collisions
    unique_suffix = uuid.uuid4().hex[:8]
    timestamp_iso = timestamp.isoformat()

    # Generate unique ZIP path
    zip_path = f"/tmp/greenhouse_snapshot_{timestamp_str}_{unique_suffix}.zip"

    # Get table names and record counts, then create ZIP within the same transaction
    # to ensure metadata matches the exported database state
    tables: list[str] = []
    record_counts: dict[str, int] = {}

    async with get_session() as session:
        # Get all table names
        result = await session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        )
        tables = [row[0] for row in result.fetchall()]

        # Get record counts for each table
        for table in tables:
            count_result = await session.execute(text(f"SELECT COUNT(*) FROM {table}"))  # noqa: S608
            record_counts[table] = count_result.scalar() or 0

        # Create ZIP file within the session context to minimize race condition window
        # Note: For a mock MCP server used in testing/training, the brief window between
        # reading counts and copying the file is acceptable. For production use,
        # SQLite's backup API would provide true atomicity.
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Add database file
            zf.write(DATABASE_PATH, "db/greenhouse.db")

            # Add schema if requested
            if input.include_schema and os.path.exists(SCHEMA_SQL_PATH):
                zf.write(SCHEMA_SQL_PATH, "schema/schema.sql")

            # Add metadata
            server_config = get_server_config()
            metadata = {
                "export_timestamp": timestamp_iso,
                "server_version": server_config.version if server_config else "unknown",
                "tables": tables,
                "record_counts": record_counts,
            }
            zf.writestr("metadata.json", json.dumps(metadata, indent=2))

    return ExportSnapshotOutput(
        snapshot_path=zip_path,
        tables_exported=tables,
        record_counts=record_counts,
        timestamp=timestamp_iso,
    )


def register_admin_tools(mcp: FastMCP) -> None:
    """Register admin tools with the MCP server."""
    mcp.tool()(greenhouse_reset_state)
    mcp.tool()(greenhouse_export_snapshot)
