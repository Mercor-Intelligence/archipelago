"""KiCad SPICE Check Verifier.

Reads pre-computed SPICE simulation results from the snapshot and verifies
circuit behavior against expected values (voltage, current, gain).
Simulation is run during snapshot generation, not during grading.
"""

import zipfile

from runner.evals.kicad.common import find_report
from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus


async def kicad_spice_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify circuit behavior from SPICE simulation results.

    Reads pre-computed simulation results from the snapshot. Checks that
    a specific node value (voltage, current, etc.) is within tolerance
    of the expected value.
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version

    node_name = input.verifier.verifier_values.get("node_name")
    expected_value = input.verifier.verifier_values.get("expected_value")
    try:
        tolerance_pct = float(
            input.verifier.verifier_values.get("tolerance_percent", "5")
        )
    except (ValueError, TypeError):
        tolerance_pct = 5.0
    project_name = input.verifier.verifier_values.get("project_name")

    if not node_name:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="node_name is required (e.g., 'V(vout)', 'I(R1)')",
        )

    if expected_value is None:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="expected_value is required",
        )

    try:
        expected_float = float(expected_value)
    except (ValueError, TypeError):
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"expected_value must be numeric, got: {expected_value}",
        )

    if not input.final_snapshot_bytes:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="No final snapshot available",
        )

    try:
        input.final_snapshot_bytes.seek(0)
        with zipfile.ZipFile(input.final_snapshot_bytes, "r") as snapshot_zip:
            results = find_report(
                snapshot_zip, "_results.json", project_name, directory="simulation/"
            )
    except zipfile.BadZipFile:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Invalid snapshot zip file",
        )

    if results is None:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="No SPICE simulation results found in snapshot. Ensure simulation ran during snapshot.",
        )

    values = results.get("values", {})
    actual = None
    node_key = node_name.lower().replace(" ", "")
    for key, val in values.items():
        if key.lower() == node_key or key.lower().replace(" ", "") == node_key:
            actual = val
            break

    if actual is None:
        all_nodes = list(values.keys())
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={
                "available_nodes": ", ".join(all_nodes[:20]),
            },
            message=f"Node '{node_name}' not found in simulation results. Available: {', '.join(all_nodes[:10])}",
        )

    if isinstance(actual, list):
        actual = actual[-1] if actual else 0.0

    try:
        actual_float = float(actual)
    except (TypeError, ValueError):
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"raw_value": str(actual)[:100]},
            message=f"Non-numeric value for node '{node_name}': {actual!r}",
        )
    # Clamp to non-negative so tolerance and scoring logic stay consistent
    tolerance_pct = max(tolerance_pct, 0.0)
    tolerance = (
        abs(expected_float) * tolerance_pct / 100.0
        if expected_float != 0
        else tolerance_pct / 100.0
    )
    within_tolerance = abs(actual_float - expected_float) <= tolerance

    if tolerance_pct == 0:
        score = 1.0 if actual_float == expected_float else 0.0
    elif expected_float != 0:
        error_pct = abs(actual_float - expected_float) / abs(expected_float) * 100
        score = max(0.0, 1.0 - error_pct / (tolerance_pct * 2))
    else:
        # expected is zero: use absolute error relative to tolerance for smooth scoring
        score = (
            max(0.0, 1.0 - abs(actual_float) / (tolerance * 2))
            if tolerance > 0
            else (1.0 if actual_float == 0.0 else 0.0)
        )

    score = round(max(0.0, min(1.0, score)), 4)

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=score,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "node_name": node_name,
            "actual_value": actual_float,
            "expected_value": expected_float,
            "tolerance_percent": tolerance_pct,
            "within_tolerance": within_tolerance,
            "simulation_type": results.get("type", "unknown"),
        },
        message=f"SPICE {node_name}: {actual_float:.4g} vs expected {expected_float:.4g} (±{tolerance_pct}%) {'PASS' if within_tolerance else 'FAIL'}",
    )
