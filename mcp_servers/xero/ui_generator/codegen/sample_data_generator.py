"""
Sample Data Generator - Generate realistic CSV sample data from database schemas.

Creates example data for database tables to help users understand the schema.
"""

import csv
import io
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4


class SampleDataGenerator:
    """Generate sample CSV data from database table schemas."""

    def __init__(self, num_rows: int = 3):
        """
        Initialize generator.

        Args:
            num_rows: Number of sample rows to generate per table (default: 3)
        """
        self.num_rows = num_rows
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
            return str(uuid4())

        # Handle foreign keys
        if col_name.endswith("_id") and not col["primary_key"]:
            # Try to find referenced table
            ref_table = col_name[:-3] + "s"  # e.g., workspace_id -> workspaces
            if ref_table in self._foreign_key_values and self._foreign_key_values[ref_table]:
                # Use existing value from referenced table
                return self._foreign_key_values[ref_table][
                    min(row_idx, len(self._foreign_key_values[ref_table]) - 1)
                ]
            # Fallback to UUID
            if col_type == "String":
                return str(uuid4())
            return row_idx + 1

        # Handle timestamps
        if col_name in ("created_at", "updated_at", "started_at", "completed_at", "submitted_at"):
            base_time = datetime.now() - timedelta(days=row_idx)
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
            base_time = datetime.now() - timedelta(days=row_idx)
            return base_time.strftime("%Y-%m-%d %H:%M:%S")

        if col_type == "Date":
            base_time = datetime.now() - timedelta(days=row_idx)
            return base_time.strftime("%Y-%m-%d")

        # JSON types
        if col_type in ("JSONType", "JSON"):
            return "{}"

        # Enum types
        if col_type.startswith("Enum"):
            return f"Value{row_idx + 1}"

        # Default fallback
        return f"value{row_idx + 1}"


def generate_csv_from_json_data(
    json_data: dict[str, Any], entity_key: str, max_depth: int = 3, max_rows: int = 3
) -> str | None:
    """
    Generate CSV content from JSON data with support for nested structures.

    Args:
        json_data: The loaded JSON data (e.g., synthetic_data.json)
        entity_key: The key in JSON data (e.g., "Accounts", "Contacts")
        max_depth: Maximum nesting depth for dot notation (default: 3)
        max_rows: Maximum number of rows to include (default: 3)

    Returns:
        CSV string with dot notation headers, or None if entity not found
    """
    if entity_key not in json_data:
        return None

    entity_data = json_data[entity_key]

    # Handle case where entity is not a list
    if not isinstance(entity_data, list):
        return None

    if not entity_data:
        return None

    # Limit to max_rows
    sample_data = entity_data[:max_rows]

    # Flatten all objects and collect all possible paths
    all_paths = set()
    for item in sample_data:
        paths = _extract_all_paths(item, max_depth=max_depth)
        all_paths.update(paths)

    # Sort paths for consistent column ordering
    sorted_paths = sorted(all_paths)

    # Create CSV
    output = io.StringIO()
    writer = csv.writer(output)

    # Write header
    writer.writerow(sorted_paths)

    # Write rows
    for item in sample_data:
        row = []
        for path in sorted_paths:
            value = _get_value_by_path(item, path)
            # Convert value to CSV-friendly format
            if value is None:
                row.append("")
            elif isinstance(value, bool):
                row.append(str(value).lower())
            elif isinstance(value, int | float):
                row.append(value)
            else:
                row.append(str(value))
        writer.writerow(row)

    return output.getvalue()


def _extract_all_paths(
    obj: Any, prefix: str = "", max_depth: int = 3, current_depth: int = 0
) -> set[str]:
    """
    Extract all dot-notation paths from a nested object.

    Args:
        obj: Object to extract paths from
        prefix: Current path prefix
        max_depth: Maximum nesting depth
        current_depth: Current recursion depth

    Returns:
        Set of dot-notation paths
    """
    if current_depth >= max_depth:
        return set()

    paths = set()

    if isinstance(obj, dict):
        for key, value in obj.items():
            new_prefix = f"{prefix}.{key}" if prefix else key

            # Add the path for this field
            if not isinstance(value, dict | list):
                paths.add(new_prefix)

            # Recurse for nested objects
            if isinstance(value, dict):
                paths.update(_extract_all_paths(value, new_prefix, max_depth, current_depth + 1))
            elif isinstance(value, list) and value:
                # For arrays, extract paths from first item only
                first_item = value[0]
                if isinstance(first_item, dict):
                    for sub_path in _extract_all_paths(
                        first_item, f"{new_prefix}.0", max_depth, current_depth + 1
                    ):
                        paths.add(sub_path)
                else:
                    paths.add(f"{new_prefix}.0")

    return paths


def _get_value_by_path(obj: Any, path: str) -> Any:
    """
    Get value from nested object using dot notation path.

    Args:
        obj: Object to get value from
        path: Dot-notation path (e.g., "Contact.Name" or "Addresses.0.City")

    Returns:
        Value at path, or None if not found
    """
    parts = path.split(".")
    current = obj

    for part in parts:
        if current is None:
            return None

        # Handle array index
        if part.isdigit():
            index = int(part)
            if isinstance(current, list) and index < len(current):
                current = current[index]
            else:
                return None
        # Handle dict key
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None

    return current


def generate_sample_data_from_synthetic_json(
    synthetic_data_path: str, server_name: str, max_rows: int = 3
) -> list[dict[str, Any]]:
    """
    Generate sample CSV data from a synthetic_data.json file.

    Args:
        synthetic_data_path: Path to synthetic_data.json file
        server_name: Name of the MCP server
        max_rows: Maximum rows per entity (default: 3)

    Returns:
        List of sample data table dictionaries
    """
    import json
    from pathlib import Path

    # Load synthetic data
    data_path = Path(synthetic_data_path)
    if not data_path.exists():
        return []

    try:
        with open(data_path) as f:
            json_data = json.load(f)
    except Exception:
        return []

    result = []

    # Entity type descriptions
    entity_descriptions = {
        "Accounts": "Chart of accounts with bank and revenue accounts",
        "Contacts": "Customers and suppliers with addresses and phone numbers",
        "Invoices": "Sales and purchase invoices with line items",
        "BankTransactions": "Bank transactions including deposits and withdrawals",
        "Payments": "Payments linking invoices to bank accounts",
        "Reports": "Financial reports (Balance Sheet, Profit & Loss)",
    }

    # Generate CSV for each entity type
    for entity_key in json_data.keys():
        if isinstance(json_data[entity_key], list) and json_data[entity_key]:
            csv_content = generate_csv_from_json_data(
                json_data=json_data, entity_key=entity_key, max_depth=3, max_rows=max_rows
            )

            if csv_content:
                # Count actual rows in CSV
                row_count = len(csv_content.strip().split("\n")) - 1  # Subtract header

                result.append(
                    {
                        "table_name": entity_key,
                        "model_name": entity_key.rstrip("s"),  # Singular form
                        "description": entity_descriptions.get(entity_key, f"{entity_key} data"),
                        "row_count": row_count,
                        "csv_content": csv_content.strip(),
                        "server": server_name,
                    }
                )

    return result
