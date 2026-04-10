"""Greenhouse ATS Field Check Verifier.

Checks Greenhouse database fields via dropdown selection + expected value.
"""

import os
import sqlite3
import tempfile
import zipfile
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

# Greenhouse-specific database paths (avoid picking up other services)
DATABASE_PATHS = [
    ".apps_data/greenhouse/data.db",
    "mcp_servers/greenhouse/greenhouse.db",
    "mcp_servers/greenhouse/data.db",
    "greenhouse/greenhouse.db",
    "greenhouse.db",
]

# Field -> SQL query mappings
FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    # Candidate checks
    "Candidate Count": {
        "query": "SELECT COUNT(*) FROM candidates",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of candidates",
    },
    "Candidate Exists by Name": {
        "query": """
            SELECT COUNT(*) FROM candidates
            WHERE LOWER(COALESCE(first_name, '') || ' ' || COALESCE(last_name, ''))
            LIKE LOWER('%' || :candidate_name || '%')
        """,
        "required_params": ["candidate_name"],
        "result_type": "boolean",
        "description": "Check if candidate exists by full or partial name",
    },
    "Candidate Exists by Email": {
        "query": "SELECT COUNT(*) FROM candidate_email_addresses WHERE LOWER(value) = LOWER(:email)",
        "required_params": ["email"],
        "result_type": "boolean",
        "description": "Check if candidate exists by email address",
    },
    "Candidate First Name": {
        "query": "SELECT first_name FROM candidates WHERE id = :candidate_id",
        "required_params": ["candidate_id"],
        "result_type": "string",
        "description": "Get candidate's first name by ID",
    },
    "Candidate Last Name": {
        "query": "SELECT last_name FROM candidates WHERE id = :candidate_id",
        "required_params": ["candidate_id"],
        "result_type": "string",
        "description": "Get candidate's last name by ID",
    },
    "Candidate Company": {
        "query": "SELECT company FROM candidates WHERE id = :candidate_id",
        "required_params": ["candidate_id"],
        "result_type": "string",
        "description": "Get candidate's company by ID",
    },
    "Candidate Title": {
        "query": "SELECT title FROM candidates WHERE id = :candidate_id",
        "required_params": ["candidate_id"],
        "result_type": "string",
        "description": "Get candidate's title by ID",
    },
    "Candidate Tag Count": {
        "query": "SELECT COUNT(*) FROM candidate_tags WHERE candidate_id = :candidate_id",
        "required_params": ["candidate_id"],
        "result_type": "integer",
        "description": "Count number of tags for candidate",
    },
    "Candidate Has Tag": {
        "query": """
            SELECT COUNT(*) FROM candidate_tags ct
            JOIN tags t ON ct.tag_id = t.id
            WHERE ct.candidate_id = :candidate_id AND LOWER(t.name) = LOWER(:tag_name)
        """,
        "required_params": ["candidate_id", "tag_name"],
        "result_type": "boolean",
        "description": "Check if candidate has specific tag",
    },
    # Job checks
    "Job Count": {
        "query": "SELECT COUNT(*) FROM jobs",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of jobs",
    },
    "Job Count by Status": {
        "query": "SELECT COUNT(*) FROM jobs WHERE status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count jobs with specific status",
    },
    "Open Job Count": {
        "query": "SELECT COUNT(*) FROM jobs WHERE status = 'open'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count jobs with open status",
    },
    "Job Exists by Name": {
        "query": "SELECT COUNT(*) FROM jobs WHERE LOWER(name) LIKE LOWER('%' || :job_name || '%')",
        "required_params": ["job_name"],
        "result_type": "boolean",
        "description": "Check if job exists by full or partial name",
    },
    "Job Name by ID": {
        "query": "SELECT name FROM jobs WHERE id = :job_id",
        "required_params": ["job_id"],
        "result_type": "string",
        "description": "Get job name by ID",
    },
    "Job Status": {
        "query": "SELECT status FROM jobs WHERE id = :job_id",
        "required_params": ["job_id"],
        "result_type": "string",
        "description": "Get job status by ID",
    },
    "Job Requisition ID": {
        "query": "SELECT requisition_id FROM jobs WHERE id = :job_id",
        "required_params": ["job_id"],
        "result_type": "string",
        "description": "Get job requisition ID by job ID",
    },
    "Job Stage Count": {
        "query": "SELECT COUNT(*) FROM job_stages WHERE job_id = :job_id",
        "required_params": ["job_id"],
        "result_type": "integer",
        "description": "Count number of stages for job",
    },
    "Job Opening Count": {
        "query": "SELECT COUNT(*) FROM job_openings WHERE job_id = :job_id",
        "required_params": ["job_id"],
        "result_type": "integer",
        "description": "Count number of openings for job",
    },
    "Job Open Openings": {
        "query": "SELECT COUNT(*) FROM job_openings WHERE job_id = :job_id AND status = 'open'",
        "required_params": ["job_id"],
        "result_type": "integer",
        "description": "Count number of open openings for job",
    },
    # Application checks
    "Application Count": {
        "query": "SELECT COUNT(*) FROM applications",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of applications",
    },
    "Application Count by Status": {
        "query": "SELECT COUNT(*) FROM applications WHERE status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count applications with specific status",
    },
    "Active Application Count": {
        "query": "SELECT COUNT(*) FROM applications WHERE status = 'active'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count applications with active status",
    },
    "Rejected Application Count": {
        "query": "SELECT COUNT(*) FROM applications WHERE status = 'rejected'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count applications with rejected status",
    },
    "Hired Application Count": {
        "query": "SELECT COUNT(*) FROM applications WHERE status = 'hired'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count applications with hired status",
    },
    "Application Status": {
        "query": "SELECT status FROM applications WHERE id = :application_id",
        "required_params": ["application_id"],
        "result_type": "string",
        "description": "Get application status by ID",
    },
    "Application Current Stage": {
        "query": """
            SELECT js.name FROM applications a
            JOIN job_stages js ON a.current_stage_id = js.id
            WHERE a.id = :application_id
        """,
        "required_params": ["application_id"],
        "result_type": "string",
        "description": "Get application's current stage name by ID",
    },
    "Application Stage ID": {
        "query": "SELECT current_stage_id FROM applications WHERE id = :application_id",
        "required_params": ["application_id"],
        "result_type": "integer",
        "description": "Get application's current stage ID by ID",
    },
    "Applications for Job": {
        "query": "SELECT COUNT(*) FROM applications WHERE job_id = :job_id",
        "required_params": ["job_id"],
        "result_type": "integer",
        "description": "Count applications for specific job",
    },
    "Active Applications for Job": {
        "query": "SELECT COUNT(*) FROM applications WHERE job_id = :job_id AND status = 'active'",
        "required_params": ["job_id"],
        "result_type": "integer",
        "description": "Count active applications for specific job",
    },
    "Applications for Candidate": {
        "query": "SELECT COUNT(*) FROM applications WHERE candidate_id = :candidate_id",
        "required_params": ["candidate_id"],
        "result_type": "integer",
        "description": "Count applications for specific candidate",
    },
    "Application Rejection Reason": {
        "query": """
            SELECT rr.name FROM applications a
            JOIN rejection_reasons rr ON a.rejection_reason_id = rr.id
            WHERE a.id = :application_id
        """,
        "required_params": ["application_id"],
        "result_type": "string",
        "description": "Get application's rejection reason by ID",
    },
    # Scorecard checks
    "Scorecard Count": {
        "query": "SELECT COUNT(*) FROM scorecards",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of scorecards",
    },
    "Scorecards for Application": {
        "query": "SELECT COUNT(*) FROM scorecards WHERE application_id = :application_id",
        "required_params": ["application_id"],
        "result_type": "integer",
        "description": "Count scorecards for specific application",
    },
    "Scorecard Recommendation": {
        "query": "SELECT overall_recommendation FROM scorecards WHERE id = :scorecard_id",
        "required_params": ["scorecard_id"],
        "result_type": "string",
        "description": "Get scorecard's overall recommendation by ID",
    },
    "Scorecards with Recommendation": {
        "query": """
            SELECT COUNT(*) FROM scorecards
            WHERE application_id = :application_id AND overall_recommendation = :recommendation
        """,
        "required_params": ["application_id", "recommendation"],
        "result_type": "integer",
        "description": "Count scorecards for application with specific recommendation",
    },
    # User checks
    "User Count": {
        "query": "SELECT COUNT(*) FROM users",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of users",
    },
    "Active User Count": {
        "query": "SELECT COUNT(*) FROM users WHERE disabled = 0",
        "required_params": [],
        "result_type": "integer",
        "description": "Count non-disabled users",
    },
    "User Exists by Email": {
        "query": "SELECT COUNT(*) FROM users WHERE LOWER(primary_email_address) = LOWER(:email)",
        "required_params": ["email"],
        "result_type": "boolean",
        "description": "Check if user exists by email address",
    },
    "User Name by ID": {
        "query": "SELECT COALESCE(first_name, '') || ' ' || COALESCE(last_name, '') FROM users WHERE id = :user_id",
        "required_params": ["user_id"],
        "result_type": "string",
        "description": "Get user's full name by ID",
    },
    # Department & Office checks
    "Department Count": {
        "query": "SELECT COUNT(*) FROM departments",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of departments",
    },
    "Department Exists": {
        "query": "SELECT COUNT(*) FROM departments WHERE LOWER(name) = LOWER(:department_name)",
        "required_params": ["department_name"],
        "result_type": "boolean",
        "description": "Check if department exists by name",
    },
    "Office Count": {
        "query": "SELECT COUNT(*) FROM offices",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of offices",
    },
    "Office Exists": {
        "query": "SELECT COUNT(*) FROM offices WHERE LOWER(name) = LOWER(:office_name)",
        "required_params": ["office_name"],
        "result_type": "boolean",
        "description": "Check if office exists by name",
    },
    # Note & Activity checks
    "Note Count for Candidate": {
        "query": "SELECT COUNT(*) FROM notes WHERE candidate_id = :candidate_id",
        "required_params": ["candidate_id"],
        "result_type": "integer",
        "description": "Count notes for specific candidate",
    },
    "Activity Count for Candidate": {
        "query": "SELECT COUNT(*) FROM activities WHERE candidate_id = :candidate_id",
        "required_params": ["candidate_id"],
        "result_type": "integer",
        "description": "Count activities for specific candidate",
    },
    "Note Exists with Text": {
        "query": """
            SELECT COUNT(*) FROM notes
            WHERE candidate_id = :candidate_id AND LOWER(body) LIKE LOWER('%' || :text || '%')
        """,
        "required_params": ["candidate_id", "text"],
        "result_type": "boolean",
        "description": "Check if note exists for candidate containing specific text",
    },
    # Tag checks
    "Tag Count": {
        "query": "SELECT COUNT(*) FROM tags",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of tags",
    },
    "Tag Exists": {
        "query": "SELECT COUNT(*) FROM tags WHERE LOWER(name) = LOWER(:tag_name)",
        "required_params": ["tag_name"],
        "result_type": "boolean",
        "description": "Check if tag exists by name",
    },
    # Source checks
    "Source Count": {
        "query": "SELECT COUNT(*) FROM sources",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of sources",
    },
    "Source Exists": {
        "query": "SELECT COUNT(*) FROM sources WHERE LOWER(name) = LOWER(:source_name)",
        "required_params": ["source_name"],
        "result_type": "boolean",
        "description": "Check if source exists by name",
    },
}


def _extract_db_from_snapshot(
    snapshot_bytes,
) -> tuple[sqlite3.Connection | None, str | None]:
    """Extract Greenhouse database from snapshot zip.

    Returns (connection, temp_file_path). Caller must clean up temp file.
    """
    snapshot_bytes.seek(0)
    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            namelist = zf.namelist()
            for db_path in DATABASE_PATHS:
                for prefix in ["", "filesystem/"]:
                    full_path = prefix + db_path
                    if full_path not in namelist:
                        continue

                    db_bytes = zf.read(full_path)
                    tmp_path = None
                    try:
                        with tempfile.NamedTemporaryFile(
                            suffix=".db", delete=False
                        ) as tmp:
                            tmp_path = (
                                tmp.name
                            )  # Assign before write for cleanup on failure
                            tmp.write(db_bytes)
                        conn = sqlite3.connect(tmp_path)
                        logger.info(f"Found Greenhouse database at: {full_path}")
                        return conn, tmp_path
                    except (OSError, sqlite3.Error) as e:
                        if tmp_path:
                            try:
                                os.unlink(tmp_path)
                            except OSError:
                                pass
                        logger.warning(f"Failed to connect to {full_path}: {e}")
    except zipfile.BadZipFile:
        logger.error("Invalid zip file in snapshot")

    return None, None


def _compare_values(result: Any, expected: str, result_type: str) -> tuple[bool, str]:
    """Compare actual result to expected value based on type. Returns (matches, message)."""
    if result is None:
        return False, "No result found"

    if result_type == "boolean":
        actual = result > 0
        expected_bool = str(expected).lower() in ("true", "1", "yes")
        matches = actual == expected_bool
        return matches, f"{'Yes' if actual else 'No'}"

    if result_type == "integer":
        try:
            matches = int(result) == int(expected)
            return matches, str(result)
        except (ValueError, TypeError) as e:
            raise ValueError(
                f"Invalid integer comparison (result={result}, expected={expected}): {e}"
            ) from e

    # String comparison (case-insensitive)
    matches = str(result).lower().strip() == str(expected).lower().strip()
    return matches, str(result)


async def greenhouse_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify a Greenhouse ATS field against an expected value."""
    vid = input.verifier.verifier_id
    vver = input.verifier.verifier_version
    values = input.verifier.verifier_values

    # Validate required inputs
    field_to_check_raw = values.get("field_to_check")
    if not field_to_check_raw:
        raise ValueError("field_to_check is required")

    # Strip arrow notation (e.g., "Candidate Count by Status → Status" -> "Candidate Count by Status")
    field_to_check = field_to_check_raw.split(" → ")[0].strip()

    expected_value = values.get("expected_value")
    if expected_value is None:
        raise ValueError("expected_value is required")

    field_config = FIELD_MAPPINGS.get(field_to_check)
    if not field_config:
        raise ValueError(f"Unknown field_to_check: {field_to_check}")

    # Gather query parameters
    params = {}
    for param in field_config["required_params"]:
        val = values.get(param)
        if val is None:
            raise ValueError(f"Missing required parameter: {param}")
        params[param] = val

    # Get database connection (prefer helper, fallback to snapshot extraction)
    conn = None
    temp_path: str | None = None
    owns_conn = False

    if input.helper_results:
        dbs = input.helper_results.get(HelperIds.SNAPSHOT_DBS, {})
        greenhouse_dbs = [(k, v) for k, v in dbs.items() if "greenhouse" in k.lower()]
        if len(greenhouse_dbs) == 1:
            conn = greenhouse_dbs[0][1].get("connection")
        elif len(greenhouse_dbs) > 1:
            raise ValueError(
                f"Multiple Greenhouse databases found: {[k for k, _ in greenhouse_dbs]}"
            )

    if not conn:
        conn, temp_path = _extract_db_from_snapshot(input.final_snapshot_bytes)
        if conn:
            owns_conn = True

    if not conn:
        raise ValueError("Greenhouse database not found in snapshot")

    # Execute query
    try:
        row = conn.cursor().execute(field_config["query"], params).fetchone()
        result = row[0] if row else None
    except Exception as e:
        raise ValueError(f"Query failed: {e}") from e
    finally:
        if owns_conn:
            conn.close()
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    # Compare result
    matches, actual_str = _compare_values(
        result, expected_value, field_config["result_type"]
    )
    check_mark = "✓" if matches else f"✗ expected {expected_value}"

    return VerifierResult(
        verifier_id=vid,
        verifier_version=vver,
        score=1.0 if matches else 0.0,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "field_checked": field_to_check,
            "actual_value": actual_str,
            "expected_value": str(expected_value),
            "matches": matches,
        },
        message=f"{field_to_check}: {actual_str} {check_mark}",
    )
