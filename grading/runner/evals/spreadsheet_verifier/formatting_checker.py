"""Formatting check execution for spreadsheet verifier."""

import re
from typing import Any

from loguru import logger
from openpyxl.cell import Cell
from openpyxl.styles.colors import COLOR_INDEX, Color
from openpyxl.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from .cell_utils import get_cells_from_worksheet, parse_cell_range
from .config import CheckResult, FormattingCheck


def run_formatting_checks(
    workbook: Workbook,
    worksheet: Worksheet,
    checks: list[FormattingCheck],
    task_id: str = "unknown",
) -> list[CheckResult]:
    """Execute all formatting checks against worksheet.

    Args:
        workbook: openpyxl Workbook
        worksheet: openpyxl Worksheet
        checks: List of FormattingCheck configurations
        task_id: Task ID for logging

    Returns:
        List of CheckResult objects.
    """
    results: list[CheckResult] = []

    for check in checks:
        if not check.cell_reference:
            results.append(
                CheckResult(
                    cell_reference="",
                    passed=False,
                    reason="cell_reference is required for formatting checks",
                )
            )
            continue

        if not check.has_any_expectation():
            results.append(
                CheckResult(
                    cell_reference=check.cell_reference,
                    passed=False,
                    reason="At least one expected formatting property must be specified",
                )
            )
            continue

        cell_range = parse_cell_range(check.cell_reference)
        if not cell_range:
            results.append(
                CheckResult(
                    cell_reference=check.cell_reference,
                    passed=False,
                    reason=f"Invalid cell reference: '{check.cell_reference}'",
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
                )
            )
            continue

        cells = get_cells_from_worksheet(worksheet, cell_range, workbook)
        if not cells:
            results.append(
                CheckResult(
                    cell_reference=check.cell_reference,
                    passed=False,
                    reason=f"No cells found for reference: {check.cell_reference}",
                )
            )
            continue

        all_passed = True
        cell_details: dict[str, dict[str, Any]] = {}

        for cell in cells:
            cell_coord = cell.coordinate
            cell_result = _check_cell_formatting(cell, check)
            cell_details[cell_coord] = cell_result

            if not cell_result["passed"]:
                all_passed = False

        failed_cells = [
            coord for coord, res in cell_details.items() if not res["passed"]
        ]
        passed_count = len(cells) - len(failed_cells)

        if all_passed:
            reason = f"All {len(cells)} cell(s) passed formatting checks"
        else:
            reason = f"{len(failed_cells)}/{len(cells)} cell(s) failed: {', '.join(failed_cells[:5])}"
            if len(failed_cells) > 5:
                reason += f" (+{len(failed_cells) - 5} more)"

        results.append(
            CheckResult(
                cell_reference=check.cell_reference,
                passed=all_passed,
                reason=reason,
                details={
                    "cells_checked": len(cells),
                    "cells_passed": passed_count,
                    "cell_results": cell_details,
                },
            )
        )

        logger.info(
            f"[SPREADSHEET_VERIFIER] task={task_id} | Formatting Check: "
            f"ref={check.cell_reference}, passed={all_passed}, "
            f"cells={passed_count}/{len(cells)}"
        )

    return results


def _check_cell_formatting(cell: Cell, check: FormattingCheck) -> dict[str, Any]:
    """Check formatting for a single cell.

    Returns:
        Dict with check results per property and overall passed status.
    """
    result: dict[str, Any] = {"passed": True}

    if check.expected_highlight is not None:
        actual = get_fill_color(cell)
        match = colors_match(actual, check.expected_highlight)
        result["highlight"] = {
            "expected": check.expected_highlight,
            "actual": actual,
            "match": match,
        }
        if not match:
            result["passed"] = False

    if check.expected_text_color is not None:
        actual = get_font_color(cell)
        match = colors_match(actual, check.expected_text_color)
        result["text_color"] = {
            "expected": check.expected_text_color,
            "actual": actual,
            "match": match,
        }
        if not match:
            result["passed"] = False

    if check.expected_bold is not None:
        actual = bool(cell.font.bold) if cell.font else False
        match = actual == check.expected_bold
        result["bold"] = {
            "expected": check.expected_bold,
            "actual": actual,
            "match": match,
        }
        if not match:
            result["passed"] = False

    if check.expected_formula is not None:
        actual = get_cell_formula(cell)
        match = formulas_match(actual, check.expected_formula)
        result["formula"] = {
            "expected": check.expected_formula,
            "actual": actual,
            "match": match,
        }
        if not match:
            result["passed"] = False

    return result


def get_color_hex(color: Color | None) -> str | None:
    """Convert openpyxl Color object to hex string."""
    if color is None:
        return None

    if color.type == "rgb" and color.rgb:
        rgb = color.rgb
        if len(rgb) == 8:
            return f"#{rgb[2:]}"
        return f"#{rgb}"

    if color.type == "indexed" and color.indexed is not None:
        try:
            idx = int(color.indexed)
            if 0 <= idx < len(COLOR_INDEX):
                indexed_color = COLOR_INDEX[idx]
                if indexed_color and indexed_color != "00000000":
                    return (
                        f"#{indexed_color[2:]}"
                        if len(indexed_color) == 8
                        else f"#{indexed_color}"
                    )
        except (ValueError, IndexError):
            pass

    if color.type == "theme" and color.theme is not None:
        tint = color.tint if color.tint else 0
        return f"theme:{color.theme}:{tint:.2f}"

    return None


def normalize_color(color_str: str | None) -> str | None:
    """Normalize color string for comparison."""
    if color_str is None:
        return None

    color_str = color_str.strip()
    if color_str.startswith("theme:"):
        return color_str

    if color_str.startswith("#"):
        color_str = color_str[1:]

    return color_str.upper()


def colors_match(actual: str | None, expected: str | None) -> bool:
    """Compare two color values."""
    actual_norm = normalize_color(actual)
    expected_norm = normalize_color(expected)

    if actual_norm is None and expected_norm is None:
        return True
    if actual_norm is None or expected_norm is None:
        return False

    return actual_norm == expected_norm


def get_fill_color(cell: Cell) -> str | None:
    """Get the fill/highlight color of a cell."""
    if cell.fill is None:
        return None

    if cell.fill.fill_type == "solid" and cell.fill.fgColor:
        return get_color_hex(cell.fill.fgColor)

    return None


def get_font_color(cell: Cell) -> str | None:
    """Get the font/text color of a cell."""
    if cell.font is None or cell.font.color is None:
        return None

    return get_color_hex(cell.font.color)


def get_cell_formula(cell: Cell) -> str | None:
    """Get the formula from a cell if it has one."""
    if cell.value is None:
        return None

    value_str = str(cell.value)
    if cell.data_type == "f" or value_str.startswith("="):
        if not value_str.startswith("="):
            value_str = f"={value_str}"
        return value_str
    return None


def normalize_formula(formula: str | None) -> str | None:
    """Normalize formula for comparison."""
    if formula is None:
        return None

    formula = formula.strip()
    if formula.startswith("="):
        formula = formula[1:]

    formula = formula.upper()
    formula = re.sub(r"\s+", "", formula)

    return formula


def formulas_match(actual: str | None, expected: str | None) -> bool:
    """Compare two formula values."""
    actual_norm = normalize_formula(actual)
    expected_norm = normalize_formula(expected)

    if actual_norm is None and expected_norm is None:
        return True
    if actual_norm is None or expected_norm is None:
        return False

    return actual_norm == expected_norm
