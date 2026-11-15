"""Difference calculation utilities."""

from typing import Any

from ..core.models import Difference, DifferenceType, Severity


class DiffCalculator:
    """Calculate differences between two data structures."""

    @staticmethod
    def calculate(expected: Any, actual: Any, path: str = "") -> list[Difference]:
        """Calculate differences between expected and actual values.

        Args:
            expected: Expected value
            actual: Actual value
            path: Current path in the data structure

        Returns:
            List of Difference objects
        """
        differences = []

        # Check type compatibility (handle bool/int distinction like APIComparator)
        types_compatible = True
        # Booleans must match booleans exactly (not treated as int despite subclass)
        if isinstance(expected, bool) or isinstance(actual, bool):
            types_compatible = isinstance(expected, bool) and isinstance(actual, bool)
        # Allow numeric type compatibility (int/float) but NOT bool
        elif isinstance(expected, int | float) and isinstance(actual, int | float):
            types_compatible = True
        else:
            types_compatible = isinstance(actual, type(expected))

        if not types_compatible:
            differences.append(
                Difference(
                    type=DifferenceType.TYPE_MISMATCH,
                    path=path or "root",
                    expected=type(expected).__name__,
                    actual=type(actual).__name__,
                    severity=Severity.ERROR,
                )
            )
            return differences

        if isinstance(expected, dict):
            differences.extend(DiffCalculator._diff_dicts(expected, actual, path))
        elif isinstance(expected, list):
            differences.extend(DiffCalculator._diff_lists(expected, actual, path))
        elif expected != actual:
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

    @staticmethod
    def _diff_dicts(expected: dict, actual: dict, path: str) -> list[Difference]:
        """Calculate differences between two dictionaries."""
        differences = []

        for key in expected:
            field_path = f"{path}.{key}" if path else key

            if key not in actual:
                differences.append(
                    Difference(
                        type=DifferenceType.MISSING_FIELD,
                        path=field_path,
                        expected=expected[key],
                        actual=None,
                        severity=Severity.ERROR,
                    )
                )
            else:
                nested_diffs = DiffCalculator.calculate(expected[key], actual[key], field_path)
                differences.extend(nested_diffs)

        for key in actual:
            field_path = f"{path}.{key}" if path else key

            if key not in expected:
                differences.append(
                    Difference(
                        type=DifferenceType.EXTRA_FIELD,
                        path=field_path,
                        expected=None,
                        actual=actual[key],
                        severity=Severity.INFO,
                    )
                )

        return differences

    @staticmethod
    def _diff_lists(expected: list, actual: list, path: str) -> list[Difference]:
        """Calculate differences between two lists."""
        differences = []

        if len(expected) != len(actual):
            differences.append(
                Difference(
                    type=DifferenceType.LENGTH_MISMATCH,
                    path=f"{path}.length",
                    expected=len(expected),
                    actual=len(actual),
                    severity=Severity.ERROR,
                    message=(
                        f"List length mismatch at {path}: "
                        f"expected {len(expected)}, got {len(actual)}"
                    ),
                )
            )

        # Compare elements up to the length of the shorter list
        min_len = min(len(expected), len(actual))
        for i in range(min_len):
            item_path = f"{path}[{i}]"
            item_diffs = DiffCalculator.calculate(expected[i], actual[i], item_path)
            differences.extend(item_diffs)

        # Report missing elements from expected list
        for i in range(min_len, len(expected)):
            differences.append(
                Difference(
                    type=DifferenceType.MISSING_FIELD,
                    path=f"{path}[{i}]",
                    expected=expected[i],
                    actual=None,
                    severity=Severity.WARNING,
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
                    severity=Severity.INFO,
                    message=f"Extra element at index {i}",
                )
            )

        return differences
