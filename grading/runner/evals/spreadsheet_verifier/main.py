"""Spreadsheet Verifier - validates cell values and formatting in CSV/Excel files."""

import io
import zipfile
from pathlib import Path

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus

from .config import CheckResult, SpreadsheetVerifierConfig, SpreadsheetVerifierOutput
from .file_utils import (
    find_matching_files,
    get_worksheet,
    is_excel_file,
    load_spreadsheet_data,
    load_workbook_from_bytes,
)
from .formatting_checker import run_formatting_checks
from .value_checker import run_value_checks


async def spreadsheet_verifier_eval(input: EvalImplInput) -> VerifierResult:
    """Verify cell values and formatting in CSV/Excel files.

    Configuration fields:
    - file_pattern: Pattern to match files (e.g., "*.csv", "output.xlsx")
    - sheet_name: Sheet name for Excel files (optional, defaults to first sheet)
    - checks: List of value checks, each with:
        - cell_reference: Cell reference (e.g., "A1", "B2:B10")
        - check_type: Type of check (exact_match, contains, regex, etc.)
        - expected_value: Expected value for comparison
        - expected_min/expected_max: For numeric range checks
        - case_sensitive: Whether string comparisons are case-sensitive
    - formatting_checks: List of formatting checks, each with:
        - cell_reference: Cell reference (e.g., "A1", "B2:D10")
        - expected_highlight: Expected fill color (hex, e.g. "#FFFF00")
        - expected_text_color: Expected font color (hex, e.g. "#FF0000")
        - expected_bold: Whether cells should be bold
        - expected_formula: Expected formula (e.g., "=SUM(A1:A10)")

    Supported file types: CSV (.csv), Excel (.xlsx, .xlsm, .xls)
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version
    task_id = input.verifier.task_id or "unknown"

    config = SpreadsheetVerifierConfig.from_verifier_values(
        input.verifier.verifier_values
    )

    if not config.has_checks():
        return _error_result(verifier_id, verifier_version, "No checks configured")

    logger.info(
        f"[SPREADSHEET_VERIFIER] task={task_id} | pattern={config.file_pattern} | "
        f"sheet={config.sheet_name} | value_checks={len(config.value_checks)} | "
        f"formatting_checks={len(config.formatting_checks)}"
    )

    try:
        input.final_snapshot_bytes.seek(0)

        with zipfile.ZipFile(input.final_snapshot_bytes, "r") as zf:
            matching_files = find_matching_files(zf, config.file_pattern)

            if not matching_files:
                return _build_result(
                    verifier_id,
                    verifier_version,
                    SpreadsheetVerifierOutput(
                        passed=False,
                        reason=f"No files matching pattern '{config.file_pattern}' found",
                    ),
                )

            file_path = matching_files[0]
            file_ext = Path(file_path).suffix.lower()

            if config.requires_excel() and not is_excel_file(file_ext):
                return _error_result(
                    verifier_id,
                    verifier_version,
                    f"Formatting checks require Excel file (.xlsx/.xlsm/.xls), got '{file_ext}'",
                )

            logger.info(
                f"[SPREADSHEET_VERIFIER] task={task_id} | Loading file: {file_path}"
            )

            file_bytes_raw = zf.read(file_path)
            output = _run_checks(file_bytes_raw, file_ext, file_path, config, task_id)

        input.final_snapshot_bytes.seek(0)
        return _build_result(verifier_id, verifier_version, output)

    except zipfile.BadZipFile as e:
        return _error_result(
            verifier_id, verifier_version, f"Invalid snapshot ZIP: {e}"
        )
    except Exception as e:
        logger.exception(f"[SPREADSHEET_VERIFIER] task={task_id} | Error: {e}")
        return _error_result(verifier_id, verifier_version, str(e))


def _run_checks(
    file_bytes: bytes,
    file_ext: str,
    file_path: str,
    config: SpreadsheetVerifierConfig,
    task_id: str,
) -> SpreadsheetVerifierOutput:
    """Run all configured checks and return output."""
    value_results = []
    formatting_results = []

    if config.value_checks:
        data = load_spreadsheet_data(
            io.BytesIO(file_bytes), file_ext, config.sheet_name
        )
        logger.info(
            f"[SPREADSHEET_VERIFIER] task={task_id} | "
            f"Loaded {data.num_rows} rows, {data.num_cols} columns"
        )
        value_results = run_value_checks(data, config.value_checks, task_id)

    if config.formatting_checks:
        workbook = load_workbook_from_bytes(file_bytes)
        try:
            worksheet, error = get_worksheet(workbook, config.sheet_name)
            if error or worksheet is None:
                return SpreadsheetVerifierOutput(
                    passed=False,
                    reason=error or "Could not get worksheet",
                    file_checked=file_path,
                    sheet_name=config.sheet_name,
                )
            formatting_results = run_formatting_checks(
                workbook, worksheet, config.formatting_checks, task_id
            )
        finally:
            workbook.close()

    def _summarize(results: list[CheckResult]) -> tuple[int, int, list[str]]:
        """Return (passed_count, total, failed_reasons)."""
        passed_count = 0
        failed_reasons: list[str] = []
        for r in results:
            if r.passed:
                passed_count += 1
            else:
                failed_reasons.append(f"{r.cell_reference}: {r.reason}")
        return passed_count, len(results), failed_reasons

    value_passed, value_total, value_failed = _summarize(value_results)
    fmt_passed, fmt_total, fmt_failed = _summarize(formatting_results)

    all_passed = (value_passed == value_total) and (fmt_passed == fmt_total)

    summary_parts = []
    if value_results:
        summary_parts.append(f"Values: {value_passed}/{value_total} passed")
    if formatting_results:
        summary_parts.append(f"Formatting: {fmt_passed}/{fmt_total} passed")

    summary = " | ".join(summary_parts) if summary_parts else "No checks performed"

    if not all_passed:
        failed = value_failed + fmt_failed
        if failed:
            summary += f" | Failed: {'; '.join(failed[:3])}"
            if len(failed) > 3:
                summary += f" (+{len(failed) - 3} more)"

    return SpreadsheetVerifierOutput(
        passed=all_passed,
        reason=summary,
        file_checked=file_path,
        sheet_name=config.sheet_name,
        value_check_results=value_results,
        formatting_check_results=formatting_results,
    )


def _build_result(
    verifier_id: str,
    verifier_version: int | None,
    output: SpreadsheetVerifierOutput,
) -> VerifierResult:
    """Build VerifierResult from output."""
    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version or 0,
        score=1.0 if output.passed else 0.0,
        verifier_result_values=output.to_dict(),
        status=VerifierResultStatus.OK,
        message=output.reason,
    )


def _error_result(
    verifier_id: str,
    verifier_version: int | None,
    message: str,
) -> VerifierResult:
    """Build error VerifierResult."""
    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version or 0,
        score=0.0,
        verifier_result_values={"passed": False, "error": message},
        status=VerifierResultStatus.ERROR,
        message=message,
    )
