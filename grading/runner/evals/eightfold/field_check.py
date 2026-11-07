"""Eightfold Field Check Verifier - Flexible dropdown + value interface.

This verifier provides a simple dropdown interface for domain experts to check
any Eightfold ATS field without writing SQL. The dropdown selection maps to
pre-defined SQL queries that have been verified against the actual Eightfold schema.
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

# All queries are schema-accurate for Eightfold ATS
FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    # =========================================================================
    # PROFILE CHECKS
    # =========================================================================
    "Profile Count": {
        "query": "SELECT COUNT(*) FROM profiles",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of profiles",
    },
    "Profile Count by Status": {
        "query": "SELECT COUNT(*) FROM profiles WHERE status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count profiles by status (active, archived, etc.)",
    },
    "Active Profile Count": {
        "query": "SELECT COUNT(*) FROM profiles WHERE is_archived = 0",
        "required_params": [],
        "result_type": "integer",
        "description": "Count active (non-archived) profiles",
    },
    "Profile Exists by Name": {
        "query": """
            SELECT COUNT(*) FROM profiles
            WHERE LOWER(COALESCE(first_name, '') || ' ' || COALESCE(last_name, '')) LIKE LOWER('%' || :name || '%')
        """,
        "required_params": ["name"],
        "result_type": "boolean",
        "description": "Verify profile exists by name (fuzzy match)",
    },
    "Profile Exists by Email": {
        "query": "SELECT COUNT(*) FROM profiles WHERE LOWER(email) = LOWER(:email)",
        "required_params": ["email"],
        "result_type": "boolean",
        "description": "Verify profile exists by email address",
    },
    "Profile First Name": {
        "query": "SELECT first_name FROM profiles WHERE id = :profile_id",
        "required_params": ["profile_id"],
        "result_type": "string",
        "description": "Get profile's first name by ID",
    },
    "Profile Last Name": {
        "query": "SELECT last_name FROM profiles WHERE id = :profile_id",
        "required_params": ["profile_id"],
        "result_type": "string",
        "description": "Get profile's last name by ID",
    },
    "Profile Email": {
        "query": "SELECT email FROM profiles WHERE id = :profile_id",
        "required_params": ["profile_id"],
        "result_type": "string",
        "description": "Get profile's email by ID",
    },
    "Profile Title": {
        "query": "SELECT title FROM profiles WHERE id = :profile_id",
        "required_params": ["profile_id"],
        "result_type": "string",
        "description": "Get profile's current title",
    },
    "Profile Company": {
        "query": "SELECT company FROM profiles WHERE id = :profile_id",
        "required_params": ["profile_id"],
        "result_type": "string",
        "description": "Get profile's current company",
    },
    "Profile Status": {
        "query": "SELECT status FROM profiles WHERE id = :profile_id",
        "required_params": ["profile_id"],
        "result_type": "string",
        "description": "Get profile's status",
    },
    "Profile Is Archived": {
        "query": "SELECT is_archived FROM profiles WHERE id = :profile_id",
        "required_params": ["profile_id"],
        "result_type": "boolean",
        "description": "Check if profile is archived",
    },
    # =========================================================================
    # POSITION CHECKS
    # =========================================================================
    "Position Count": {
        "query": "SELECT COUNT(*) FROM positions",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of positions",
    },
    "Position Count by Status": {
        "query": "SELECT COUNT(*) FROM positions WHERE status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count positions by status (open, closed, on-hold)",
    },
    "Open Position Count": {
        "query": "SELECT COUNT(*) FROM positions WHERE status = 'open' AND is_archived = 0",
        "required_params": [],
        "result_type": "integer",
        "description": "Count open (non-archived) positions",
    },
    "Position Exists by Title": {
        "query": """
            SELECT COUNT(*) FROM positions
            WHERE LOWER(title) LIKE LOWER('%' || :title || '%')
        """,
        "required_params": ["title"],
        "result_type": "boolean",
        "description": "Verify position exists by title (fuzzy match)",
    },
    "Position Title": {
        "query": "SELECT title FROM positions WHERE id = :position_id",
        "required_params": ["position_id"],
        "result_type": "string",
        "description": "Get position title by ID",
    },
    "Position Department": {
        "query": "SELECT department FROM positions WHERE id = :position_id",
        "required_params": ["position_id"],
        "result_type": "string",
        "description": "Get position department by ID",
    },
    "Position Status": {
        "query": "SELECT status FROM positions WHERE id = :position_id",
        "required_params": ["position_id"],
        "result_type": "string",
        "description": "Get position status",
    },
    "Position Hiring Manager ID": {
        "query": "SELECT hiring_manager_id FROM positions WHERE id = :position_id",
        "required_params": ["position_id"],
        "result_type": "integer",
        "description": "Get position's hiring manager ID",
    },
    "Position Recruiter ID": {
        "query": "SELECT recruiter_id FROM positions WHERE id = :position_id",
        "required_params": ["position_id"],
        "result_type": "integer",
        "description": "Get position's recruiter ID",
    },
    # =========================================================================
    # APPLICATION CHECKS
    # =========================================================================
    "Application Count": {
        "query": "SELECT COUNT(*) FROM applications",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of applications",
    },
    "Application Count by Stage": {
        "query": "SELECT COUNT(*) FROM applications WHERE stage = :stage",
        "required_params": ["stage"],
        "result_type": "integer",
        "description": "Count applications by stage (Applied, Screen, Interview, etc.)",
    },
    "Active Application Count": {
        "query": "SELECT COUNT(*) FROM applications WHERE is_active = 1",
        "required_params": [],
        "result_type": "integer",
        "description": "Count active applications",
    },
    "Applications for Profile": {
        "query": "SELECT COUNT(*) FROM applications WHERE profile_id = :profile_id",
        "required_params": ["profile_id"],
        "result_type": "integer",
        "description": "Count applications for a specific profile",
    },
    "Applications for Position": {
        "query": "SELECT COUNT(*) FROM applications WHERE position_id = :position_id",
        "required_params": ["position_id"],
        "result_type": "integer",
        "description": "Count applications for a specific position",
    },
    "Application Exists": {
        "query": "SELECT COUNT(*) FROM applications WHERE profile_id = :profile_id AND position_id = :position_id",
        "required_params": ["profile_id", "position_id"],
        "result_type": "boolean",
        "description": "Verify application exists for profile-position pair",
    },
    "Application Stage": {
        "query": "SELECT stage FROM applications WHERE id = :application_id",
        "required_params": ["application_id"],
        "result_type": "string",
        "description": "Get application stage by ID",
    },
    "Application Is Active": {
        "query": "SELECT is_active FROM applications WHERE id = :application_id",
        "required_params": ["application_id"],
        "result_type": "boolean",
        "description": "Check if application is active",
    },
    "Application Source": {
        "query": "SELECT source FROM applications WHERE id = :application_id",
        "required_params": ["application_id"],
        "result_type": "string",
        "description": "Get application source",
    },
    # =========================================================================
    # USER CHECKS
    # =========================================================================
    "User Count": {
        "query": "SELECT COUNT(*) FROM users",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of users",
    },
    "Active User Count": {
        "query": "SELECT COUNT(*) FROM users WHERE is_active = 1",
        "required_params": [],
        "result_type": "integer",
        "description": "Count active users",
    },
    "User Count by Persona": {
        "query": "SELECT COUNT(*) FROM users WHERE persona = :persona",
        "required_params": ["persona"],
        "result_type": "integer",
        "description": "Count users by persona (recruiter, coordinator, hiring_manager, admin)",
    },
    "User Exists by Email": {
        "query": "SELECT COUNT(*) FROM users WHERE LOWER(email) = LOWER(:email)",
        "required_params": ["email"],
        "result_type": "boolean",
        "description": "Verify user exists by email address",
    },
    "User Email": {
        "query": "SELECT email FROM users WHERE id = :user_id",
        "required_params": ["user_id"],
        "result_type": "string",
        "description": "Get user's email by ID",
    },
    "User Persona": {
        "query": "SELECT persona FROM users WHERE id = :user_id",
        "required_params": ["user_id"],
        "result_type": "string",
        "description": "Get user's persona by ID",
    },
    "User First Name": {
        "query": "SELECT first_name FROM users WHERE id = :user_id",
        "required_params": ["user_id"],
        "result_type": "string",
        "description": "Get user's first name by ID",
    },
    "User Last Name": {
        "query": "SELECT last_name FROM users WHERE id = :user_id",
        "required_params": ["user_id"],
        "result_type": "string",
        "description": "Get user's last name by ID",
    },
    # =========================================================================
    # TAG CHECKS
    # =========================================================================
    "Tag Count": {
        "query": "SELECT COUNT(*) FROM profile_tags",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of tags",
    },
    "Tag Count for Profile": {
        "query": "SELECT COUNT(*) FROM profile_tags WHERE entity_type = 'profile' AND entity_id = :profile_id",
        "required_params": ["profile_id"],
        "result_type": "integer",
        "description": "Count tags for a specific profile",
    },
    "Tag Count for Position": {
        "query": "SELECT COUNT(*) FROM profile_tags WHERE entity_type = 'position' AND entity_id = :position_id",
        "required_params": ["position_id"],
        "result_type": "integer",
        "description": "Count tags for a specific position",
    },
    "Tag Exists": {
        "query": """
            SELECT COUNT(*) FROM profile_tags
            WHERE entity_type = :entity_type AND entity_id = :entity_id AND LOWER(name) = LOWER(:tag_name)
        """,
        "required_params": ["entity_type", "entity_id", "tag_name"],
        "result_type": "boolean",
        "description": "Check if tag exists for entity",
    },
    # =========================================================================
    # NOTE CHECKS
    # =========================================================================
    "Note Count for Profile": {
        "query": "SELECT COUNT(*) FROM profile_notes WHERE profile_id = :profile_id",
        "required_params": ["profile_id"],
        "result_type": "integer",
        "description": "Count notes for a specific profile",
    },
    "Note Count for Application": {
        "query": "SELECT COUNT(*) FROM profile_notes WHERE application_id = :application_id",
        "required_params": ["application_id"],
        "result_type": "integer",
        "description": "Count notes for a specific application",
    },
    "Note Exists with Text": {
        "query": """
            SELECT COUNT(*) FROM profile_notes
            WHERE ((:profile_id IS NOT NULL AND profile_id = :profile_id) OR (:application_id IS NOT NULL AND application_id = :application_id))
            AND LOWER(body) LIKE LOWER('%' || :text || '%')
        """,
        "required_params": ["text"],
        "optional_params": ["profile_id", "application_id"],
        "result_type": "boolean",
        "description": "Check if note exists with specific text (requires profile_id or application_id)",
    },
    # =========================================================================
    # FEEDBACK CHECKS
    # =========================================================================
    "Feedback Count for Profile": {
        "query": "SELECT COUNT(*) FROM profile_feedbacks WHERE profile_id = :profile_id",
        "required_params": ["profile_id"],
        "result_type": "integer",
        "description": "Count feedback entries for a specific profile",
    },
    "Feedback Count for Application": {
        "query": "SELECT COUNT(*) FROM profile_feedbacks WHERE application_id = :application_id",
        "required_params": ["application_id"],
        "result_type": "integer",
        "description": "Count feedback entries for a specific application",
    },
    "Feedback Rating": {
        "query": "SELECT rating FROM profile_feedbacks WHERE id = :feedback_id",
        "required_params": ["feedback_id"],
        "result_type": "string",
        "description": "Get feedback rating by ID",
    },
    # =========================================================================
    # ATS CANDIDATE CHECKS
    # =========================================================================
    "ATS Candidate Count": {
        "query": "SELECT COUNT(*) FROM ats_candidates",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of ATS candidates",
    },
    "ATS Candidate Exists by External ID": {
        "query": "SELECT COUNT(*) FROM ats_candidates WHERE external_id = :external_id",
        "required_params": ["external_id"],
        "result_type": "boolean",
        "description": "Verify ATS candidate exists by external ID",
    },
    "ATS Candidate Profile Link": {
        "query": "SELECT profile_id FROM ats_candidates WHERE id = :ats_candidate_id",
        "required_params": ["ats_candidate_id"],
        "result_type": "integer",
        "description": "Get linked profile ID for ATS candidate",
    },
    # =========================================================================
    # ATS POSITION CHECKS
    # =========================================================================
    "ATS Position Count": {
        "query": "SELECT COUNT(*) FROM ats_positions",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of ATS positions",
    },
    "ATS Position Exists by External ID": {
        "query": "SELECT COUNT(*) FROM ats_positions WHERE external_id = :external_id",
        "required_params": ["external_id"],
        "result_type": "boolean",
        "description": "Verify ATS position exists by external ID",
    },
    "ATS Position Position Link": {
        "query": "SELECT position_id FROM ats_positions WHERE id = :ats_position_id",
        "required_params": ["ats_position_id"],
        "result_type": "integer",
        "description": "Get linked position ID for ATS position",
    },
    # =========================================================================
    # JIE ROLE CHECKS
    # =========================================================================
    "JIE Role Count": {
        "query": "SELECT COUNT(*) FROM jie_roles",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of JIE roles",
    },
    "JIE Role Exists by Title": {
        "query": """
            SELECT COUNT(*) FROM jie_roles
            WHERE LOWER(title) LIKE LOWER('%' || :title || '%')
        """,
        "required_params": ["title"],
        "result_type": "boolean",
        "description": "Verify JIE role exists by title (fuzzy match)",
    },
    "JIE Role Title": {
        "query": "SELECT title FROM jie_roles WHERE id = :jie_role_id",
        "required_params": ["jie_role_id"],
        "result_type": "string",
        "description": "Get JIE role title by ID",
    },
    # =========================================================================
    # APPLICATION STAGE HISTORY CHECKS
    # =========================================================================
    "Application Stage History Count": {
        "query": "SELECT COUNT(*) FROM application_stages WHERE application_id = :application_id",
        "required_params": ["application_id"],
        "result_type": "integer",
        "description": "Count stage history entries for an application",
    },
    "Application Current Stage from History": {
        "query": """
            SELECT stage FROM application_stages
            WHERE application_id = :application_id
            ORDER BY created_at DESC
            LIMIT 1
        """,
        "required_params": ["application_id"],
        "result_type": "string",
        "description": "Get most recent stage from history",
    },
}


async def eightfold_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify any Eightfold field using dropdown + value interface.

    Domain experts select what to check from a dropdown and provide the expected value.
    The system automatically generates and runs the appropriate SQL query.
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version

    # Validate helper results available
    if not input.helper_results:
        raise ValueError("Helper results not available")

    # Get field selection (strip arrow notation if present)
    field_to_check_raw = input.verifier.verifier_values.get("field_to_check")
    if not field_to_check_raw:
        raise ValueError("field_to_check is required")

    # Strip arrow notation (e.g., "Profile Count by Status → Status" -> "Profile Count by Status")
    field_to_check = field_to_check_raw.split(" → ")[0].strip()

    # Get expected value
    expected_value = input.verifier.verifier_values.get("expected_value")
    if expected_value is None:
        raise ValueError("expected_value is required")

    # Get field configuration
    field_config = FIELD_MAPPINGS.get(field_to_check)
    if not field_config:
        raise ValueError(f"Unknown field_to_check: {field_to_check}")

    # Extract required parameters from verifier_values
    params = {}
    for param in field_config["required_params"]:
        value = input.verifier.verifier_values.get(param)
        if value is None:
            raise ValueError(
                f"Required parameter '{param}' is missing for '{field_to_check}'"
            )
        params[param] = value

    # Extract optional parameters from verifier_values
    optional_params = field_config.get("optional_params", [])
    for param in optional_params:
        value = input.verifier.verifier_values.get(param)
        # Allow None for optional parameters
        params[param] = value

    # Special validation for "Note Exists with Text" - at least one ID must be provided
    if field_to_check == "Note Exists with Text":
        if params.get("profile_id") is None and params.get("application_id") is None:
            raise ValueError(
                "At least one of 'profile_id' or 'application_id' must be provided for 'Note Exists with Text'"
            )

    # Get database connection from snapshot_dbs helper
    dbs = input.helper_results.get(HelperIds.SNAPSHOT_DBS, {})
    if not dbs:
        raise ValueError("No databases found in snapshot")

    # Filter to Eightfold databases only (avoid picking up other HR apps)
    eightfold_dbs = [
        (alias, db_info)
        for alias, db_info in dbs.items()
        if "eightfold" in alias.lower()
    ]
    if len(eightfold_dbs) != 1:
        raise ValueError(
            f"Expected exactly 1 Eightfold database, found {len(eightfold_dbs)}. Available: {list(dbs.keys())}"
        )

    _alias, db_info = eightfold_dbs[0]
    conn = db_info.get("connection")
    if not conn:
        raise ValueError("Eightfold database found but connection is None")

    # Execute query
    try:
        cursor = conn.cursor()
        result_row = cursor.execute(field_config["query"], params).fetchone()

        if result_row is None:
            result = None
        else:
            result = result_row[0]

        logger.info(
            f"Eightfold field check: {field_to_check} = {result} (expected: {expected_value})"
        )

    except Exception as e:
        logger.error(f"Error executing query for {field_to_check}: {e}")
        raise ValueError(f"Query execution failed: {e!s}") from e

    # Compare based on result_type
    result_type = field_config["result_type"]

    if result is None:
        matches = False
        message = f"{field_to_check}: No result found"
    elif result_type == "decimal":
        # Get tolerance with validation (only needed for decimal comparisons)
        try:
            tolerance_value = input.verifier.verifier_values.get("tolerance", 0.01)
            if tolerance_value is None:
                tolerance_value = 0.01
            tolerance = Decimal(str(tolerance_value))
        except (ValueError, TypeError, InvalidOperation) as e:
            raise ValueError(f"Invalid tolerance value: {e!s}") from e

        # Ensure tolerance is non-negative
        if tolerance < 0:
            raise ValueError(f"Tolerance must be non-negative, got: {tolerance}")

        try:
            result_decimal = Decimal(str(result))
            expected_decimal = Decimal(str(expected_value))
        except (ValueError, TypeError, InvalidOperation) as e:
            raise ValueError(
                f"Invalid decimal value - result: {result}, expected: {expected_value}. Error: {e!s}"
            ) from e
        matches = abs(result_decimal - expected_decimal) <= tolerance
        message = f"{field_to_check}: {float(result_decimal):.2f} {'✓' if matches else f'✗ expected {float(expected_decimal):.2f}'}"
    elif result_type == "boolean":
        # Convert various result types to boolean
        # Handles: COUNT results (int), direct boolean columns, string values
        try:
            if isinstance(result, bool):
                actual_bool = result
            elif isinstance(result, int | float):
                actual_bool = result > 0
            elif isinstance(result, str):
                actual_bool = result.lower() in ("true", "1", "yes", "t", "y")
            else:
                # Fallback: try truthiness
                actual_bool = bool(result)
        except (ValueError, TypeError) as e:
            raise ValueError(
                f"Invalid boolean value - result: {result}. Error: {e!s}"
            ) from e
        expected_str = str(expected_value).strip().lower()
        expected_bool = expected_str in ("true", "1", "yes", "t", "y")
        matches = actual_bool == expected_bool
        message = f"{field_to_check}: {'Yes' if actual_bool else 'No'} {'✓' if matches else '✗'}"
    elif result_type == "integer":
        try:
            actual_int = int(result)
            expected_int = int(expected_value)
            matches = actual_int == expected_int
        except (ValueError, TypeError) as e:
            raise ValueError(
                f"Invalid integer value - result: {result}, expected: {expected_value}. Error: {e!s}"
            ) from e
        message = f"{field_to_check}: {actual_int} {'✓' if matches else f'✗ expected {expected_int}'}"
    elif result_type == "string":
        # Case-insensitive string comparison
        actual_str = str(result).strip().lower() if result is not None else ""
        expected_str = str(expected_value).strip().lower()
        matches = actual_str == expected_str
        message = f"{field_to_check}: '{result}' {'✓' if matches else f'✗ expected {expected_value}'}"
    else:
        matches = str(result) == str(expected_value)
        message = f"{field_to_check}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=1.0 if matches else 0.0,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "actual_value": str(result) if result is not None else None,
            "expected_value": str(expected_value),
            "field_checked": field_to_check,
            "matches": matches,
        },
        message=message,
    )
