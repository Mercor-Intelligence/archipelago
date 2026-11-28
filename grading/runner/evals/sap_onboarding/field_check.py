"""SAP Onboarding Field Check Verifier - Flexible dropdown + value interface.

This verifier provides a simple dropdown interface for domain experts to check
any SAP Onboarding field without writing SQL. The dropdown selection maps to
pre-defined SQL queries that have been verified against the actual SAP Onboarding schema.

The verifier can check entities created by:
- Seeding tools (sf_onboarding_process_create, sf_onboarding_nha_step_create,
  sf_onboarding_activity_meeting_create, sf_onboarding_activity_equipment_create,
  sf_onboarding_activity_goal_create)
- Meta seeding tool (sf_onboarding_seeding)
- Regular onboarding workflow tools
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

# All queries are schema-accurate for SAP Onboarding
FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
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
        "query": "SELECT COUNT(*) FROM users WHERE disabled = 0",
        "required_params": [],
        "result_type": "integer",
        "description": "Count active (non-disabled) users",
    },
    "User Exists by Username": {
        "query": "SELECT COUNT(*) FROM users WHERE LOWER(user_name) = LOWER(:user_name)",
        "required_params": ["user_name"],
        "result_type": "boolean",
        "description": "Verify user exists by username",
    },
    "User Exists by Email": {
        "query": "SELECT COUNT(*) FROM users WHERE LOWER(email) = LOWER(:email)",
        "required_params": ["email"],
        "result_type": "boolean",
        "description": "Verify user exists by email address",
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
    "User Email": {
        "query": "SELECT email FROM users WHERE id = :user_id",
        "required_params": ["user_id"],
        "result_type": "string",
        "description": "Get user's email by ID",
    },
    "User Disabled Status": {
        "query": "SELECT disabled FROM users WHERE id = :user_id",
        "required_params": ["user_id"],
        "result_type": "boolean",
        "description": "Check if user is disabled (1 = disabled, 0 = active)",
    },
    # =========================================================================
    # ONBOARDING PROCESS CHECKS
    # =========================================================================
    "Onboarding Process Count": {
        "query": "SELECT COUNT(*) FROM onboarding_processes",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of onboarding processes",
    },
    "Onboarding Process Count by Status": {
        "query": "SELECT COUNT(*) FROM onboarding_processes WHERE process_status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count processes by status (OPEN, COMPLETED, CANCELLED)",
    },
    "Open Onboarding Process Count": {
        "query": "SELECT COUNT(*) FROM onboarding_processes WHERE process_status = 'OPEN'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count open onboarding processes",
    },
    "Completed Onboarding Process Count": {
        "query": "SELECT COUNT(*) FROM onboarding_processes WHERE process_status = 'COMPLETED'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count completed onboarding processes",
    },
    "Onboarding Process Exists": {
        "query": "SELECT COUNT(*) FROM onboarding_processes WHERE onboarding_process_id = :process_id",
        "required_params": ["process_id"],
        "result_type": "boolean",
        "description": "Verify onboarding process exists by ID",
    },
    "Onboarding Process Status": {
        "query": "SELECT process_status FROM onboarding_processes WHERE onboarding_process_id = :process_id",
        "required_params": ["process_id"],
        "result_type": "string",
        "description": "Get onboarding process status by ID",
    },
    "Onboarding Process External Name": {
        "query": "SELECT external_name FROM onboarding_processes WHERE onboarding_process_id = :process_id",
        "required_params": ["process_id"],
        "result_type": "string",
        "description": "Get onboarding process external name by ID",
    },
    "Onboarding Process Candidate Info": {
        "query": "SELECT candidate_info FROM onboarding_processes WHERE onboarding_process_id = :process_id",
        "required_params": ["process_id"],
        "result_type": "string",
        "description": "Get candidate info for an onboarding process",
    },
    "Onboarding Process Hiring Manager": {
        "query": "SELECT assigned_hiring_manager_id FROM onboarding_processes WHERE onboarding_process_id = :process_id",
        "required_params": ["process_id"],
        "result_type": "string",
        "description": "Get assigned hiring manager ID for an onboarding process",
    },
    "Onboarding Process Config": {
        "query": "SELECT process_config FROM onboarding_processes WHERE onboarding_process_id = :process_id",
        "required_params": ["process_id"],
        "result_type": "string",
        "description": "Get process configuration for an onboarding process",
    },
    # =========================================================================
    # NHA STEP CHECKS
    # =========================================================================
    "NHA Step Count": {
        "query": "SELECT COUNT(*) FROM onboarding_nha_steps",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of NHA steps",
    },
    "NHA Step Count by Process": {
        "query": "SELECT COUNT(*) FROM onboarding_nha_steps WHERE onboarding_process_id = :process_id",
        "required_params": ["process_id"],
        "result_type": "integer",
        "description": "Count NHA steps for a specific onboarding process",
    },
    "NHA Step Count by Status": {
        "query": "SELECT COUNT(*) FROM onboarding_nha_steps WHERE process_step_status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count NHA steps by status (OPEN, COMPLETED, CANCELLED)",
    },
    "NHA Step Exists": {
        "query": "SELECT COUNT(*) FROM onboarding_nha_steps WHERE process_step_id = :step_id",
        "required_params": ["step_id"],
        "result_type": "boolean",
        "description": "Verify NHA step exists by step ID",
    },
    "NHA Step Status": {
        "query": "SELECT process_step_status FROM onboarding_nha_steps WHERE process_step_id = :step_id",
        "required_params": ["step_id"],
        "result_type": "string",
        "description": "Get NHA step status by step ID",
    },
    "NHA Step External Name": {
        "query": "SELECT external_name FROM onboarding_nha_steps WHERE process_step_id = :step_id",
        "required_params": ["step_id"],
        "result_type": "string",
        "description": "Get NHA step external name by step ID",
    },
    # =========================================================================
    # MEETING ACTIVITY CHECKS
    # =========================================================================
    "Meeting Activity Count": {
        "query": "SELECT COUNT(*) FROM onboarding_meeting_activities",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of meeting activities",
    },
    "Meeting Activity Count by Step": {
        "query": "SELECT COUNT(*) FROM onboarding_meeting_activities WHERE process_step_id = :step_id AND onboarding_process_id = :process_id",
        "required_params": ["step_id", "process_id"],
        "result_type": "integer",
        "description": "Count meeting activities for a specific NHA step",
    },
    "Meeting Activity Count by Status": {
        "query": "SELECT COUNT(*) FROM onboarding_meeting_activities WHERE activity_status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count meeting activities by status (OPEN, COMPLETED, CANCELLED)",
    },
    "Meeting Activity Exists": {
        "query": "SELECT COUNT(*) FROM onboarding_meeting_activities WHERE activity_id = :activity_id",
        "required_params": ["activity_id"],
        "result_type": "boolean",
        "description": "Verify meeting activity exists by activity ID",
    },
    "Meeting Activity Status": {
        "query": "SELECT activity_status FROM onboarding_meeting_activities WHERE activity_id = :activity_id",
        "required_params": ["activity_id"],
        "result_type": "string",
        "description": "Get meeting activity status by activity ID",
    },
    "Meeting Activity Optional": {
        "query": "SELECT optional FROM onboarding_meeting_activities WHERE activity_id = :activity_id",
        "required_params": ["activity_id"],
        "result_type": "boolean",
        "description": "Check if meeting activity is optional (1 = optional, 0 = required)",
    },
    "Meeting Activity External Name": {
        "query": "SELECT external_name FROM onboarding_meeting_activities WHERE activity_id = :activity_id",
        "required_params": ["activity_id"],
        "result_type": "string",
        "description": "Get meeting activity external name by activity ID",
    },
    # =========================================================================
    # EQUIPMENT ACTIVITY CHECKS
    # =========================================================================
    "Equipment Activity Count": {
        "query": "SELECT COUNT(*) FROM onboarding_equipment_activities",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of equipment activities",
    },
    "Equipment Activity Count by Step": {
        "query": "SELECT COUNT(*) FROM onboarding_equipment_activities WHERE process_step_id = :step_id AND onboarding_process_id = :process_id",
        "required_params": ["step_id", "process_id"],
        "result_type": "integer",
        "description": "Count equipment activities for a specific NHA step",
    },
    "Equipment Activity Count by Status": {
        "query": "SELECT COUNT(*) FROM onboarding_equipment_activities WHERE activity_status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count equipment activities by status (OPEN, COMPLETED, CANCELLED)",
    },
    "Equipment Activity Exists": {
        "query": "SELECT COUNT(*) FROM onboarding_equipment_activities WHERE activity_id = :activity_id",
        "required_params": ["activity_id"],
        "result_type": "boolean",
        "description": "Verify equipment activity exists by activity ID",
    },
    "Equipment Activity Status": {
        "query": "SELECT activity_status FROM onboarding_equipment_activities WHERE activity_id = :activity_id",
        "required_params": ["activity_id"],
        "result_type": "string",
        "description": "Get equipment activity status by activity ID",
    },
    "Equipment Activity Optional": {
        "query": "SELECT optional FROM onboarding_equipment_activities WHERE activity_id = :activity_id",
        "required_params": ["activity_id"],
        "result_type": "boolean",
        "description": "Check if equipment activity is optional (1 = optional, 0 = required)",
    },
    "Equipment Activity External Name": {
        "query": "SELECT external_name FROM onboarding_equipment_activities WHERE activity_id = :activity_id",
        "required_params": ["activity_id"],
        "result_type": "string",
        "description": "Get equipment activity external name by activity ID",
    },
    # =========================================================================
    # GOAL ACTIVITY CHECKS
    # =========================================================================
    "Goal Activity Count": {
        "query": "SELECT COUNT(*) FROM onboarding_goal_activities",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of goal activities",
    },
    "Goal Activity Count by Step": {
        "query": "SELECT COUNT(*) FROM onboarding_goal_activities WHERE process_step_id = :step_id AND onboarding_process_id = :process_id",
        "required_params": ["step_id", "process_id"],
        "result_type": "integer",
        "description": "Count goal activities for a specific NHA step",
    },
    "Goal Activity Count by Status": {
        "query": "SELECT COUNT(*) FROM onboarding_goal_activities WHERE activity_status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count goal activities by status (OPEN, COMPLETED, CANCELLED)",
    },
    "Goal Activity Exists": {
        "query": "SELECT COUNT(*) FROM onboarding_goal_activities WHERE activity_id = :activity_id",
        "required_params": ["activity_id"],
        "result_type": "boolean",
        "description": "Verify goal activity exists by activity ID",
    },
    "Goal Activity Status": {
        "query": "SELECT activity_status FROM onboarding_goal_activities WHERE activity_id = :activity_id",
        "required_params": ["activity_id"],
        "result_type": "string",
        "description": "Get goal activity status by activity ID",
    },
    "Goal Activity Optional": {
        "query": "SELECT optional FROM onboarding_goal_activities WHERE activity_id = :activity_id",
        "required_params": ["activity_id"],
        "result_type": "boolean",
        "description": "Check if goal activity is optional (1 = optional, 0 = required)",
    },
    "Goal Activity Goals Transfer Status": {
        "query": "SELECT goals_transfer_status FROM onboarding_goal_activities WHERE activity_id = :activity_id",
        "required_params": ["activity_id"],
        "result_type": "string",
        "description": "Get goals transfer status (NOT_TRANSFERED, TRANSFERED)",
    },
    "Goal Activity Status Set Manually": {
        "query": "SELECT goal_activity_status_set_manually FROM onboarding_goal_activities WHERE activity_id = :activity_id",
        "required_params": ["activity_id"],
        "result_type": "boolean",
        "description": "Check if goal activity status was set manually (1 = yes, 0 = no)",
    },
    "Goal Activity External Name": {
        "query": "SELECT external_name FROM onboarding_goal_activities WHERE activity_id = :activity_id",
        "required_params": ["activity_id"],
        "result_type": "string",
        "description": "Get goal activity external name by activity ID",
    },
    # =========================================================================
    # ONBOARDEE CHECKS
    # =========================================================================
    "Onboardee Count": {
        "query": "SELECT COUNT(*) FROM onboardees",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of onboardees",
    },
    "Onboardee Exists by Username": {
        "query": "SELECT COUNT(*) FROM onboardees WHERE LOWER(user_name) = LOWER(:user_name)",
        "required_params": ["user_name"],
        "result_type": "boolean",
        "description": "Verify onboardee exists by username",
    },
    "Onboardee Exists by User ID": {
        "query": "SELECT COUNT(*) FROM onboardees WHERE user_id = :user_id_onboardee",
        "required_params": ["user_id_onboardee"],
        "result_type": "boolean",
        "description": "Verify onboardee exists by user ID",
    },
    "Onboardee Exists by Email": {
        "query": "SELECT COUNT(*) FROM onboardees WHERE LOWER(email) = LOWER(:email)",
        "required_params": ["email"],
        "result_type": "boolean",
        "description": "Verify onboardee exists by email address",
    },
    "Onboardee Exists by Application ID": {
        "query": "SELECT COUNT(*) FROM onboardees WHERE application_id = :application_id",
        "required_params": ["application_id"],
        "result_type": "boolean",
        "description": "Verify onboardee exists by application ID",
    },
    "Onboardee Email": {
        "query": "SELECT email FROM onboardees WHERE id = :onboardee_id",
        "required_params": ["onboardee_id"],
        "result_type": "string",
        "description": "Get onboardee email by ID",
    },
    "Onboardee User ID": {
        "query": "SELECT user_id FROM onboardees WHERE id = :onboardee_id",
        "required_params": ["onboardee_id"],
        "result_type": "string",
        "description": "Get onboardee user ID by ID",
    },
    "Onboardee Application ID": {
        "query": "SELECT application_id FROM onboardees WHERE id = :onboardee_id",
        "required_params": ["onboardee_id"],
        "result_type": "string",
        "description": "Get onboardee application ID by ID",
    },
    "Onboardee Hire Type": {
        "query": "SELECT hire_type FROM onboardees WHERE id = :onboardee_id",
        "required_params": ["onboardee_id"],
        "result_type": "string",
        "description": "Get onboardee hire type (NEW_HIRE, REHIRE_NEW_EMPLOYMENT, REHIRE_OLD_EMPLOYMENT, LEGAL_ENTITY_TRANSFER_NEW_EMPL)",
    },
    "Onboardee Onboarding Process ID": {
        "query": "SELECT onboarding_process_id FROM onboardees WHERE id = :onboardee_id",
        "required_params": ["onboardee_id"],
        "result_type": "integer",
        "description": "Get onboarding process ID associated with an onboardee",
    },
    "Onboardee Count by Process": {
        "query": "SELECT COUNT(*) FROM onboardees WHERE onboarding_process_id = :process_id",
        "required_params": ["process_id"],
        "result_type": "integer",
        "description": "Count onboardees for a specific onboarding process",
    },
}


async def sap_onboarding_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify any SAP Onboarding field using dropdown + value interface.

    Domain experts select what to check from a dropdown and provide the expected value.
    The system automatically generates and runs the appropriate SQL query.
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version

    # Validate helper results available
    if not input.helper_results:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Helper results not available",
        )

    # Get field selection and strip arrow notation if present
    field_to_check_raw = input.verifier.verifier_values.get("field_to_check")
    if not field_to_check_raw:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="field_to_check is required",
        )

    # Strip arrow notation (e.g., "User Exists by Username → Username" -> "User Exists by Username")
    field_to_check = field_to_check_raw.split(" → ")[0].strip()

    # Get expected value
    expected_value = input.verifier.verifier_values.get("expected_value")
    if expected_value is None:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="expected_value is required",
        )

    # Get field configuration
    field_config = FIELD_MAPPINGS.get(field_to_check)
    if not field_config:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Unknown field_to_check: {field_to_check}",
        )

    # Extract required parameters from verifier_values
    params = {}
    for param in field_config["required_params"]:
        value = input.verifier.verifier_values.get(param)
        if value is None:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Required parameter '{param}' is missing for '{field_to_check}'",
            )
        params[param] = value

    # Get database connection from snapshot_dbs helper
    dbs = input.helper_results.get(HelperIds.SNAPSHOT_DBS, {})
    if not dbs:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="No databases found in snapshot",
        )

    # Filter to SAP Onboarding database only
    # Expected alias: .apps_data_sap_onboarding_data (from /.apps_data/sap_onboarding/data.db)
    sap_dbs = [
        (alias, db_info)
        for alias, db_info in dbs.items()
        if "sap_onboarding" in alias.lower()
    ]
    if len(sap_dbs) != 1:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Expected exactly 1 SAP Onboarding database (alias containing 'sap_onboarding'), found {len(sap_dbs)}. Available: {list(dbs.keys())}",
        )

    _alias, db_info = sap_dbs[0]
    conn = db_info.get("connection")
    if not conn:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="SAP database found but connection is None",
        )

    # Execute query
    try:
        cursor = conn.cursor()
        result_row = cursor.execute(field_config["query"], params).fetchone()

        if result_row is None:
            result = None
        else:
            result = result_row[0]

        logger.info(
            f"SAP Onboarding field check: {field_to_check} = {result} (expected: {expected_value})"
        )

    except Exception as e:
        logger.error(f"Error executing query for {field_to_check}: {e}")
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Query execution failed: {e!s}",
        )

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
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Invalid tolerance value: {e!s}",
            )

        # Ensure tolerance is non-negative
        if tolerance < 0:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Tolerance must be non-negative, got: {tolerance}",
            )

        try:
            result_decimal = Decimal(str(result))
            expected_decimal = Decimal(str(expected_value))
        except (ValueError, TypeError, InvalidOperation) as e:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Invalid decimal value - result: {result}, expected: {expected_value}. Error: {e!s}",
            )
        matches = abs(result_decimal - expected_decimal) <= tolerance
        message = f"{field_to_check}: {float(result_decimal):.2f} {'✓' if matches else f'✗ expected {float(expected_decimal):.2f}'}"
    elif result_type == "boolean":
        # Convert various result types to boolean with robust handling
        try:
            if isinstance(result, bool):
                actual_bool = result
            elif isinstance(result, int):
                actual_bool = result > 0
            elif isinstance(result, str):
                actual_bool = result.lower() in ("true", "1", "yes", "t", "y")
            else:
                # Try numeric comparison as fallback
                actual_bool = float(result) > 0
        except (TypeError, ValueError) as e:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Invalid boolean value - result: {result}. Error: {e!s}",
            )
        # Use consistent truthy values for expected
        expected_str = str(expected_value).lower()
        expected_bool = expected_str in ("true", "1", "yes", "t", "y")
        matches = actual_bool == expected_bool
        message = f"{field_to_check}: {'Yes' if actual_bool else 'No'} {'✓' if matches else '✗'}"
    elif result_type == "integer":
        try:
            matches = int(result) == int(expected_value)
        except (ValueError, TypeError) as e:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Invalid integer value - result: {result}, expected: {expected_value}. Error: {e!s}",
            )
        message = f"{field_to_check}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"
    elif result_type == "string":
        # Case-insensitive string comparison with consistent None handling
        actual_str = str(result).strip().lower() if result is not None else ""
        expected_str = (
            str(expected_value).strip().lower() if expected_value is not None else ""
        )
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
