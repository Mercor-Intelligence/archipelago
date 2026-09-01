"""Value check execution for spreadsheet verifier."""

import re
from typing import Any

from loguru import logger

from .cell_utils import (
    SpreadsheetData,
    expand_cell_range,
    get_cell_value,
    parse_cell_range,
)
from .config import CheckResult, ValueCheck, ValueCheckType


def run_value_checks(
    data: SpreadsheetData,
    checks: list[ValueCheck],
    task_id: str = "unknown",
) -> list[CheckResult]:
    """Execute all value checks against spreadsheet data.

    Args:
        data: SpreadsheetData with cell values
        checks: List of ValueCheck configurations
        task_id: Task ID for logging

    Returns:
        List of CheckResult objects.
    """
    results: list[CheckResult] = []

    for check in checks:
        cell_range = parse_cell_range(check.cell_reference)
        if not cell_range:
            results.append(
                CheckResult(
                    cell_reference=check.cell_reference,
                    passed=False,
                    reason=f"Invalid cell reference: '{check.cell_reference}'",
                    check_type=check.check_type.value,
                )
            )
            continue

        # Reject sheet prefixes in cell references - use config.sheet_name instead
        if cell_range.start.sheet is not None:
            results.append(
                CheckResult(
                    cell_reference=check.cell_reference,
                    passed=False,
                    reason=(
                        f"Sheet prefix '{cell_range.start.sheet}!' in cell reference is not "
                        f"supported. Use 'sheet_name' in config to specify the target sheet, "
                        f"and use simple cell references like 'A1' or 'B2:B10'."
                    ),
                    check_type=check.check_type.value,
                )
            )
            continue

        cells = expand_cell_range(cell_range)

        for cell in cells:
            actual_value, error = get_cell_value(data, cell)
            cell_ref = str(cell)

            if error:
                results.append(
                    CheckResult(
                        cell_reference=cell_ref,
                        passed=False,
                        reason=error,
                        check_type=check.check_type.value,
                    )
                )
                continue

            passed, reason = _execute_value_check(
                actual_value=actual_value,
                check=check,
            )

            results.append(
                CheckResult(
                    cell_reference=cell_ref,
                    passed=passed,
                    reason=reason,
                    check_type=check.check_type.value,
                    expected=check.expected_value,
                    actual=str(actual_value) if actual_value is not None else None,
                )
            )

            logger.info(
                f"[SPREADSHEET_VERIFIER] task={task_id} | Value Check: "
                f"cell={cell_ref}, type={check.check_type.value}, "
                f"passed={passed} | {reason}"
            )

    return results


def _execute_value_check(actual_value: Any, check: ValueCheck) -> tuple[bool, str]:
    """Execute a single value check.

    Returns:
        Tuple of (passed, reason).
    """
    check_type = check.check_type
    expected_value = check.expected_value
    expected_min = check.expected_min
    expected_max = check.expected_max
    case_sensitive = check.case_sensitive

    actual_is_empty = actual_value is None or (
        isinstance(actual_value, str) and not actual_value.strip()
    )

    if check_type == ValueCheckType.IS_EMPTY:
        passed = actual_is_empty
        return passed, f"Value is {'empty' if actual_is_empty else 'not empty'}"

    if check_type == ValueCheckType.NOT_EMPTY:
        passed = not actual_is_empty
        return passed, f"Value is {'not empty' if not actual_is_empty else 'empty'}"

    actual_str = "" if actual_is_empty else str(actual_value)

    if check_type == ValueCheckType.EXACT_MATCH:
        expected = expected_value or ""
        if case_sensitive:
            passed = actual_str == expected
        else:
            passed = actual_str.lower() == expected.lower()
        return passed, f"Expected '{expected}', got '{actual_str}'"

    if check_type == ValueCheckType.CONTAINS:
        if expected_value is None:
            return False, "expected_value required for contains check"
        if case_sensitive:
            passed = expected_value in actual_str
        else:
            passed = expected_value.lower() in actual_str.lower()
        return (
            passed,
            f"'{actual_str}' {'contains' if passed else 'does not contain'} '{expected_value}'",
        )

    if check_type == ValueCheckType.NOT_CONTAINS:
        if expected_value is None:
            return False, "expected_value required for not_contains check"
        if case_sensitive:
            passed = expected_value not in actual_str
        else:
            passed = expected_value.lower() not in actual_str.lower()
        return (
            passed,
            f"'{actual_str}' {'does not contain' if passed else 'contains'} '{expected_value}'",
        )

    if check_type == ValueCheckType.REGEX:
        if expected_value is None:
            return False, "expected_value required for regex check"
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            passed = bool(re.search(expected_value, actual_str, flags))
            return (
                passed,
                f"'{actual_str}' {'matches' if passed else 'does not match'} pattern '{expected_value}'",
            )
        except re.error as e:
            return False, f"Invalid regex pattern '{expected_value}': {e}"

    if check_type in (
        ValueCheckType.EQUALS_NUMBER,
        ValueCheckType.GREATER_THAN,
        ValueCheckType.LESS_THAN,
        ValueCheckType.BETWEEN,
    ):
        try:
            cleaned = re.sub(r"[$€£¥,\s]", "", actual_str)
            if cleaned.endswith("%"):
                actual_num = float(cleaned[:-1]) / 100
            else:
                actual_num = float(cleaned)
        except (ValueError, TypeError):
            return False, f"Cannot convert '{actual_str}' to number"

        if check_type == ValueCheckType.EQUALS_NUMBER:
            if expected_value is None:
                return False, "expected_value required for equals_number check"
            try:
                expected_num = float(expected_value)
            except (ValueError, TypeError):
                return False, f"Invalid expected_value: '{expected_value}'"
            tolerance = abs(expected_num) * 0.0001 if expected_num != 0 else 0.0001
            passed = abs(actual_num - expected_num) <= tolerance
            return passed, f"Expected {expected_num}, got {actual_num}"

        if check_type == ValueCheckType.GREATER_THAN:
            if expected_min is None:
                return False, "expected_min required for greater_than check"
            passed = actual_num > expected_min
            return passed, f"{actual_num} {'>' if passed else '<='} {expected_min}"

        if check_type == ValueCheckType.LESS_THAN:
            if expected_max is None:
                return False, "expected_max required for less_than check"
            passed = actual_num < expected_max
            return passed, f"{actual_num} {'<' if passed else '>='} {expected_max}"

        if check_type == ValueCheckType.BETWEEN:
            if expected_min is None or expected_max is None:
                return False, "expected_min and expected_max required for between check"
            passed = expected_min <= actual_num <= expected_max
            return (
                passed,
                f"{expected_min} <= {actual_num} <= {expected_max} is {passed}",
            )

    return False, f"Unknown check type: {check_type}"
