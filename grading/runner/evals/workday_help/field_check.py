"""Workday Help field check verifier."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import zipfile
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    "Case Count": {
        "query": "SELECT COUNT(*) FROM cases",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total cases",
    },
    "Case Count by Status": {
        "query": "SELECT COUNT(*) FROM cases WHERE status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count cases by status",
    },
    "Case Exists": {
        "query": "SELECT COUNT(*) FROM cases WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "boolean",
        "description": "Verify a case exists by ID",
    },
    "Case Exists by Candidate Identifier": {
        "query": "SELECT COUNT(*) FROM cases WHERE LOWER(candidate_identifier) = LOWER(:candidate_identifier)",
        "required_params": ["candidate_identifier"],
        "result_type": "boolean",
        "description": "Verify a case exists by candidate identifier",
    },
    "Case Status": {
        "query": "SELECT status FROM cases WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "string",
        "description": "Get case status",
    },
    "Case Owner": {
        "query": "SELECT owner FROM cases WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "string",
        "description": "Get case owner",
    },
    "Case Type": {
        "query": "SELECT case_type FROM cases WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "string",
        "description": "Get case type",
    },
    "Case Due Date": {
        "query": "SELECT due_date FROM cases WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "string",
        "description": "Get case due date",
    },
    "Case Created At": {
        "query": "SELECT created_at FROM cases WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "string",
        "description": "Get case created timestamp",
    },
    "Case Updated At": {
        "query": "SELECT updated_at FROM cases WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "string",
        "description": "Get case updated timestamp",
    },
    "Timeline Event Count": {
        "query": "SELECT COUNT(*) FROM timeline_events WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "integer",
        "description": "Count timeline events for a case",
    },
    "Timeline Event Count by Type": {
        "query": "SELECT COUNT(*) FROM timeline_events WHERE case_id = :case_id AND event_type = :event_type",
        "required_params": ["case_id", "event_type"],
        "result_type": "integer",
        "description": "Count timeline events by type",
    },
    "Timeline Event Exists by Type": {
        "query": "SELECT COUNT(*) FROM timeline_events WHERE case_id = :case_id AND event_type = :event_type",
        "required_params": ["case_id", "event_type"],
        "result_type": "boolean",
        "description": "Verify a timeline event exists by type",
    },
    "Latest Timeline Event Type": {
        "query": (
            "SELECT event_type FROM timeline_events WHERE case_id = :case_id "
            "ORDER BY created_at DESC, CAST(SUBSTR(event_id, 5) AS INTEGER) DESC LIMIT 1"
        ),
        "required_params": ["case_id"],
        "result_type": "string",
        "description": "Get the most recent timeline event type",
    },
    "Timeline Event Notes": {
        "query": (
            "SELECT notes FROM timeline_events WHERE case_id = :case_id AND event_type = :event_type "
            "ORDER BY created_at DESC, CAST(SUBSTR(event_id, 5) AS INTEGER) DESC LIMIT 1"
        ),
        "required_params": ["case_id", "event_type"],
        "result_type": "string",
        "description": "Get latest timeline event notes for a type",
    },
    "Message Count": {
        "query": "SELECT COUNT(*) FROM messages WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "integer",
        "description": "Count messages for a case",
    },
    "Message Count by Direction": {
        "query": "SELECT COUNT(*) FROM messages WHERE case_id = :case_id AND direction = :direction",
        "required_params": ["case_id", "direction"],
        "result_type": "integer",
        "description": "Count messages by direction",
    },
    "Message Exists by Body": {
        "query": "SELECT COUNT(*) FROM messages WHERE case_id = :case_id AND LOWER(body) = LOWER(:body)",
        "required_params": ["case_id", "body"],
        "result_type": "boolean",
        "description": "Verify a message exists by exact body match",
    },
    "Message Direction": {
        "query": "SELECT direction FROM messages WHERE message_id = :message_id",
        "required_params": ["message_id"],
        "result_type": "string",
        "description": "Get message direction",
    },
    "Message Sender": {
        "query": "SELECT sender FROM messages WHERE message_id = :message_id",
        "required_params": ["message_id"],
        "result_type": "string",
        "description": "Get message sender",
    },
    "Message Audience": {
        "query": "SELECT audience FROM messages WHERE message_id = :message_id",
        "required_params": ["message_id"],
        "result_type": "string",
        "description": "Get message audience",
    },
    "Attachment Count": {
        "query": "SELECT COUNT(*) FROM attachments WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "integer",
        "description": "Count attachments for a case",
    },
    "Attachment Exists by Filename": {
        "query": "SELECT COUNT(*) FROM attachments WHERE case_id = :case_id AND filename = :filename",
        "required_params": ["case_id", "filename"],
        "result_type": "boolean",
        "description": "Verify attachment exists by filename",
    },
    "Attachment Uploaded By": {
        "query": "SELECT uploader FROM attachments WHERE attachment_id = :attachment_id",
        "required_params": ["attachment_id"],
        "result_type": "string",
        "description": "Get attachment uploader",
    },
    "Attachment External Reference": {
        "query": "SELECT external_reference FROM attachments WHERE attachment_id = :attachment_id",
        "required_params": ["attachment_id"],
        "result_type": "string",
        "description": "Get attachment external reference",
    },
    "Audit Log Count": {
        "query": "SELECT COUNT(*) FROM audit_log WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "integer",
        "description": "Count audit log entries for a case",
    },
    "Audit Log Count by Action": {
        "query": "SELECT COUNT(*) FROM audit_log WHERE case_id = :case_id AND action = :action",
        "required_params": ["case_id", "action"],
        "result_type": "integer",
        "description": "Count audit log entries by action",
    },
    "Audit Log Exists by Action": {
        "query": "SELECT COUNT(*) FROM audit_log WHERE case_id = :case_id AND action = :action",
        "required_params": ["case_id", "action"],
        "result_type": "boolean",
        "description": "Verify audit log entry exists by action",
    },
    "Audit Log Actor": {
        "query": "SELECT actor FROM audit_log WHERE log_id = :log_id",
        "required_params": ["log_id"],
        "result_type": "string",
        "description": "Get audit log actor",
    },
    "Audit Log Actor Persona": {
        "query": "SELECT actor_persona FROM audit_log WHERE log_id = :log_id",
        "required_params": ["log_id"],
        "result_type": "string",
        "description": "Get audit log actor persona",
    },
}


def _get_connection_from_helpers(
    input: EvalImplInput,
) -> tuple[sqlite3.Connection | None, str | None]:
    """Get Workday database connection from snapshot_dbs helper.

    Returns:
        Tuple of (connection, error_message). If connection is None, error_message
        explains why (for better error reporting in verifier).
    """
    helper_results = input.helper_results or {}
    dbs = helper_results.get(HelperIds.SNAPSHOT_DBS)
    if not dbs:
        return None, "No databases found in snapshot"

    # Filter to Workday Help database only
    # Expected alias: .apps_data_workday_help_data (from /.apps_data/workday_help/data.db)
    workday_dbs = [
        (alias, db_info)
        for alias, db_info in dbs.items()
        if isinstance(db_info, dict) and "workday_help" in alias.lower()
    ]

    if len(workday_dbs) != 1:
        return (
            None,
            f"Expected exactly 1 Workday Help database (alias containing 'workday_help'), found {len(workday_dbs)}. Available: {list(dbs.keys())}",
        )

    _alias, db_info = workday_dbs[0]
    conn = db_info.get("connection")
    if not conn:
        return None, "Workday database found but connection is None"

    return conn, None


def _load_snapshot_db(
    snapshot_bytes: Any,
) -> tuple[sqlite3.Connection | None, str | None, str | None]:
    snapshot_bytes.seek(0)
    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as snapshot_zip:
            db_files = [
                name
                for name in snapshot_zip.namelist()
                if name.lower().endswith(".db") and "workday_help" in name.lower()
            ]
            if not db_files:
                db_files = [
                    name
                    for name in snapshot_zip.namelist()
                    if name.lower().endswith(".db")
                ]
            if not db_files:
                return None, None, "No database file found in snapshot"

            db_file = db_files[0]
            db_bytes = snapshot_zip.read(db_file)

    except zipfile.BadZipFile as exc:
        return None, None, f"Invalid snapshot zip: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        return None, None, f"Failed to read snapshot: {exc}"

    temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    temp_path = temp_file.name
    try:
        temp_file.write(db_bytes)
        temp_file.flush()
        temp_file.close()
        conn = sqlite3.connect(temp_path)
    except Exception as exc:
        # Close the file handle before attempting deletion
        try:
            temp_file.close()
        except Exception:
            pass
        try:
            os.unlink(temp_path)
        except OSError:
            logger.warning("Failed to clean up temp db after error")
        return None, None, f"Failed to load snapshot database: {exc}"

    return conn, temp_path, None


def _build_params(
    values: dict[str, Any], required_params: list[str]
) -> tuple[dict[str, Any], list[str]]:
    params: dict[str, Any] = {}
    missing: list[str] = []
    for param in required_params:
        value = values.get(param)
        if value is None:
            missing.append(param)
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                missing.append(param)
                continue
        params[param] = value
    return params, missing


def _compare_result(
    result: Any, expected_value: Any, result_type: str
) -> tuple[bool, str, Any]:
    # Use consistent truthy values
    truthy_values = ("true", "1", "yes", "t", "y")

    if result is None:
        if result_type == "boolean":
            expected_bool = str(expected_value).lower() in truthy_values
            matches = not expected_bool
            return matches, "No result found", False
        if result_type == "string":
            matches = str(expected_value).strip().upper() == "NULL"
            return matches, "No result found", None
        return False, "No result found", None

    if result_type == "boolean":
        # Handle various result types with robust boolean conversion
        try:
            if isinstance(result, bool):
                actual_bool = result
            elif isinstance(result, int):
                actual_bool = result > 0
            elif isinstance(result, str):
                actual_bool = result.lower() in truthy_values
            else:
                # Try numeric comparison as fallback
                actual_bool = float(result) > 0
        except (TypeError, ValueError):
            # If conversion fails, treat non-empty as truthy
            actual_bool = bool(result)

        expected_bool = str(expected_value).lower() in truthy_values
        matches = actual_bool == expected_bool
        message = "Exists" if actual_bool else "Does not exist"
        return matches, message, actual_bool

    if result_type == "integer":
        try:
            actual_int = int(result)
            expected_int = int(expected_value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid integer comparison: {exc}") from exc
        matches = actual_int == expected_int
        return matches, f"{actual_int}", actual_int

    if result_type == "string":
        # Consistent None handling
        actual_str = str(result).strip().lower() if result is not None else ""
        expected_str = (
            str(expected_value).strip().lower() if expected_value is not None else ""
        )
        matches = actual_str == expected_str
        return matches, str(result), result

    matches = str(result) == str(expected_value)
    return matches, str(result), result


async def workday_help_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Run a Workday Help field check against snapshot database state."""

    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version
    verifier_values = input.verifier.verifier_values

    # Get field selection and strip arrow notation if present
    field_to_check_raw = verifier_values.get("field_to_check")
    expected_value = verifier_values.get("expected_value")

    if not field_to_check_raw:
        raise ValueError("field_to_check is required")
    if expected_value is None:
        raise ValueError("expected_value is required")

    # Strip arrow notation (e.g., "Case Status → Case ID" -> "Case Status")
    field_to_check = field_to_check_raw.split(" → ")[0].strip()

    if field_to_check not in FIELD_MAPPINGS:
        raise ValueError(f"Unknown field: '{field_to_check}'")

    field_config = FIELD_MAPPINGS[field_to_check]
    params, missing = _build_params(verifier_values, field_config["required_params"])
    if missing:
        raise ValueError(f"Missing parameters: {', '.join(missing)}")

    conn, helper_error = _get_connection_from_helpers(input)
    temp_path = None
    if conn is None:
        # Try loading directly from snapshot as fallback
        conn, temp_path, snapshot_error = _load_snapshot_db(input.final_snapshot_bytes)
        if conn is None:
            # Prefer helper error message (more informative about available DBs)
            raise ValueError(
                helper_error or snapshot_error or "No database connection available"
            )

    try:
        cursor = conn.cursor()
        result_row = cursor.execute(field_config["query"], params).fetchone()
        result = result_row[0] if result_row else None
        logger.info(f"Workday Help field check: {field_to_check} -> {result}")
    except Exception as exc:
        raise ValueError(f"Query execution failed: {exc}") from exc
    finally:
        if temp_path:
            try:
                conn.close()
            except Exception:
                logger.warning("Failed to close database connection")
            try:
                os.unlink(temp_path)
            except OSError:
                logger.warning("Failed to delete temp db file")

    try:
        matches, result_message, actual_value = _compare_result(
            result,
            expected_value,
            field_config["result_type"],
        )
    except ValueError:
        raise

    message = f"{field_to_check}: {result_message}"
    if field_config["result_type"] in {"string", "integer"} and not matches:
        message = f"{field_to_check}: {result_message} (expected {expected_value})"

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=1.0 if matches else 0.0,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "actual_value": None if actual_value is None else str(actual_value),
            "expected_value": None if expected_value is None else str(expected_value),
            "field_checked": field_to_check,
            "matches": matches,
        },
        message=message,
    )
