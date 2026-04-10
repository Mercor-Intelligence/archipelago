"""OpenEMR Field Check Verifier - Flexible dropdown + value interface.

This verifier provides a simple dropdown interface for domain experts to check
any OpenEMR field without writing SQL. The dropdown selection maps to
pre-defined SQL queries verified against the actual OpenEMR schema.
"""

import io
import sqlite3
import tempfile
import zipfile
from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus

# Field mappings for OpenEMR database queries
# All queries verified against actual SQLite schema
FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    # =========================================================================
    # Encounter Checks
    # =========================================================================
    "Encounter Count": {
        "query": "SELECT COUNT(*) FROM form_encounter",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of encounters",
    },
    "Encounter Exists": {
        "query": "SELECT COUNT(*) FROM form_encounter WHERE id = :encounter_id",
        "required_params": ["encounter_id"],
        "result_type": "boolean",
        "description": "Check if a specific encounter exists",
    },
    "Encounter Status": {
        "query": "SELECT status FROM form_encounter WHERE id = :encounter_id",
        "required_params": ["encounter_id"],
        "result_type": "string",
        "description": "Get status of a specific encounter",
    },
    "Encounter Patient ID": {
        "query": "SELECT pid FROM form_encounter WHERE id = :encounter_id",
        "required_params": ["encounter_id"],
        "result_type": "string",
        "description": "Get patient ID for a specific encounter",
    },
    # =========================================================================
    # Notes Checks
    # =========================================================================
    "Notes Count": {
        "query": "SELECT COUNT(*) FROM notes",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of notes",
    },
    "Notes Count by Encounter": {
        "query": "SELECT COUNT(*) FROM notes WHERE encounter_id = :encounter_id",
        "required_params": ["encounter_id"],
        "result_type": "integer",
        "description": "Count notes for a specific encounter",
    },
    "Notes Exists": {
        "query": "SELECT COUNT(*) FROM notes WHERE encounter_id = :encounter_id",
        "required_params": ["encounter_id"],
        "result_type": "boolean",
        "description": "Check if notes exist for an encounter",
    },
    "Notes Count by Type": {
        "query": "SELECT COUNT(*) FROM notes WHERE encounter_id = :encounter_id AND note_type = :note_type",
        "required_params": ["encounter_id", "note_type"],
        "result_type": "integer",
        "description": "Count notes of a specific type for an encounter",
    },
    # =========================================================================
    # Orders Checks
    # =========================================================================
    "Orders Count": {
        "query": "SELECT COUNT(*) FROM orders",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of orders",
    },
    "Orders Count by Encounter": {
        "query": "SELECT COUNT(*) FROM orders WHERE encounter_id = :encounter_id",
        "required_params": ["encounter_id"],
        "result_type": "integer",
        "description": "Count orders for a specific encounter",
    },
    "Orders Exists": {
        "query": "SELECT COUNT(*) FROM orders WHERE encounter_id = :encounter_id",
        "required_params": ["encounter_id"],
        "result_type": "boolean",
        "description": "Check if orders exist for an encounter",
    },
    "Orders Count by Type": {
        "query": "SELECT COUNT(*) FROM orders WHERE encounter_id = :encounter_id AND order_type = :order_type",
        "required_params": ["encounter_id", "order_type"],
        "result_type": "integer",
        "description": "Count orders of a specific type for an encounter",
    },
    "Orders Count by Status": {
        "query": "SELECT COUNT(*) FROM orders WHERE encounter_id = :encounter_id AND status = :status",
        "required_params": ["encounter_id", "status"],
        "result_type": "integer",
        "description": "Count orders with a specific status for an encounter",
    },
    # =========================================================================
    # Differential Checks
    # =========================================================================
    "Differential Exists": {
        "query": "SELECT COUNT(*) FROM differentials WHERE encounter_id = :encounter_id",
        "required_params": ["encounter_id"],
        "result_type": "boolean",
        "description": "Check if differential diagnosis exists for an encounter",
    },
    "Differential Count": {
        "query": "SELECT COUNT(*) FROM differentials",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of differential diagnoses",
    },
    "Differential Count by Encounter": {
        "query": "SELECT COUNT(*) FROM differentials WHERE encounter_id = :encounter_id",
        "required_params": ["encounter_id"],
        "result_type": "integer",
        "description": "Count differential diagnoses for a specific encounter",
    },
    # =========================================================================
    # Medications Checks
    # =========================================================================
    "Medications Count": {
        "query": "SELECT COUNT(*) FROM medications",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of medications",
    },
    "Medications Count by Encounter": {
        "query": "SELECT COUNT(*) FROM medications WHERE encounter_id = :encounter_id",
        "required_params": ["encounter_id"],
        "result_type": "integer",
        "description": "Count medications for a specific encounter",
    },
    "Medications Exists": {
        "query": "SELECT COUNT(*) FROM medications WHERE encounter_id = :encounter_id",
        "required_params": ["encounter_id"],
        "result_type": "boolean",
        "description": "Check if medications exist for an encounter",
    },
    "Active Medications Count": {
        "query": "SELECT COUNT(*) FROM medications WHERE encounter_id = :encounter_id AND is_active = 1",
        "required_params": ["encounter_id"],
        "result_type": "integer",
        "description": "Count active medications for an encounter",
    },
    # =========================================================================
    # Lab Results Checks
    # =========================================================================
    "Lab Results Count": {
        "query": "SELECT COUNT(*) FROM lab_results",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of lab results",
    },
    "Lab Results Count by Patient": {
        "query": "SELECT COUNT(*) FROM lab_results WHERE patient_id = :patient_id",
        "required_params": ["patient_id"],
        "result_type": "integer",
        "description": "Count lab results for a specific patient",
    },
    "Lab Results Exists": {
        "query": "SELECT COUNT(*) FROM lab_results WHERE patient_id = :patient_id",
        "required_params": ["patient_id"],
        "result_type": "boolean",
        "description": "Check if lab results exist for a patient",
    },
    "Lab Results Count by Panel": {
        "query": "SELECT COUNT(*) FROM lab_results WHERE patient_id = :patient_id AND panel_name = :panel_name",
        "required_params": ["patient_id", "panel_name"],
        "result_type": "integer",
        "description": "Count lab results for a specific panel",
    },
}


def _extract_db_from_snapshot(
    final_snapshot_bytes: io.BytesIO,
) -> tuple[sqlite3.Connection | None, str | None]:
    """Extract SQLite database from snapshot zip.

    Args:
        final_snapshot_bytes: BytesIO containing the snapshot zip file

    Returns:
        Tuple of (SQLite connection or None, temp file path or None)
    """
    final_snapshot_bytes.seek(0)

    try:
        with zipfile.ZipFile(final_snapshot_bytes, "r") as final_zip:
            # Find OpenEMR database file
            db_files = [
                f
                for f in final_zip.namelist()
                if f.endswith(".db") and "openemr" in f.lower()
            ]

            # Fallback to any .db file
            if not db_files:
                db_files = [f for f in final_zip.namelist() if f.endswith(".db")]

            if not db_files:
                logger.warning("No database file found in snapshot")
                return None, None

            db_file = db_files[0]
            db_bytes = final_zip.read(db_file)

            temp_file = tempfile.NamedTemporaryFile(
                suffix=".db", delete=False, mode="wb"
            )
            temp_file_path = temp_file.name

            try:
                temp_file.write(db_bytes)
                temp_file.flush()
                temp_file.close()

                conn = sqlite3.connect(temp_file_path)
                logger.info(f"Extracted OpenEMR database: {db_file}")
                return conn, temp_file_path

            except Exception as e:
                logger.error(f"Failed to extract database: {e}")
                try:
                    import os

                    os.unlink(temp_file_path)
                except OSError:
                    pass
                return None, None

    except zipfile.BadZipFile as e:
        logger.error(f"Invalid snapshot zip file: {e}")
        return None, None


async def openemr_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify any OpenEMR field using dropdown + value interface.

    Domain experts select what to check from a dropdown and provide the expected value.
    The system automatically generates and runs the appropriate SQL query.
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version

    # Validate helper results available
    if not input.verifier.verifier_values:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Verifier values not available",
        )

    # Get field selection
    field_to_check = input.verifier.verifier_values.get("field_to_check")
    if not field_to_check:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="field_to_check is required",
        )

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
                message=f"Required parameter '{param}' is missing",
            )
        params[param] = value

    # Initialize variables for cleanup
    conn: sqlite3.Connection | None = None
    temp_file_path: str | None = None

    try:
        # Extract database from snapshot
        conn, temp_file_path = _extract_db_from_snapshot(input.final_snapshot_bytes)
        if not conn:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message="No database found in snapshot",
            )
        # Execute query
        cursor = conn.cursor()
        result_row = cursor.execute(field_config["query"], params).fetchone()

        if result_row is None:
            result = None
        else:
            result = result_row[0]

        logger.info(
            f"OpenEMR field check: {field_to_check} = {result} (expected: {expected_value})"
        )

        # Compare result to expected value based on result type
        result_type = field_config.get("result_type", "string")
        matches = False

        if result is None:
            matches = False
            message = f"{field_to_check}: No result found"
        elif result_type == "boolean":
            # Convert result to boolean (0/1 or True/False)
            result_bool = bool(result)
            expected_bool = str(expected_value).lower() in ("true", "1", "yes")
            matches = result_bool == expected_bool
            message = f"{field_to_check}: {result_bool} {'✓' if matches else f'✗ expected {expected_bool}'}"

        elif result_type == "integer":
            try:
                result_int = int(result)
                expected_int = int(expected_value)
                matches = result_int == expected_int
                message = f"{field_to_check}: {result_int} {'✓' if matches else f'✗ expected {expected_int}'}"
            except (ValueError, TypeError) as e:
                return VerifierResult(
                    verifier_id=verifier_id,
                    verifier_version=verifier_version,
                    score=0.0,
                    status=VerifierResultStatus.ERROR,
                    verifier_result_values={},
                    message=f"Invalid integer value - result: {result}, expected: {expected_value}. Error: {str(e)}",
                )

        elif result_type == "decimal":
            try:
                result_decimal = Decimal(str(result))
                expected_decimal = Decimal(str(expected_value))
                matches = abs(result_decimal - expected_decimal) < Decimal("0.01")
                message = f"{field_to_check}: {result_decimal} {'✓' if matches else f'✗ expected {expected_decimal}'}"
            except (InvalidOperation, ValueError, TypeError) as e:
                return VerifierResult(
                    verifier_id=verifier_id,
                    verifier_version=verifier_version,
                    score=0.0,
                    status=VerifierResultStatus.ERROR,
                    verifier_result_values={},
                    message=f"Invalid decimal value - result: {result}, expected: {expected_value}. Error: {str(e)}",
                )

        elif result_type == "string":
            # Case-insensitive comparison
            matches = str(result).lower() == str(expected_value).lower()
            message = f"{field_to_check}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"

        else:
            # Default: exact match
            matches = str(result) == str(expected_value)
            message = f"{field_to_check}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"

        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=1.0 if matches else 0.0,
            verifier_result_values={
                "actual_value": str(result) if result is not None else None,
                "expected_value": str(expected_value),
                "field_checked": field_to_check,
                "matches": matches,
            },
            message=message,
        )

    except Exception as e:
        logger.error(f"Error executing query for {field_to_check}: {e}")
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Query execution failed: {str(e)}",
        )

    finally:
        # Clean up database connection
        if conn:
            try:
                conn.close()
            except Exception as e:
                logger.warning(f"Failed to close database connection: {e}")

        # Clean up temp file
        if temp_file_path:
            try:
                import os

                os.unlink(temp_file_path)
            except OSError as e:
                logger.warning(f"Failed to delete temp file {temp_file_path}: {e}")
