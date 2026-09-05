import csv
import io
from typing import Any

from .base import BaseParser, TableMapping


class CSVParser(BaseParser):
    """Parser for CSV files.

    Parses CSV content with automatic header detection and type inference.
    """

    def parse(self, content: str) -> list[dict[str, Any]]:
        """Parse CSV content into list of row dictionaries.

        Args:
            content: Raw CSV text.

        Returns:
            List of dictionaries where keys are column headers.
        """
        rows: list[dict[str, Any]] = []

        # Use csv.DictReader to parse with headers
        reader = csv.DictReader(io.StringIO(content))

        for row in reader:
            parsed_row: dict[str, Any] = {}
            for key, value in row.items():
                if key is not None:  # Skip None keys from malformed CSVs
                    parsed_row[key] = self._parse_value(value)
            rows.append(parsed_row)

        return rows

    def apply_mapping(
        self, data: list[dict[str, Any]], mapping: TableMapping
    ) -> list[dict[str, Any]]:
        """Return all parsed CSV rows, optionally converting types back to strings.

        CSV files typically represent a single table, so the mapping's
        file_pattern is used at the file selection level, not here.

        Args:
            data: Parsed CSV rows (with type inference already applied).
            mapping: Table mapping config (file_pattern used for file selection,
                     type_inference controls whether to keep inferred types).

        Returns:
            All parsed rows for inclusion in the ArtifactState table.
        """
        if not mapping.type_inference:
            # Convert all values back to strings when type inference is disabled
            return [
                {k: str(v) if v is not None else None for k, v in row.items()}
                for row in data
            ]
        return data

    def _parse_value(self, value: str | None) -> Any:
        """Parse a single CSV value with type inference.

        Args:
            value: Raw string value from CSV cell.

        Returns:
            Parsed value (int, float, bool, None, or string).
        """
        if value is None or value == "":
            return None

        # Try to parse as number first (before boolean check)
        # This ensures "0" and "1" are treated as integers, not booleans
        try:
            if "." in value:
                return float(value)
            return int(value)
        except ValueError:
            pass

        # Check for boolean values (only non-numeric ones)
        if value.lower() in ("true", "yes"):
            return True
        if value.lower() in ("false", "no"):
            return False

        return value
