"""Core data models for testing framework."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from .types import JSONValue


class DifferenceType(Enum):
    """Types of differences that can be found."""

    MISSING_FIELD = "missing_field"
    EXTRA_FIELD = "extra_field"
    VALUE_MISMATCH = "value_mismatch"
    TYPE_MISMATCH = "type_mismatch"
    LENGTH_MISMATCH = "length_mismatch"
    STATUS_CODE_MISMATCH = "status_code_mismatch"


class Severity(Enum):
    """Severity levels for differences."""

    ERROR = "error"  # Blocking issue - test should fail
    WARNING = "warning"  # Non-critical difference - may pass in non-strict mode
    INFO = "info"  # Informational only


@dataclass
class Difference:
    """Represents a difference between expected and actual values."""

    type: DifferenceType
    path: str
    expected: Any
    actual: Any
    severity: Severity  # Required - must explicitly categorize
    message: str = ""

    def __str__(self) -> str:
        """Human-readable difference description."""
        if self.message:
            return self.message
        return f"{self.type.value} at {self.path}: expected {self.expected!r}, got {self.actual!r}"


@dataclass
class ComparisonResult:
    """Result of comparing mock vs live API responses."""

    endpoint: str
    method: str
    request_data: dict[str, Any]
    mock_response: JSONValue
    live_response: JSONValue
    differences: list[Difference] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    duration_ms: float = 0.0

    @property
    def passed(self) -> bool:
        """Check if comparison passed (no critical differences)."""
        return not any(d.severity == Severity.ERROR for d in self.differences)

    @property
    def has_warnings(self) -> bool:
        """Check if there are any warnings."""
        return any(d.severity == Severity.WARNING for d in self.differences)


@dataclass
class ValidationResult:
    """Result of a validation/test case execution."""

    test_name: str
    passed: bool
    message: str = ""
    duration_ms: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    comparison: ComparisonResult | None = None
    error: Exception | None = None

    def __str__(self) -> str:
        """Human-readable test result."""
        status = "✓ PASSED" if self.passed else "✗ FAILED"
        msg = f" - {self.message}" if self.message else ""
        return f"{status}: {self.test_name}{msg}"
