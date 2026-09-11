"""Base comparator for API responses."""

import logging
from abc import ABC, abstractmethod
from typing import Any

from .models import ComparisonResult, Difference, DifferenceType, Severity
from .types import JSONValue

logger = logging.getLogger(__name__)

# Default fields to ignore during comparison - common dynamic fields
# Users can override by passing custom list or empty list [] to ignore nothing
DEFAULT_IGNORE_FIELDS = ["id", "timestamp", "created_at", "updated_at", "requestId"]


class APIComparator(ABC):
    """Abstract base class for comparing API responses.

    Subclasses should implement the compare method to define
    comparison logic for their specific API.
    """

    def __init__(
        self,
        ignore_fields: list[str] | None = None,
        strict_mode: bool = False,
    ):
        """Initialize the comparator.

        Args:
            ignore_fields: Fields to ignore during comparison. Common dynamic fields
                like timestamps and IDs are ignored by default: ["id", "timestamp",
                "created_at", "updated_at", "requestId"]. Pass a custom list to override
                defaults, or pass an empty list [] to ignore nothing.
            strict_mode: If True, treat all differences as errors, not warnings
        """
        if ignore_fields is None:
            self.ignore_fields = DEFAULT_IGNORE_FIELDS.copy()
            logger.debug(f"Using default ignore fields: {DEFAULT_IGNORE_FIELDS}")
        else:
            self.ignore_fields = ignore_fields
            if ignore_fields:
                logger.debug(f"Using custom ignore_fields: {ignore_fields}")
            else:
                logger.debug("No ignore_fields specified, will compare all fields")
        self.strict_mode = strict_mode

    @abstractmethod
    async def compare(
        self,
        endpoint: str,
        method: str,
        request_data: dict[str, Any],
        expected: dict[str, Any],
        actual: dict[str, Any],
    ) -> ComparisonResult:
        """Compare expected and actual API responses.

        Args:
            endpoint: API endpoint path
            method: HTTP method
            request_data: Request payload/params
            expected: Expected response data
            actual: Actual response data

        Returns:
            ComparisonResult with differences found
        """
        pass

    def compare_fields(
        self,
        expected: JSONValue,
        actual: JSONValue,
        path: str = "",
    ) -> list[Difference]:
        """Recursively compare two data structures (any valid JSON type).

        Args:
            expected: Expected data (dict, list, or primitive)
            actual: Actual data (dict, list, or primitive)
            path: Current field path for nested objects

        Returns:
            List of Difference objects
        """
        differences = []

        # Handle different data types
        if isinstance(expected, dict) and isinstance(actual, dict):
            # Compare dictionaries field by field
            return self._compare_dicts(expected, actual, path)

        elif isinstance(expected, list) and isinstance(actual, list):
            # Compare lists
            return self._compare_lists(expected, actual, path)

        elif not self._types_compatible(expected, actual):
            # Type mismatch
            expected_type = type(expected).__name__
            actual_type = type(actual).__name__
            differences.append(
                Difference(
                    type=DifferenceType.TYPE_MISMATCH,
                    path=path or "root",
                    expected=expected,
                    actual=actual,
                    severity=Severity.ERROR,
                    message=f"Type mismatch: expected {expected_type}, got {actual_type}",
                )
            )

        elif expected != actual:
            # Value mismatch for primitives
            differences.append(
                Difference(
                    type=DifferenceType.VALUE_MISMATCH,
                    path=path or "root",
                    expected=expected,
                    actual=actual,
                    severity=Severity.ERROR,
                )
            )

        return differences

    def _compare_dicts(
        self,
        expected: dict[str, Any],
        actual: dict[str, Any],
        path: str = "",
    ) -> list[Difference]:
        """Compare two dictionaries field by field."""
        differences = []

        # Check for missing fields
        for key in expected:
            field_path = f"{path}.{key}" if path else key

            if key in self.ignore_fields:
                logger.debug(f"Ignoring field '{key}' at path '{field_path}'")
                continue

            if key not in actual:
                differences.append(
                    Difference(
                        type=DifferenceType.MISSING_FIELD,
                        path=field_path,
                        expected=expected[key],
                        actual=None,
                        severity=Severity.ERROR if self.strict_mode else Severity.WARNING,
                    )
                )
            elif isinstance(expected[key], dict) and isinstance(actual.get(key), dict):
                # Recursive comparison for nested objects
                nested_diffs = self.compare_fields(expected[key], actual[key], field_path)
                differences.extend(nested_diffs)
            elif isinstance(expected[key], list) and isinstance(actual.get(key), list):
                # Compare lists
                list_diffs = self._compare_lists(expected[key], actual[key], field_path)
                differences.extend(list_diffs)
            else:
                # Check for type mismatch before comparing values
                actual_value = actual.get(key)
                if not self._types_compatible(expected[key], actual_value):
                    # Type mismatch
                    expected_type = type(expected[key]).__name__
                    actual_type = type(actual_value).__name__
                    differences.append(
                        Difference(
                            type=DifferenceType.TYPE_MISMATCH,
                            path=field_path,
                            expected=expected[key],
                            actual=actual_value,
                            severity=Severity.ERROR,
                            message=f"Type mismatch: expected {expected_type}, got {actual_type}",
                        )
                    )
                elif not self._values_equal(expected[key], actual_value):
                    # Value mismatch for compatible types
                    differences.append(
                        Difference(
                            type=DifferenceType.VALUE_MISMATCH,
                            path=field_path,
                            expected=expected[key],
                            actual=actual_value,
                            severity=Severity.ERROR,
                        )
                    )

        # Check for extra fields in actual
        for key in actual:
            field_path = f"{path}.{key}" if path else key

            if key in self.ignore_fields:
                logger.debug(f"Ignoring extra field '{key}' at path '{field_path}'")
                continue

            if key not in expected:
                differences.append(
                    Difference(
                        type=DifferenceType.EXTRA_FIELD,
                        path=field_path,
                        expected=None,
                        actual=actual[key],
                        severity=Severity.INFO if not self.strict_mode else Severity.ERROR,
                    )
                )

        return differences

    def _compare_lists(self, expected: list[Any], actual: list[Any], path: str) -> list[Difference]:
        """Compare two lists."""
        differences = []

        if len(expected) != len(actual):
            differences.append(
                Difference(
                    type=DifferenceType.LENGTH_MISMATCH,
                    path=f"{path}[length]",
                    expected=len(expected),
                    actual=len(actual),
                    severity=Severity.ERROR if self.strict_mode else Severity.WARNING,
                    message=(
                        f"List length mismatch at {path}: "
                        f"expected {len(expected)}, got {len(actual)}"
                    ),
                )
            )

        # Compare elements up to the length of the shorter list
        min_len = min(len(expected), len(actual))
        for i in range(min_len):
            exp_item, act_item = expected[i], actual[i]
            item_path = f"{path}[{i}]"
            # Recursively compare complex types (dicts and lists)
            if isinstance(exp_item, dict | list) or isinstance(act_item, dict | list):
                item_diffs = self.compare_fields(exp_item, act_item, item_path)
                differences.extend(item_diffs)
            else:
                # Check type compatibility before comparing values (consistent with _compare_dicts)
                if not self._types_compatible(exp_item, act_item):
                    # Type mismatch
                    expected_type = type(exp_item).__name__
                    actual_type = type(act_item).__name__
                    differences.append(
                        Difference(
                            type=DifferenceType.TYPE_MISMATCH,
                            path=item_path,
                            expected=exp_item,
                            actual=act_item,
                            severity=Severity.ERROR,
                            message=f"Type mismatch: expected {expected_type}, got {actual_type}",
                        )
                    )
                elif not self._values_equal(exp_item, act_item):
                    # Value mismatch for compatible types
                    differences.append(
                        Difference(
                            type=DifferenceType.VALUE_MISMATCH,
                            path=item_path,
                            expected=exp_item,
                            actual=act_item,
                            severity=Severity.ERROR,
                        )
                    )

        # Report missing elements from expected list
        for i in range(min_len, len(expected)):
            differences.append(
                Difference(
                    type=DifferenceType.MISSING_FIELD,
                    path=f"{path}[{i}]",
                    expected=expected[i],
                    actual=None,
                    severity=Severity.ERROR if self.strict_mode else Severity.WARNING,
                    message=f"Missing element at index {i}",
                )
            )

        # Report extra elements in actual list
        for i in range(min_len, len(actual)):
            differences.append(
                Difference(
                    type=DifferenceType.EXTRA_FIELD,
                    path=f"{path}[{i}]",
                    expected=None,
                    actual=actual[i],
                    severity=Severity.INFO if not self.strict_mode else Severity.ERROR,
                    message=f"Extra element at index {i}",
                )
            )

        return differences

    def _types_compatible(self, expected: Any, actual: Any) -> bool:
        """Check if types are compatible for comparison.

        Args:
            expected: Expected value
            actual: Actual value

        Returns:
            True if types are compatible, False otherwise
        """
        # Booleans must match booleans exactly (not treated as int despite subclass)
        if isinstance(expected, bool) or isinstance(actual, bool):
            return isinstance(expected, bool) and isinstance(actual, bool)

        # Allow numeric type compatibility (int/float) but NOT bool
        if isinstance(expected, int | float) and isinstance(actual, int | float):
            return True

        # Otherwise require exact type match
        return isinstance(actual, type(expected))

    def _values_equal(self, expected: Any, actual: Any) -> bool:
        """Check if two values are equal using strict equality.

        Uses strict equality (==) for all comparisons - critical for financial accuracy.
        For financial applications (QuickBooks, FactSet, Bloomberg), exact matching is
        required for amounts, balances, and transactions.

        No fuzzy matching:
        - Numbers: $100.00 != $100.01 (exact comparison, no tolerance)
        - Strings: Whitespace is significant (no trimming)

        If you need to ignore specific differences, use ignore_fields parameter.
        """
        return expected == actual


class DataComparator(APIComparator):
    """Concrete comparator for general data validation.

    Provides compare_fields() functionality without requiring endpoint/method context.
    Used by validators that only need field-level comparison (FixtureValidator, MCPValidator).

    The abstract compare() method raises NotImplementedError since it's not needed
    for simple data validation use cases.
    """

    async def compare(
        self,
        endpoint: str,
        method: str,
        request_data: dict[str, Any],
        expected: dict[str, Any],
        actual: dict[str, Any],
    ) -> ComparisonResult:
        """Not implemented - use compare_fields() directly for data validation."""
        raise NotImplementedError(
            "DataComparator is for field-level comparison only. "
            "Use compare_fields() directly or use LiveAPIComparator for full endpoint comparison."
        )
