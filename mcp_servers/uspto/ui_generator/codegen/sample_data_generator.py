"""
Sample Data Generator - Generate realistic CSV sample data from database schemas.

Creates example data for database tables to help users understand the schema.
"""

import csv
import io
from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_DNS, uuid5


class SampleDataGenerator:
    """Generate sample CSV data from database table schemas."""

    def __init__(self, num_rows: int = 3, base_timestamp: datetime | None = None):
        """
        Initialize generator.

        Args:
            num_rows: Number of sample rows to generate per table (default: 3)
            base_timestamp: Fixed base timestamp for deterministic data (default: 2025-01-01)
        """
        self.num_rows = num_rows
        self.base_timestamp = base_timestamp or datetime(2025, 1, 1, 0, 0, 0)
        self._foreign_key_values: dict[str, list[Any]] = {}

    def generate_sample_data(
        self, tables: list[dict[str, Any]], server_name: str
    ) -> list[dict[str, Any]]:
        """
        Generate sample data for multiple tables.

        Args:
            tables: List of table schema dictionaries
            server_name: Name of the MCP server

        Returns:
            List of table data dictionaries with CSV content
        """
        result = []

        # First pass: generate data for tables without foreign keys
        # Second pass: generate data for tables with foreign keys
        tables_sorted = self._sort_tables_by_dependencies(tables)

        for table in tables_sorted:
            csv_content = self._generate_table_csv(table)
            result.append(
                {
                    "table_name": table["table_name"],
                    "model_name": table["model_name"],
                    "description": table.get("docstring", "").split("\n")[0].strip(),
                    "row_count": self.num_rows,
                    "csv_content": csv_content,
                    "server": server_name,
                    "columns": table.get("columns", []),
                }
            )

        return result

    def _sort_tables_by_dependencies(self, tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Sort tables so those without foreign keys come first."""
        # Simple heuristic: tables with 'id' as primary key and no foreign keys first
        independent = []
        dependent = []

        for table in tables:
            has_fk = any(
                "_id" in col["name"] and not col["primary_key"] for col in table["columns"]
            )
            if has_fk:
                dependent.append(table)
            else:
                independent.append(table)

        return independent + dependent

    def _generate_table_csv(self, table: dict[str, Any]) -> str:
        """Generate CSV content for a single table."""
        columns = table["columns"]

        # Create CSV in memory
        output = io.StringIO()
        writer = csv.writer(output)

        # Write header
        header = [col["name"] for col in columns]
        writer.writerow(header)

        # Generate rows
        for row_idx in range(self.num_rows):
            row = []
            row_data = {}

            for col in columns:
                value = self._generate_column_value(col, row_idx, table["table_name"])
                row.append(value)
                # Store for foreign key references
                if col["primary_key"]:
                    if table["table_name"] not in self._foreign_key_values:
                        self._foreign_key_values[table["table_name"]] = []
                    self._foreign_key_values[table["table_name"]].append(value)
                row_data[col["name"]] = value

            writer.writerow(row)

        return output.getvalue()

    def _generate_column_value(self, col: dict[str, Any], row_idx: int, table_name: str) -> Any:
        """Generate a sample value for a column."""
        col_name = col["name"]
        col_type = col["type"]

        # Handle auto-increment IDs
        if col["primary_key"] and col_type in ("Integer", "BigInteger"):
            return row_idx + 1

        # Handle UUIDs
        if col_type == "String" and col["primary_key"]:
            # Use deterministic UUID based on table name and row index
            return str(uuid5(NAMESPACE_DNS, f"{table_name}.{col_name}.{row_idx}"))

        # Handle foreign keys
        if col_name.endswith("_id") and not col["primary_key"]:
            # Try to find referenced table
            ref_table = col_name[:-3] + "s"  # e.g., workspace_id -> workspaces
            if ref_table in self._foreign_key_values and self._foreign_key_values[ref_table]:
                # Use existing value from referenced table
                fk_values = self._foreign_key_values[ref_table]
                return fk_values[min(row_idx, len(fk_values) - 1)]
            # Fallback to UUID
            if col_type == "String":
                # Use deterministic UUID based on table name, column name, and row index
                return str(uuid5(NAMESPACE_DNS, f"{table_name}.{col_name}.{row_idx}"))
            return row_idx + 1

        # Handle timestamps
        if col_name in (
            "created_at",
            "updated_at",
            "started_at",
            "completed_at",
            "submitted_at",
        ):
            base_time = self.base_timestamp - timedelta(days=row_idx)
            return base_time.strftime("%Y-%m-%d %H:%M:%S")

        # Handle specific column names
        if col_name == "name":
            return f"{table_name.capitalize()} {row_idx + 1}"

        if col_name in ("email", "user_email"):
            return f"user{row_idx + 1}@example.com"

        if col_name in ("username", "user"):
            return f"user{row_idx + 1}"

        if col_name in ("description", "error_message"):
            return f"Sample {col_name.replace('_', ' ')} {row_idx + 1}"

        if col_name in ("url", "web_url", "embed_url", "qna_url"):
            return f"https://example.com/{table_name}/{row_idx + 1}"

        if col_name == "filename":
            return f"file{row_idx + 1}.pbix"

        if col_name == "status":
            statuses = ["Succeeded", "Running", "Queued"]
            return statuses[row_idx % len(statuses)]

        # Handle types
        if col_type in ("String", "Text"):
            if col_name.endswith("_type"):
                return f"Type{row_idx + 1}"
            if col_name.endswith("_name"):
                return f"Name {row_idx + 1}"
            return f"Sample {col_name} {row_idx + 1}"

        if col_type in ("Integer", "BigInteger"):
            return (row_idx + 1) * 100

        if col_type in ("Numeric", "Float", "Decimal"):
            return round(100.0 + row_idx * 10.5, 2)

        if col_type == "Boolean":
            return str(row_idx % 2 == 0).lower()

        if col_type == "DateTime":
            base_time = self.base_timestamp - timedelta(days=row_idx)
            return base_time.strftime("%Y-%m-%d %H:%M:%S")

        if col_type == "Date":
            base_time = self.base_timestamp - timedelta(days=row_idx)
            return base_time.strftime("%Y-%m-%d")

        # JSON types
        if col_type in ("JSONType", "JSON"):
            return "{}"

        # Enum types
        if col_type.startswith("Enum"):
            return f"Value{row_idx + 1}"

        # Default fallback
        return f"value{row_idx + 1}"
