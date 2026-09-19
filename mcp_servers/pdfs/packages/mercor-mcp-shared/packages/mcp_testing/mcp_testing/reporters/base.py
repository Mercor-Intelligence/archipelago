"""Base reporter interface."""

from abc import ABC, abstractmethod
from pathlib import Path

from ..core.models import ComparisonResult, ValidationResult


class Reporter(ABC):
    """Abstract base class for test result reporters."""

    @abstractmethod
    def generate(self, results: list[ComparisonResult] | list[ValidationResult]) -> str:
        """Generate a report from test/comparison results.

        Args:
            results: List of ComparisonResult or ValidationResult objects

        Returns:
            Formatted report as string
        """
        pass

    def save(self, report: str, output_path: str | Path) -> None:
        """Save report to a file.

        Args:
            report: Report content
            output_path: Path where to save the report
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report)

    def _format_duration(self, duration_ms: float) -> str:
        """Format duration in human-readable format."""
        if duration_ms < 1:
            return f"{duration_ms:.2f}ms"
        elif duration_ms < 1000:
            return f"{duration_ms:.0f}ms"
        else:
            return f"{duration_ms / 1000:.2f}s"
