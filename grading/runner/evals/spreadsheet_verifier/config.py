"""Configuration models for spreadsheet verifier."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from loguru import logger


class ValueCheckType(StrEnum):
    """Supported value check types."""

    EXACT_MATCH = "exact_match"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    REGEX = "regex"
    EQUALS_NUMBER = "equals_number"
    GREATER_THAN = "greater_than"
    LESS_THAN = "less_than"
    BETWEEN = "between"
    NOT_EMPTY = "not_empty"
    IS_EMPTY = "is_empty"


EXCEL_EXTENSIONS = frozenset({".xlsx", ".xlsm", ".xls"})
SUPPORTED_EXTENSIONS = frozenset({".csv", ".xlsx", ".xlsm", ".xls"})


@dataclass
class ValueCheck:
    """Configuration for a single value check.

    Uses cell_reference format like "A1", "B2:B10", "Sheet1!C3".
    """

    cell_reference: str
    check_type: ValueCheckType = ValueCheckType.EXACT_MATCH
    expected_value: str | None = None
    expected_min: float | None = None
    expected_max: float | None = None
    case_sensitive: bool = False


@dataclass
class FormattingCheck:
    """Configuration for a single formatting check."""

    cell_reference: str
    expected_highlight: str | None = None
    expected_text_color: str | None = None
    expected_bold: bool | None = None
    expected_formula: str | None = None

    def has_any_expectation(self) -> bool:
        """Check if at least one formatting expectation is set."""
        return any(
            v is not None
            for v in [
                self.expected_highlight,
                self.expected_text_color,
                self.expected_bold,
                self.expected_formula,
            ]
        )


@dataclass
class SpreadsheetVerifierConfig:
    """Complete configuration for spreadsheet verifier."""

    file_pattern: str = "*.xlsx"
    sheet_name: str | None = None
    value_checks: list[ValueCheck] = field(default_factory=list)
    formatting_checks: list[FormattingCheck] = field(default_factory=list)

    @classmethod
    def from_verifier_values(
        cls, verifier_values: dict[str, Any] | None
    ) -> "SpreadsheetVerifierConfig":
        """Parse configuration from verifier_values dict.

        Expected format:
        - check_type: "value_<type>" or "formatting_<type>"
        - cell_reference: Cell reference (e.g., "A1", "B2:B10")
        - Additional fields based on check_type
        """
        if not verifier_values:
            return cls()

        value_checks: list[ValueCheck] = []
        formatting_checks: list[FormattingCheck] = []

        check_type = verifier_values.get("check_type", "")
        if not check_type:
            logger.warning(
                "[SPREADSHEET_VERIFIER] Missing 'check_type' in verifier_values. "
                "Expected format: 'value_<type>' or 'formatting_<type>'"
            )
            return cls()

        cell_reference = verifier_values.get("cell_reference", "A1")

        if check_type.startswith("value_"):
            actual_check_type = check_type.replace("value_", "", 1)
            try:
                parsed_check_type = ValueCheckType(actual_check_type)
            except ValueError as err:
                valid_types = ", ".join(t.value for t in ValueCheckType)
                raise ValueError(
                    f"Invalid value check type: '{actual_check_type}'. "
                    f"Valid types: {valid_types}"
                ) from err

            value_checks = [
                ValueCheck(
                    cell_reference=cell_reference,
                    check_type=parsed_check_type,
                    expected_value=verifier_values.get("expected_value"),
                    expected_min=_parse_float(verifier_values.get("expected_min")),
                    expected_max=_parse_float(verifier_values.get("expected_max")),
                    case_sensitive=verifier_values.get("case_sensitive", False),
                )
            ]
        elif check_type.startswith("formatting_"):
            formatting_type = check_type.replace("formatting_", "", 1)

            formatting_check = FormattingCheck(cell_reference=cell_reference)
            if formatting_type == "highlight":
                formatting_check.expected_highlight = verifier_values.get(
                    "expected_highlight"
                )
            elif formatting_type == "text_color":
                formatting_check.expected_text_color = verifier_values.get(
                    "expected_text_color"
                )
            elif formatting_type == "bold":
                formatting_check.expected_bold = verifier_values.get("expected_bold")
            elif formatting_type == "formula":
                formatting_check.expected_formula = verifier_values.get(
                    "expected_formula"
                )
            else:
                logger.warning(
                    f"[SPREADSHEET_VERIFIER] Unknown formatting type: '{formatting_type}'. "
                    "Valid types: highlight, text_color, bold, formula"
                )

            formatting_checks = [formatting_check]
        else:
            logger.warning(
                f"[SPREADSHEET_VERIFIER] Invalid check_type: '{check_type}'. "
                "Must start with 'value_' or 'formatting_'"
            )

        file_pattern = verifier_values.get("file_pattern")
        if not file_pattern:
            file_pattern = "*.xlsx" if formatting_checks else "*.csv"

        return cls(
            file_pattern=file_pattern,
            sheet_name=verifier_values.get("sheet_name"),
            value_checks=value_checks,
            formatting_checks=formatting_checks,
        )

    def has_checks(self) -> bool:
        """Check if any checks are configured."""
        return bool(self.value_checks or self.formatting_checks)

    def requires_excel(self) -> bool:
        """Check if configuration requires Excel file."""
        return bool(self.formatting_checks)


@dataclass
class CheckResult:
    """Result of a single check (value or formatting)."""

    cell_reference: str
    passed: bool
    reason: str
    check_type: str | None = None
    expected: str | None = None
    actual: str | None = None
    details: dict[str, Any] | None = None


@dataclass
class SpreadsheetVerifierOutput:
    """Structured output from spreadsheet verifier."""

    passed: bool
    reason: str
    file_checked: str | None = None
    sheet_name: str | None = None
    value_check_results: list[CheckResult] = field(default_factory=list)
    formatting_check_results: list[CheckResult] = field(default_factory=list)

    @property
    def value_passed_count(self) -> int:
        return sum(1 for r in self.value_check_results if r.passed)

    @property
    def value_total_count(self) -> int:
        return len(self.value_check_results)

    @property
    def formatting_passed_count(self) -> int:
        return sum(1 for r in self.formatting_check_results if r.passed)

    @property
    def formatting_total_count(self) -> int:
        return len(self.formatting_check_results)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for verifier_result_values."""
        return {
            "passed": self.passed,
            "reason": self.reason,
            "file_checked": self.file_checked,
            "sheet_name": self.sheet_name,
            "value_checks": [
                {
                    "cell_reference": r.cell_reference,
                    "check_type": r.check_type,
                    "passed": r.passed,
                    "reason": r.reason,
                    "expected": r.expected,
                    "actual": r.actual,
                }
                for r in self.value_check_results
            ],
            "formatting_checks": [
                {
                    "cell_reference": r.cell_reference,
                    "passed": r.passed,
                    "reason": r.reason,
                    "details": r.details,
                }
                for r in self.formatting_check_results
            ],
            "value_passed_count": self.value_passed_count,
            "value_total_count": self.value_total_count,
            "formatting_passed_count": self.formatting_passed_count,
            "formatting_total_count": self.formatting_total_count,
        }


def _parse_float(value: str | float | int | None) -> float | None:
    """Parse a value to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
