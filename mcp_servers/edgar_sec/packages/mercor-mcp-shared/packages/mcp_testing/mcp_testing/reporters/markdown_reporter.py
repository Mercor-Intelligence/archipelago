"""Markdown report generator."""

from datetime import UTC, datetime

from ..core.models import ComparisonResult, ValidationResult
from .base import Reporter


class MarkdownReporter(Reporter):
    """Generate test reports in Markdown format."""

    def __init__(self, include_details: bool = True):
        """Initialize Markdown reporter.

        Args:
            include_details: Whether to include detailed difference information
        """
        self.include_details = include_details

    def generate(self, results: list[ComparisonResult] | list[ValidationResult]) -> str:
        """Generate Markdown report from results.

        Args:
            results: List of test or comparison results

        Returns:
            Markdown formatted report
        """
        lines = []

        # Header
        lines.append("# Test Report")
        lines.append(f"\n**Generated:** {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        lines.append("")

        if all(isinstance(r, ComparisonResult) for r in results):
            lines.extend(self._format_comparison_results(results))
        elif all(isinstance(r, ValidationResult) for r in results):
            lines.extend(self._format_test_results(results))
        else:
            # Mixed result types - report error
            raise ValueError(
                "Cannot generate report from mixed result types. "
                "Results contain both ComparisonResult and ValidationResult objects. "
                "Please provide a homogeneous list of results."
            )

        return "\n".join(lines)

    def _format_comparison_results(self, results: list[ComparisonResult]) -> list[str]:
        """Format comparison results as Markdown."""
        lines = []

        # Summary
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        failed = total - passed
        pass_rate = (passed / total * 100) if total > 0 else 0

        lines.append("## Summary")
        lines.append("")
        lines.append(f"- **Total Tests:** {total}")
        lines.append(f"- **Passed:** {passed} ✓")
        lines.append(f"- **Failed:** {failed} ✗")
        lines.append(f"- **Pass Rate:** {pass_rate:.1f}%")
        lines.append("")

        # Results table
        lines.append("## Test Results")
        lines.append("")
        lines.append("| Endpoint | Method | Status | Duration | Differences |")
        lines.append("|----------|--------|--------|----------|-------------|")

        for result in results:
            status = "✓ PASS" if result.passed else "✗ FAIL"
            duration = self._format_duration(result.duration_ms)
            diff_count = len(result.differences)

            lines.append(
                f"| `{result.endpoint}` | {result.method} | {status} | {duration} | {diff_count} |"
            )

        lines.append("")

        # Detailed differences (if requested)
        if self.include_details:
            lines.append("## Detailed Differences")
            lines.append("")

            for result in results:
                if not result.differences:
                    continue

                lines.append(f"### {result.method} {result.endpoint}")
                lines.append("")

                for diff in result.differences:
                    severity_icon = {
                        "error": "🔴",
                        "warning": "⚠️",
                        "info": "ℹ️",
                    }.get(diff.severity.value, "❓")

                    lines.append(f"**{severity_icon} {diff.type.value}** at `{diff.path}`")
                    lines.append(f"- Expected: `{diff.expected}`")
                    lines.append(f"- Actual: `{diff.actual}`")
                    if diff.message:
                        lines.append(f"- Note: {diff.message}")
                    lines.append("")

        return lines

    def _format_test_results(self, results: list[ValidationResult]) -> list[str]:
        """Format test results as Markdown."""
        lines = []

        # Summary
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        failed = total - passed
        pass_rate = (passed / total * 100) if total > 0 else 0

        lines.append("## Summary")
        lines.append("")
        lines.append(f"- **Total Tests:** {total}")
        lines.append(f"- **Passed:** {passed} ✓")
        lines.append(f"- **Failed:** {failed} ✗")
        lines.append(f"- **Pass Rate:** {pass_rate:.1f}%")
        lines.append("")

        # Results
        lines.append("## Test Results")
        lines.append("")

        for result in results:
            status_icon = "✓" if result.passed else "✗"
            status_text = "PASS" if result.passed else "FAIL"

            lines.append(f"### {status_icon} {result.test_name}")
            lines.append(f"**Status:** {status_text}")
            if result.message:
                lines.append(f"**Message:** {result.message}")
            if result.duration_ms > 0:
                lines.append(f"**Duration:** {self._format_duration(result.duration_ms)}")
            if result.error:
                lines.append(f"**Error:** `{result.error}`")
            lines.append("")

        return lines
