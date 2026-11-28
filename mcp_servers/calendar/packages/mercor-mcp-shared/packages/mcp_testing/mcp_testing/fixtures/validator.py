"""Fixture validation utilities."""

import logging
from collections.abc import Callable
from typing import Any

from ..core.comparator import DataComparator
from ..core.models import ComparisonResult, Difference, DifferenceType, Severity, ValidationResult
from ..core.types import MCPTool
from .models import FixtureModel

logger = logging.getLogger(__name__)


class FixtureValidator:
    """Validate API responses against fixture expectations."""

    def __init__(self, ignore_fields: list[str] | None = None, strict_mode: bool = False):
        """Initialize the validator.

        Args:
            ignore_fields: Fields to ignore during comparison. Defaults to common
                dynamic fields like timestamps and IDs. Pass an empty list [] to
                ignore nothing.
            strict_mode: If True, treat all differences as errors, not warnings
        """
        # Use comparator only for compare_fields() - builds ComparisonResult locally
        self._comparator = DataComparator(ignore_fields=ignore_fields, strict_mode=strict_mode)

    async def validate(
        self,
        fixture: FixtureModel,
        mcp_tool: MCPTool,
        request_factory: Callable[[FixtureModel], Any] | None = None,
    ) -> ValidationResult:
        """Validate that MCP tool response matches fixture expectations.

        Args:
            fixture: Fixture containing request and expected response
            mcp_tool: Async MCP tool function to test
            request_factory: Optional function to create request object from fixture

        Returns:
            ValidationResult indicating pass/fail and any differences
        """
        differences = []

        try:
            # Create request object
            if request_factory:
                request = request_factory(fixture)
            else:
                # Default: pass fixture request as-is
                request = fixture.request.model_dump()

            # Call MCP tool
            response = await mcp_tool(request)

            # Extract status code
            if hasattr(response, "status_code"):
                actual_status = response.status_code
            elif isinstance(response, dict):
                actual_status = response.get("status_code", 200)
                if "status_code" not in response:
                    logger.warning("Could not extract status code from response, assuming 200")
            else:
                logger.warning("Could not extract status code from response, assuming 200")
                actual_status = 200

            # Compare status codes
            if actual_status != fixture.expected.status:
                differences.append(
                    Difference(
                        type=DifferenceType.STATUS_CODE_MISMATCH,
                        path="status_code",
                        expected=fixture.expected.status,
                        actual=actual_status,
                        severity=Severity.ERROR,
                        message=f"Expected status {fixture.expected.status}, got {actual_status}",
                    )
                )

            # Extract response data
            if hasattr(response, "data"):
                actual_data = response.data
            elif isinstance(response, dict):
                if "data" in response:
                    actual_data = response["data"]
                else:
                    # Exclude HTTP metadata when using full dict as data
                    # Only exclude status_code (already extracted above) - don't exclude
                    # "status", "error", "fault" as these may be legitimate data fields
                    metadata_keys = {"status_code"}
                    actual_data = {k: v for k, v in response.items() if k not in metadata_keys}
            else:
                actual_data = response

            # Compare response data if expected
            if fixture.expected.data is not None:
                data_diffs = self._compare_data(fixture.expected.data, actual_data)
                differences.extend(data_diffs)

            # Check error messages if this is an error case
            if fixture.expected.error_contains:
                # Extract error text from standard API error fields
                # (same fields as FixtureGenerator._extract_error_messages)
                error_text = None
                error_fields = ["error", "message", "detail", "error_description", "fault"]

                if isinstance(response, dict):
                    # Check standard error fields
                    for field in error_fields:
                        if field in response:
                            error_text = str(response[field]).lower()
                            break
                elif hasattr(response, "fault"):
                    error_text = str(response.fault).lower()

                # Also check data field for error info
                if error_text is None and isinstance(actual_data, dict):
                    for field in error_fields:
                        if field in actual_data:
                            error_text = str(actual_data[field]).lower()
                            break

                if error_text is not None:
                    differences.extend(
                        Difference(
                            type=DifferenceType.VALUE_MISMATCH,
                            path="error_message",
                            expected=keyword,
                            actual=error_text,
                            severity=Severity.ERROR,
                            message=f"Expected error to contain '{keyword}'",
                        )
                        for keyword in fixture.expected.error_contains
                        if keyword.lower() not in error_text
                    )
                else:
                    # Expected error keywords but no error fields found
                    differences.append(
                        Difference(
                            type=DifferenceType.MISSING_FIELD,
                            path="error_fields",
                            expected=fixture.expected.error_contains,
                            actual=None,
                            severity=Severity.ERROR,
                            message=(
                                f"Expected error response with one of {error_fields}, "
                                "but none found"
                            ),
                        )
                    )

            passed = len([d for d in differences if d.severity == Severity.ERROR]) == 0

            # Create comparison result with differences
            comparison = ComparisonResult(
                endpoint=fixture.request.endpoint,
                method=fixture.request.method,
                request_data=fixture.request.model_dump(),
                mock_response=actual_data,  # Actual MCP tool output (being tested)
                live_response=fixture.expected.data,  # Expected/fixture data (ground truth)
                differences=differences,
            )

            return ValidationResult(
                test_name=fixture.name,
                passed=passed,
                message=f"Found {len(differences)} differences"
                if differences
                else "All checks passed",
                comparison=comparison,
            )

        except Exception as e:
            return ValidationResult(
                test_name=fixture.name,
                passed=False,
                message=f"Test failed with exception: {e}",
                error=e,
            )

    def _compare_data(self, expected: Any, actual: Any, path: str = "") -> list[Difference]:
        """Recursively compare expected and actual data.

        Delegates to APIComparator for consistent comparison logic.
        """
        return self._comparator.compare_fields(expected, actual, path)
