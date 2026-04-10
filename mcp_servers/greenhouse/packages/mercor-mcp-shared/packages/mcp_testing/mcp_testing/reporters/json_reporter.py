"""JSON report generator."""

import json
from datetime import UTC, datetime
from typing import Any

from ..core.models import ComparisonResult, Severity, ValidationResult
from .base import Reporter


class JSONReporter(Reporter):
    """Generate test reports in JSON format."""

    def __init__(self, indent: int = 2, include_metadata: bool = True):
        """Initialize JSON reporter.

        Args:
            indent: JSON indentation level
            include_metadata: Whether to include metadata (timestamps, versions, etc.)
        """
        self.indent = indent
        self.include_metadata = include_metadata

    def generate(self, results: list[ComparisonResult] | list[ValidationResult]) -> str:
        """Generate JSON report from results.

        Args:
            results: List of test or comparison results

        Returns:
            JSON formatted report
        """
        report_data: dict[str, Any] = {}

        if self.include_metadata:
            report_data["metadata"] = {
                "generated_at": datetime.now(UTC).isoformat(),
                "total_tests": len(results),
            }

        if all(isinstance(r, ComparisonResult) for r in results):
            report_data["type"] = "comparison"
            report_data["results"] = self._format_comparison_results(results)
            report_data["summary"] = self._summarize_comparisons(results)
        elif all(isinstance(r, ValidationResult) for r in results):
            report_data["type"] = "test"
            report_data["results"] = self._format_test_results(results)
            report_data["summary"] = self._summarize_tests(results)
        else:
            # Mixed result types - report error
            raise ValueError(
                "Cannot generate report from mixed result types. "
                "Results contain both ComparisonResult and ValidationResult objects. "
                "Please provide a homogeneous list of results."
            )

        return json.dumps(report_data, indent=self.indent)

    def _format_comparison_results(self, results: list[ComparisonResult]) -> list[dict[str, Any]]:
        """Format comparison results for JSON output."""
        return [
            {
                "endpoint": result.endpoint,
                "method": result.method,
                "passed": result.passed,
                "duration_ms": result.duration_ms,
                "differences": [
                    {
                        "type": diff.type.value,
                        "path": diff.path,
                        "expected": diff.expected,
                        "actual": diff.actual,
                        "severity": diff.severity.value,
                        "message": str(diff),
                    }
                    for diff in result.differences
                ],
            }
            for result in results
        ]

    def _format_test_results(self, results: list[ValidationResult]) -> list[dict[str, Any]]:
        """Format test results for JSON output."""
        formatted = []

        for result in results:
            item = {
                "test_name": result.test_name,
                "passed": result.passed,
                "message": result.message,
                "duration_ms": result.duration_ms,
            }

            if result.error:
                item["error"] = str(result.error)

            formatted.append(item)

        return formatted

    def _summarize_comparisons(self, results: list[ComparisonResult]) -> dict[str, Any]:
        """Generate summary statistics for comparisons."""
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        failed = total - passed

        total_diffs = sum(len(r.differences) for r in results)
        errors = sum(1 for r in results for d in r.differences if d.severity == Severity.ERROR)
        warnings = sum(1 for r in results for d in r.differences if d.severity == Severity.WARNING)

        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": f"{(passed / total * 100):.1f}%" if total > 0 else "0%",
            "total_differences": total_diffs,
            "errors": errors,
            "warnings": warnings,
        }

    def _summarize_tests(self, results: list[ValidationResult]) -> dict[str, Any]:
        """Generate summary statistics for tests."""
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        failed = total - passed

        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": f"{(passed / total * 100):.1f}%" if total > 0 else "0%",
        }
