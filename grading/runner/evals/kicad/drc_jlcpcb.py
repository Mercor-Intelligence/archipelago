"""KiCad JLCPCB DRC Verifier.

Reads the DRC report from the snapshot (expected to have been generated
with JLCPCB design rules loaded) and checks for manufacturing compliance.
"""

import zipfile

from runner.evals.kicad.common import find_report
from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus


async def kicad_drc_jlcpcb_eval(input: EvalImplInput) -> VerifierResult:
    """Verify PCB passes DRC with JLCPCB manufacturing constraints.

    Reads the DRC report JSON from the snapshot. The snapshot should have been
    generated with JLCPCB .kicad_dru rules loaded via set_design_rules.

    Score is continuous: max(0, 1 - errors/10) for partial credit.
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version
    project_name = input.verifier.verifier_values.get("project_name")

    try:
        max_errors = int(input.verifier.verifier_values.get("max_errors", "0"))
    except (ValueError, TypeError):
        max_errors = 0
    max_warnings_raw = input.verifier.verifier_values.get("max_warnings")
    max_warnings: int | None = None
    if max_warnings_raw is not None:
        try:
            max_warnings = int(max_warnings_raw)
        except (ValueError, TypeError):
            max_warnings = None

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
            report = find_report(snapshot_zip, "_drc.json", project_name)
    except zipfile.BadZipFile:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Invalid snapshot zip file",
        )

    if report is None:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="No DRC report found in snapshot",
        )

    violations = report.get("violations", [])
    errors = sum(1 for v in violations if v.get("severity") == "error")
    warnings = sum(1 for v in violations if v.get("severity") == "warning")
    total = len(violations)

    passes = errors <= max_errors
    if max_warnings is not None:
        passes = passes and warnings <= max_warnings

    # Continuous scoring: errors and warnings both degrade the score.
    # Errors weighted 1x, warnings weighted 0.2x. Divisor is at least 10
    # so the default (max_errors=0) still gives a smooth gradient.
    error_divisor = max(max_errors * 2, 10)
    warning_divisor = max(int(max_warnings * 2) if max_warnings is not None else 50, 50)
    error_penalty = errors / error_divisor
    warning_penalty = warnings / warning_divisor * 0.2
    score = max(0.0, 1.0 - error_penalty - warning_penalty)

    violations_summary = "; ".join(
        f"[{v.get('severity', '?')}] {v.get('description', 'unknown')[:80]}"
        for v in violations[:5]
    )
    if total > 5:
        violations_summary += f" ... and {total - 5} more"

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=score,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "passes": passes,
            "error_count": errors,
            "warning_count": warnings,
            "total_violations": total,
            "violations_summary": violations_summary,
        },
        message=f"DRC JLCPCB: {errors} errors, {warnings} warnings {'PASS' if passes else 'FAIL'}",
    )
