"""OpenEMR Clinical Verification - Self-contained verifier for clinical encounters.

This verifier extracts data directly from the snapshot (no external helper needed).
Includes database extraction logic inline to keep the verifier self-contained.
"""

import io
import json
import os
import sqlite3
import tempfile
import zipfile
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus

from .llm_judge import LLMJudge, MockLLMJudge
from .runner import VerificationConfig

# =============================================================================
# Database Extraction Logic (inline, no external helper)
# =============================================================================


def _extract_notes(conn: sqlite3.Connection, encounter_id: int) -> list[dict[str, Any]]:
    """Extract clinical notes for an encounter."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT id, note_type, content, created_at
            FROM notes
            WHERE encounter_id = ?
            ORDER BY created_at
            """,
            (encounter_id,),
        )
        notes = cursor.fetchall()

        return [
            {
                "id": note[0],
                "note_type": note[1],
                "content": note[2],
                "created_at": str(note[3]) if note[3] else None,
            }
            for note in notes
        ]
    except sqlite3.OperationalError as e:
        logger.warning(f"Error extracting notes: {e}")
        return []


def _extract_orders(
    conn: sqlite3.Connection, encounter_id: int
) -> list[dict[str, Any]]:
    """Extract orders for an encounter."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT id, order_type, description, status, priority, created_at
            FROM orders
            WHERE encounter_id = ?
            ORDER BY created_at
            """,
            (encounter_id,),
        )
        orders = cursor.fetchall()

        return [
            {
                "id": order[0],
                "order_type": order[1],
                "description": order[2],
                "order_name": order[2],  # Alias for compatibility
                "status": order[3],
                "priority": order[4],
                "created_at": str(order[5]) if order[5] else None,
            }
            for order in orders
        ]
    except sqlite3.OperationalError as e:
        logger.warning(f"Error extracting orders: {e}")
        return []


def _extract_differential(
    conn: sqlite3.Connection, encounter_id: int
) -> dict[str, Any] | None:
    """Extract differential diagnosis for an encounter."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT id, diagnoses, reasoning, created_at
            FROM differentials
            WHERE encounter_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (encounter_id,),
        )
        diff = cursor.fetchone()

        if diff:
            diagnoses = diff[1]
            if isinstance(diagnoses, str):
                try:
                    diagnoses = json.loads(diagnoses)
                except json.JSONDecodeError:
                    diagnoses = [diagnoses]

            return {
                "id": diff[0],
                "diagnoses": diagnoses,
                "reasoning": diff[2],
                "created_at": str(diff[3]) if diff[3] else None,
            }
        return None
    except sqlite3.OperationalError as e:
        logger.warning(f"Error extracting differential: {e}")
        return None


def _extract_medications(
    conn: sqlite3.Connection, encounter_id: int
) -> list[dict[str, Any]]:
    """Extract medications for an encounter."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT id, medication_name, dose, frequency, is_active
            FROM medications
            WHERE encounter_id = ?
            """,
            (encounter_id,),
        )
        meds = cursor.fetchall()

        return [
            {
                "id": med[0],
                "medication_name": med[1],
                "dose": med[2],
                "frequency": med[3],
                "is_active": med[4],
                "status": "active" if med[4] else "inactive",
            }
            for med in meds
        ]
    except sqlite3.OperationalError as e:
        logger.warning(f"Error extracting medications: {e}")
        return []


def _extract_lab_results(
    conn: sqlite3.Connection, patient_id: str
) -> list[dict[str, Any]]:
    """Extract lab results for a patient."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT id, panel_name, analytes_json, status
            FROM lab_results
            WHERE patient_id = ?
            """,
            (patient_id,),
        )
        results = cursor.fetchall()

        parsed_results = []
        for result in results:
            analytes = result[2]
            if isinstance(analytes, str):
                try:
                    analytes = json.loads(analytes)
                except json.JSONDecodeError:
                    analytes = []

            parsed_results.append(
                {
                    "id": result[0],
                    "panel_name": result[1],
                    "analytes": analytes,
                    "results": analytes,  # Alias
                    "status": result[3],
                }
            )

        return parsed_results
    except sqlite3.OperationalError as e:
        logger.warning(f"Error extracting lab results: {e}")
        return []


def _extract_encounter(
    conn: sqlite3.Connection, encounter_id: int
) -> dict[str, Any] | None:
    """Extract encounter details."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT id, pid, status, reason, diagnoses, created_at
            FROM form_encounter
            WHERE id = ?
            """,
            (encounter_id,),
        )
        encounter = cursor.fetchone()

        if encounter:
            diagnoses = encounter[4]
            if isinstance(diagnoses, str):
                try:
                    diagnoses = json.loads(diagnoses)
                except json.JSONDecodeError:
                    diagnoses = [diagnoses] if diagnoses else []

            return {
                "id": encounter[0],
                "patient_id": encounter[1],
                "status": encounter[2],
                "reason": encounter[3],
                "diagnoses": diagnoses,
                "created_at": str(encounter[5]) if encounter[5] else None,
            }
        return None
    except sqlite3.OperationalError as e:
        logger.warning(f"Error extracting encounter: {e}")
        return None


def _run_sql_checks(
    notes: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    differential: dict[str, Any] | None,
    medications: list[dict[str, Any]],
    lab_results: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Run SQL verification checks and return results."""
    sql_checks: dict[str, dict[str, Any]] = {}

    sql_checks["notes_added"] = {
        "passed": len(notes) > 0,
        "message": f"Found {len(notes)} note(s)" if notes else "No notes found",
        "details": {
            "note_count": len(notes),
            "note_types": [n["note_type"] for n in notes],
        },
    }

    sql_checks["orders_placed"] = {
        "passed": len(orders) > 0,
        "message": f"Found {len(orders)} order(s)" if orders else "No orders found",
        "details": {
            "order_count": len(orders),
            "order_types": list({o["order_type"] for o in orders}),
        },
    }

    sql_checks["differential_documented"] = {
        "passed": differential is not None
        and len(differential.get("diagnoses", [])) > 0,
        "message": (
            f"Differential with {len(differential.get('diagnoses', []))} diagnoses"
            if differential
            else "No differential documented"
        ),
        "details": {
            "has_differential": differential is not None,
            "diagnosis_count": len(differential.get("diagnoses", []))
            if differential
            else 0,
            "has_reasoning": bool(differential.get("reasoning"))
            if differential
            else False,
        },
    }

    sql_checks["medications_prescribed"] = {
        "passed": len(medications) > 0,
        "message": (
            f"Found {len(medications)} medication(s)"
            if medications
            else "No medications prescribed"
        ),
        "details": {"medication_count": len(medications)},
    }

    sql_checks["lab_results_reviewed"] = {
        "passed": len(lab_results) > 0,
        "message": (
            f"Found {len(lab_results)} lab result(s)"
            if lab_results
            else "No lab results found"
        ),
        "details": {
            "result_count": len(lab_results),
            "panels": [r["panel_name"] for r in lab_results],
        },
    }

    return sql_checks


def _extract_data_from_snapshot(
    final_snapshot_bytes: io.BytesIO,
    encounter_id: int | None = None,
) -> dict[str, Any]:
    """Extract OpenEMR data directly from snapshot zip.

    This is the main extraction function that reads the SQLite database
    from the snapshot and extracts all clinical encounter data.

    Args:
        final_snapshot_bytes: BytesIO containing the snapshot zip file
        encounter_id: Optional encounter ID to filter by (uses most recent if not specified)

    Returns:
        Dictionary with encounter data, notes, orders, differential, etc.
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
                return {
                    "error": "No database file found in snapshot",
                    "encounter": None,
                    "notes": [],
                    "orders": [],
                    "differential": None,
                    "medications": [],
                    "lab_results": [],
                    "sql_checks": {},
                }

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
                logger.info(f"Parsing OpenEMR data from database: {db_file}")

                try:
                    cursor = conn.cursor()

                    # Get encounter list for reference (recent 10)
                    try:
                        cursor.execute(
                            "SELECT id, pid FROM form_encounter ORDER BY id DESC LIMIT 10"
                        )
                        encounters = cursor.fetchall()
                        encounter_list = [
                            {"id": e[0], "patient_id": e[1]} for e in encounters
                        ]
                    except sqlite3.OperationalError:
                        encounter_list = []

                    # Determine target encounter
                    target_encounter_id = None
                    target_patient_id = None

                    if encounter_id is not None:
                        # Query directly for the specified encounter (not limited to top 10)
                        try:
                            cursor.execute(
                                "SELECT id, pid FROM form_encounter WHERE id = ?",
                                (encounter_id,),
                            )
                            row = cursor.fetchone()
                            if row:
                                target_encounter_id = row[0]
                                target_patient_id = row[1]
                            else:
                                logger.warning(
                                    f"Encounter ID {encounter_id} not found in database"
                                )
                        except sqlite3.OperationalError as e:
                            logger.warning(
                                f"Error querying encounter {encounter_id}: {e}"
                            )

                    # Fall back to most recent if no specific encounter found
                    if target_encounter_id is None and encounter_list:
                        target_encounter_id = encounter_list[0]["id"]
                        target_patient_id = encounter_list[0]["patient_id"]
                        if encounter_id is not None:
                            logger.warning(
                                f"Falling back to most recent encounter {target_encounter_id}"
                            )

                    if (
                        target_encounter_id is not None
                        and target_patient_id is not None
                    ):
                        encounter = _extract_encounter(conn, target_encounter_id)
                        notes = _extract_notes(conn, target_encounter_id)
                        orders = _extract_orders(conn, target_encounter_id)
                        differential = _extract_differential(conn, target_encounter_id)
                        medications = _extract_medications(conn, target_encounter_id)
                        lab_results = _extract_lab_results(conn, str(target_patient_id))

                        sql_checks = _run_sql_checks(
                            notes, orders, differential, medications, lab_results
                        )
                    else:
                        encounter = None
                        notes = []
                        orders = []
                        differential = None
                        medications = []
                        lab_results = []
                        sql_checks = {}
                finally:
                    conn.close()

                result = {
                    "available_encounters": encounter_list,
                    "encounter": encounter,
                    "encounter_id": encounter["id"] if encounter else None,
                    "patient_id": encounter["patient_id"] if encounter else None,
                    "chief_complaint": encounter.get("reason", "") if encounter else "",
                    "notes": notes,
                    "orders": orders,
                    "differential": differential,
                    "medications": medications,
                    "lab_results": lab_results,
                    "sql_checks": sql_checks,
                    "sql_feedback": [
                        c["message"]
                        for c in sql_checks.values()
                        if not c.get("passed", True)
                    ],
                    "diagnoses": encounter.get("diagnoses", []) if encounter else [],
                    "db_file": db_file,
                }

            finally:
                try:
                    os.unlink(temp_file_path)
                except OSError as e:
                    logger.warning(f"Failed to delete temp file {temp_file_path}: {e}")

    except zipfile.BadZipFile as e:
        logger.error(f"Invalid snapshot zip file: {e}")
        return {
            "error": f"Invalid snapshot zip file: {e}",
            "encounter": None,
            "notes": [],
            "orders": [],
            "differential": None,
            "medications": [],
            "lab_results": [],
            "sql_checks": {},
        }

    final_snapshot_bytes.seek(0)
    return result


# =============================================================================
# Main Verifier Function
# =============================================================================


async def openemr_clinical_verification_eval(input: EvalImplInput) -> VerifierResult:
    """Verify clinical encounter quality using SQL checks + LLM evaluation.

    This verifier is self-contained and extracts data directly from the snapshot.
    No external helper is required.

    Verification layers:
    1. SQL checks - Database state verification
    2. LLM Judge - Quality and reasoning evaluation
    3. Human rubric (handled separately)

    Args:
        input: Framework-provided evaluation input with snapshots

    Returns:
        VerifierResult with score and detailed result values
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version

    # Helper function for standardized error results
    def error_result(
        message: str, error_details: dict[str, Any] | None = None
    ) -> VerifierResult:
        """Create a standardized error result."""
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            message=message,
            verifier_result_values=error_details or {"error": message},
        )

    try:
        # Early validation: Check required inputs
        if not input.final_snapshot_bytes:
            return error_result(
                "Missing final snapshot - cannot verify without snapshot data"
            )

        if not hasattr(input.verifier, "verifier_values"):
            return error_result("Missing verifier configuration values")

        # Extract config from UI (verifier_values is the correct attribute)
        config_dict = input.verifier.verifier_values
        encounter_id_filter = config_dict.get("encounter_id")  # Optional filter

        # Validate encounter_id if provided
        if encounter_id_filter is not None:
            # Validate it's a true integer (reject floats with fractional parts)
            # Check for bool first since bool is a subclass of int in Python
            if isinstance(encounter_id_filter, bool):
                return error_result(
                    f"Invalid encounter_id: must be integer, got bool ({encounter_id_filter})"
                )
            elif isinstance(encounter_id_filter, float):
                if not encounter_id_filter.is_integer():
                    return error_result(
                        f"Invalid encounter_id: must be integer, got float {encounter_id_filter}"
                    )
                encounter_id_filter = int(encounter_id_filter)
            elif isinstance(encounter_id_filter, str):
                # Reject strings that look like floats
                if "." in encounter_id_filter:
                    return error_result(
                        f"Invalid encounter_id: must be integer, got '{encounter_id_filter}'"
                    )
                try:
                    encounter_id_filter = int(encounter_id_filter)
                except ValueError:
                    return error_result(
                        f"Invalid encounter_id: must be integer, got '{encounter_id_filter}'"
                    )
            elif isinstance(encounter_id_filter, int):
                pass  # Already an int, good to go
            else:
                return error_result(
                    f"Invalid encounter_id: must be integer, got {type(encounter_id_filter).__name__}"
                )

            # Check if positive
            if encounter_id_filter <= 0:
                return error_result(
                    f"Invalid encounter_id: must be positive integer, got {encounter_id_filter}"
                )

        ground_truth = config_dict.get("ground_truth_diagnosis")
        check_differential = config_dict.get("check_differential", True)
        use_llm = config_dict.get("use_llm", True)
        use_mock_llm = config_dict.get("use_mock_llm", False)

        # Extract data directly from snapshot (no helper needed)
        encounter_data = _extract_data_from_snapshot(
            input.final_snapshot_bytes,
            encounter_id=encounter_id_filter,
        )

        # Check for extraction errors (safe null handling)
        if encounter_data and encounter_data.get("error"):
            return error_result(
                f"Data extraction error: {encounter_data['error']}", encounter_data
            )

        if not encounter_data or not encounter_data.get("encounter"):
            # Safely extract available_encounters, handling None or non-dict cases
            available_encounters = (
                encounter_data.get("available_encounters", []) if encounter_data else []
            )
            return error_result(
                "No encounter data found. Check snapshot contains OpenEMR database.",
                {
                    "error": "Missing or empty encounter data",
                    "available_encounters": available_encounters,
                },
            )

        encounter_id = encounter_data.get("encounter_id")

        # Create verification config
        verification_config = VerificationConfig(
            check_differential=check_differential,
            use_llm=use_llm,
            use_mock_llm=use_mock_llm,
            true_diagnosis=ground_truth,
            sql_weight=0.4,
            llm_weight=0.6,
        )

        # Get SQL checks from extraction
        sql_checks_raw = encounter_data.get("sql_checks") or {}
        sql_feedback = encounter_data.get("sql_feedback") or []

        sql_checks = {
            name: check.get("passed", False) if isinstance(check, dict) else check
            for name, check in sql_checks_raw.items()
        }

        # Calculate SQL score
        sql_score = (
            sum(1 for passed in sql_checks.values() if passed) / len(sql_checks) * 100
            if sql_checks
            else 0.0
        )

        # Run LLM verifications if enabled
        llm_score = 0.0
        llm_scores = {}
        llm_feedback: list[str] = []

        if use_llm:
            # Use model from grading_settings (standard infrastructure)
            model = input.grading_settings.llm_judge_model
            extra_args = input.grading_settings.llm_judge_extra_args
            llm_judge = (
                MockLLMJudge(
                    model=model,
                    pass_threshold=verification_config.llm_pass_threshold,
                    extra_args=extra_args,
                )
                if use_mock_llm
                else LLMJudge(
                    model=model,
                    pass_threshold=verification_config.llm_pass_threshold,
                    extra_args=extra_args,
                )
            )

            note_content = "\n\n".join(
                (n.get("content") or "") for n in (encounter_data.get("notes") or [])
            )
            differential_list = (encounter_data.get("differential") or {}).get(
                "diagnoses", []
            )
            differential_reasoning = (encounter_data.get("differential") or {}).get(
                "reasoning"
            )
            orders = encounter_data.get("orders") or []
            symptoms = encounter_data.get("chief_complaint") or ""
            diagnoses = encounter_data.get("diagnoses") or []
            final_diagnosis = diagnoses[0] if diagnoses else None

            try:
                llm_results = await llm_judge.evaluate_overall_encounter(
                    note_content=note_content,
                    differential=differential_list,
                    orders=orders,
                    symptoms=symptoms,
                    final_diagnosis=final_diagnosis,
                    true_diagnosis=ground_truth,
                    reasoning=differential_reasoning,
                )

                total_score = 0.0
                for name, llm_result in llm_results.items():
                    llm_scores[name] = llm_result.score
                    total_score += llm_result.score
                    if not llm_result.passed:
                        llm_feedback.append(f"[LLM/{name}] {llm_result.feedback}")

                llm_score = total_score / len(llm_results) if llm_results else 0.0

            except Exception as e:
                logger.error(f"LLM evaluation failed: {e}")
                return error_result(
                    f"LLM evaluation failed: {e!s}",
                    {
                        "error": str(e),
                        "sql_score": sql_score,
                        "sql_checks": sql_checks,
                        "note": "SQL checks completed successfully, but LLM evaluation failed",
                    },
                )

        # Calculate overall score
        if use_llm:
            overall_score = (
                sql_score * verification_config.sql_weight
                + llm_score * verification_config.llm_weight
            )
        else:
            overall_score = sql_score

        # Determine pass/fail
        critical_sql_checks = ["notes_added"]
        if check_differential:
            critical_sql_checks.append("differential_documented")

        critical_passed = all(sql_checks.get(c, False) for c in critical_sql_checks)
        overall_pass = critical_passed and overall_score >= 60

        # Build feedback
        all_feedback = sql_feedback + llm_feedback
        feedback_text = "\n".join(all_feedback) if all_feedback else "All checks passed"

        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=overall_score / 100.0,
            status=VerifierResultStatus.OK,
            message=f"Clinical verification: {overall_score:.1f}/100 {'PASS' if overall_pass else 'FAIL'}",
            verifier_result_values={
                "encounter_id": encounter_id,
                "passed": overall_pass,
                "overall_score": overall_score,
                "sql_score": sql_score,
                "llm_score": llm_score,
                "sql_checks": sql_checks,
                "llm_scores": llm_scores,
                "feedback": feedback_text,
                "critical_checks_passed": critical_passed,
            },
        )

    except Exception as e:
        logger.exception("Clinical verification failed unexpectedly")
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            message=f"Unexpected verification error: {e!s}",
            verifier_result_values={"error": str(e), "error_type": type(e).__name__},
        )
