"""LookML Generator - Convert CSV schemas to LookML view and model files.

This module generates LookML files from CSV data, enabling automatic creation of
Looker views and explores from seeded data.

The generated LookML can be:
1. Written to disk for Git-based deployment to Looker
2. Used to keep mock_data.py in sync with CSV schemas
3. Deployed via Looker's Project API (if available)

CLI Usage:
    python lookml_generator.py --input-dir /path/to/csvs --output-dir /path/to/lookml
"""

import argparse
import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Path to CSV data directory
_CSV_DATA_DIR = Path(__file__).parent / "data" / "csv"
_LOOKML_OUTPUT_DIR = Path(__file__).parent / "data" / "lookml"

# ID field detection patterns - used to identify ID/key fields
# These fields get special treatment: number type inference, no measure generation
_ID_EXACT_MATCHES = ("id", "key", "pk", "unique_key")
_ID_SUFFIXES = ("_id", "_key", "_pk")


@dataclass
class LookMLDimension:
    """Represents a LookML dimension."""

    name: str
    type: str
    sql: str
    label: str | None = None
    description: str | None = None
    primary_key: bool = False


@dataclass
class LookMLMeasure:
    """Represents a LookML measure."""

    name: str
    type: str
    sql: str | None = None
    label: str | None = None
    description: str | None = None


@dataclass
class LookMLView:
    """Represents a LookML view."""

    name: str
    sql_table_name: str
    dimensions: list[LookMLDimension] = field(default_factory=list)
    measures: list[LookMLMeasure] = field(default_factory=list)
    label: str | None = None
    description: str | None = None


@dataclass
class LookMLExplore:
    """Represents a LookML explore."""

    name: str
    view_name: str
    label: str | None = None
    description: str | None = None


@dataclass
class LookMLModel:
    """Represents a LookML model."""

    name: str
    connection: str
    explores: list[LookMLExplore] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    label: str | None = None


def _sanitize_field_name(field_name: str) -> str:
    """Sanitize a field name to be a valid LookML identifier.

    LookML identifiers must be alphanumeric with underscores, starting with
    a letter or underscore. This matches the sanitization in build_duckdb.py
    to ensure LookML field names match DuckDB column names.

    Args:
        field_name: Raw field name from CSV header

    Returns:
        Sanitized field name safe for LookML
    """
    # Replace spaces and special chars with underscores
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", field_name)
    # Remove consecutive underscores
    sanitized = re.sub(r"_+", "_", sanitized)
    # Remove leading/trailing underscores
    sanitized = sanitized.strip("_")
    # Ensure it starts with a letter or underscore (prepend 'col_' if starts with number)
    if sanitized and sanitized[0].isdigit():
        sanitized = f"col_{sanitized}"
    # Default name if empty after sanitization
    if not sanitized:
        sanitized = "unnamed_column"
    # Convert to lowercase for consistency
    return sanitized.lower()


def _infer_field_type(field_name: str, sample_values: list[str]) -> str:
    """Infer LookML field type from field name and sample values.

    Args:
        field_name: The field name (without view prefix)
        sample_values: Sample values from the CSV

    Returns:
        LookML type string (string, number, date, datetime, yesno)
    """
    field_lower = field_name.lower()

    # Check field name patterns first - but verify with sample data
    # Some _id fields contain strings like "sess_123" not numbers
    if field_lower in _ID_EXACT_MATCHES or field_lower.endswith(_ID_SUFFIXES):
        # Verify sample values are actually numeric
        non_empty = [v for v in sample_values[:10] if v and v.strip()]
        if non_empty:
            numeric_count = sum(1 for v in non_empty if v.replace(",", "").lstrip("-").isdigit())
            if numeric_count < len(non_empty):
                return "string"  # Has non-numeric values
        return "number"

    if field_lower == "count" or field_lower.endswith("_count"):
        return "number"

    if any(
        x in field_lower for x in ("amount", "price", "cost", "revenue", "total", "qty", "quantity")
    ):
        return "number"

    # Check for score/rating fields - use word boundaries to avoid false matches
    # (e.g., "operating_system" contains "rating" but isn't a rating field)
    if field_lower in ("score", "rating") or field_lower.endswith(
        ("_score", "_rating", "_hours", "_seconds", "_minutes")
    ):
        return "number"

    if field_lower.endswith(("_at", "_date", "_time", "_timestamp")):
        # Check if it looks like datetime
        for val in sample_values[:5]:
            if val and (":" in val or "T" in val):
                return "datetime"
        return "date"

    if field_lower in ("date", "created", "updated", "closed"):
        return "date"

    if field_lower.endswith(("_flag", "_bool", "_yn")):
        return "yesno"

    # Check sample values
    non_empty = [v for v in sample_values[:10] if v and v.strip()]
    if non_empty:
        # Try to parse as number
        numeric_count = 0
        for val in non_empty:
            try:
                float(val.replace(",", ""))
                numeric_count += 1
            except ValueError:
                pass
        if numeric_count == len(non_empty):
            return "number"

        # Check for date patterns - must match the actual date format
        date_pattern = r"^\d{4}-\d{2}-\d{2}$"
        datetime_pattern = r"^\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}"
        iso_datetime_pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"
        date_match_count = 0
        datetime_match_count = 0
        for val in non_empty[:5]:
            if re.match(datetime_pattern, val) or re.match(iso_datetime_pattern, val):
                datetime_match_count += 1
            elif re.match(date_pattern, val):
                date_match_count += 1
        # Require majority of samples to match date/datetime pattern
        if datetime_match_count >= len(non_empty[:5]) // 2 + 1:
            return "datetime"
        if date_match_count >= len(non_empty[:5]) // 2 + 1:
            return "date"

    return "string"


def _generate_numeric_measures(sanitized_name: str, field_name: str) -> list[LookMLMeasure]:
    """Generate standard measures (sum, avg, min, max) for a numeric field.

    Args:
        sanitized_name: Sanitized field name for LookML
        field_name: Original field name for labels

    Returns:
        List of LookMLMeasure objects
    """
    label_base = _field_name_to_label(field_name)
    measures = []

    # Generate sum, average, min, max measures for numeric fields
    measure_types = [
        ("sum", "Total"),
        ("average", "Average"),
        ("min", "Min"),
        ("max", "Max"),
    ]

    for measure_type, label_suffix in measure_types:
        measures.append(
            LookMLMeasure(
                name=f"{sanitized_name}_{measure_type}",
                type=measure_type,
                sql=f"${{TABLE}}.{sanitized_name}",
                label=f"{label_base} ({label_suffix})",
                description=f"{label_suffix} of {label_base}",
            )
        )

    return measures


def _field_name_to_label(field_name: str) -> str:
    """Convert snake_case field name to Title Case label.

    Args:
        field_name: Field name like 'created_date'

    Returns:
        Human-readable label like 'Created Date'
    """
    return field_name.replace("_", " ").title()


def _identify_primary_key(field_names: list[str]) -> str | None:
    """Identify the primary key field from a list of field names.

    Args:
        field_names: List of field names (without view prefix)

    Returns:
        Primary key field name, or None if not found
    """
    # Common primary key patterns - exact matches
    pk_patterns = ["id", "unique_key", "pk", "key"]

    for fname in field_names:
        fname_lower = fname.lower()
        if fname_lower in pk_patterns:
            return fname

    # Look for fields ending with _id - first one is typically the primary key
    # e.g., campaign_id for marketing_campaign, event_id for events
    for fname in field_names:
        if fname.lower().endswith("_id"):
            return fname

    return field_names[0] if field_names else None


def parse_csv_schema(csv_path: Path) -> tuple[str, list[str], list[list[str]]]:
    """Parse CSV file to extract view name, fields, and sample data.

    Args:
        csv_path: Path to CSV file

    Returns:
        Tuple of (view_name, field_names, sample_rows)
    """
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        headers = next(reader)

        # Extract view name from first header (format: view_name.field_name)
        if "." in headers[0]:
            view_name = headers[0].split(".")[0]
        else:
            # Fall back to filename
            view_name = csv_path.stem

        # Extract field names (strip view prefix)
        field_names = []
        for h in headers:
            if "." in h:
                field_names.append(h.split(".", 1)[1])
            else:
                field_names.append(h)

        # Read sample rows for type inference
        sample_rows = []
        for i, row in enumerate(reader):
            if i >= 100:  # Read up to 100 rows for sampling
                break
            sample_rows.append(row)

    return view_name, field_names, sample_rows


def generate_view_from_csv(csv_path: Path) -> LookMLView:
    """Generate a LookML view from a CSV file.

    Args:
        csv_path: Path to CSV file

    Returns:
        LookMLView object
    """
    view_name, field_names, sample_rows = parse_csv_schema(csv_path)

    # Transpose sample data for per-field analysis
    field_samples: dict[str, list[str]] = {name: [] for name in field_names}
    for row in sample_rows:
        for i, val in enumerate(row):
            if i < len(field_names):
                field_samples[field_names[i]].append(val)

    # Identify primary key (using original field names for pattern matching)
    pk_field = _identify_primary_key(field_names)

    # Generate dimensions and measures
    dimensions = []
    measures = []

    for field_name in field_names:
        samples = field_samples.get(field_name, [])
        field_type = _infer_field_type(field_name, samples)

        # Sanitize field name for LookML (must match DuckDB column names)
        sanitized_name = _sanitize_field_name(field_name)

        # Always create dimension for the field (for grouping/filtering)
        dim = LookMLDimension(
            name=sanitized_name,
            type=field_type,
            sql=f"${{TABLE}}.{sanitized_name}",
            label=_field_name_to_label(field_name),
            primary_key=(field_name == pk_field),
        )
        dimensions.append(dim)

        # For numeric fields, also generate automatic measures (sum, avg, min, max)
        # Skip ID/key fields and count fields - aggregating these doesn't make sense
        # Use sanitized_name for matching since field names with spaces (e.g., "User ID")
        # become "user_id" after sanitization
        sanitized_lower = sanitized_name.lower()
        is_id_field = sanitized_lower in _ID_EXACT_MATCHES or sanitized_lower.endswith(_ID_SUFFIXES)
        is_count_field = sanitized_lower == "count" or sanitized_lower.endswith("_count")

        if field_type == "number" and not is_id_field and not is_count_field:
            numeric_measures = _generate_numeric_measures(sanitized_name, field_name)
            measures.extend(numeric_measures)

    # Always add a count measure
    measures.append(
        LookMLMeasure(
            name="count",
            type="count",
            label="Count",
            description="Count of records",
        )
    )

    return LookMLView(
        name=view_name,
        sql_table_name=f"@{{database_schema}}.{view_name}",
        dimensions=dimensions,
        measures=measures,
        label=_field_name_to_label(view_name),
    )


def generate_lookml_view_file(view: LookMLView) -> str:
    """Generate LookML view file content.

    Args:
        view: LookMLView object

    Returns:
        LookML file content as string
    """
    lines = []
    lines.append(f"view: {view.name} {{")

    if view.label:
        lines.append(f'  label: "{view.label}"')

    lines.append(f"  sql_table_name: {view.sql_table_name} ;;")
    lines.append("")

    # Dimensions
    for dim in view.dimensions:
        lines.append(f"  dimension: {dim.name} {{")
        lines.append(f"    type: {dim.type}")
        lines.append(f"    sql: {dim.sql} ;;")
        if dim.label:
            lines.append(f'    label: "{dim.label}"')
        if dim.primary_key:
            lines.append("    primary_key: yes")
        if dim.description:
            lines.append(f'    description: "{dim.description}"')
        lines.append("  }")
        lines.append("")

    # Measures
    for measure in view.measures:
        lines.append(f"  measure: {measure.name} {{")
        lines.append(f"    type: {measure.type}")
        if measure.sql:
            lines.append(f"    sql: {measure.sql} ;;")
        if measure.label:
            lines.append(f'    label: "{measure.label}"')
        if measure.description:
            lines.append(f'    description: "{measure.description}"')
        lines.append("  }")
        lines.append("")

    lines.append("}")

    return "\n".join(lines)


def generate_lookml_model_file(model: LookMLModel) -> str:
    """Generate LookML model file content.

    Args:
        model: LookMLModel object

    Returns:
        LookML file content as string
    """
    lines = []

    # Connection must be a quoted string - constants are not allowed
    lines.append(f'connection: "{model.connection}"')
    lines.append("")

    if model.label:
        lines.append(f'label: "{model.label}"')
        lines.append("")

    for include in model.includes:
        lines.append(f'include: "{include}"')

    if model.includes:
        lines.append("")

    for explore in model.explores:
        lines.append(f"explore: {explore.name} {{")
        if explore.view_name != explore.name:
            lines.append(f"  view_name: {explore.view_name}")
        if explore.label:
            lines.append(f'  label: "{explore.label}"')
        if explore.description:
            lines.append(f'  description: "{explore.description}"')
        lines.append("}")
        lines.append("")

    return "\n".join(lines)


def generate_all_lookml_from_csv_dir(
    csv_dir: Path | None = None,
    output_dir: Path | None = None,
    model_name: str = "generated",
    connection: str = "mercor",
) -> dict[str, Any]:
    """Generate LookML for all CSVs in directory.

    Args:
        csv_dir: Directory containing CSV files (default: data/csv)
        output_dir: Directory to write LookML files (default: data/lookml)
        model_name: Name for the generated model
        connection: Database connection name for the model

    Returns:
        Dict with generated file paths and content
    """
    csv_dir = csv_dir or _CSV_DATA_DIR
    output_dir = output_dir or _LOOKML_OUTPUT_DIR

    if not csv_dir.exists():
        return {"error": f"CSV directory not found: {csv_dir}"}

    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)

    views: list[LookMLView] = []
    generated_files: dict[str, str] = {}

    # Generate views from all CSVs
    for csv_path in sorted(csv_dir.glob("*.csv")):
        view = generate_view_from_csv(csv_path)
        views.append(view)

        # Generate view file
        view_content = generate_lookml_view_file(view)
        view_filename = f"{view.name}.view.lkml"
        view_path = output_dir / view_filename

        with open(view_path, "w") as f:
            f.write(view_content)

        generated_files[view_filename] = view_content

    # Generate model with explores for each view
    explores = [
        LookMLExplore(
            name=view.name,
            view_name=view.name,
            label=view.label,
        )
        for view in views
    ]

    model = LookMLModel(
        name=model_name,
        connection=connection,
        explores=explores,
        includes=["*.view.lkml"],
        label=_field_name_to_label(model_name),
    )

    model_content = generate_lookml_model_file(model)
    model_filename = f"{model_name}.model.lkml"
    model_path = output_dir / model_filename

    with open(model_path, "w") as f:
        f.write(model_content)

    generated_files[model_filename] = model_content

    return {
        "output_dir": str(output_dir),
        "files": generated_files,
        "views": [v.name for v in views],
        "model": model_name,
    }


def get_lookml_for_view(view_name: str) -> str | None:
    """Get generated LookML for a specific view.

    Args:
        view_name: Name of the view (e.g., 'service_requests')

    Returns:
        LookML content string, or None if not found
    """
    csv_path = _CSV_DATA_DIR / f"{view_name}.csv"
    if not csv_path.exists():
        return None

    view = generate_view_from_csv(csv_path)
    return generate_lookml_view_file(view)


# Convenience function for startup
def ensure_lookml_generated(
    model_name: str = "seeded_data",
    connection: str = "mercor",
) -> dict[str, Any]:
    """Ensure LookML files are generated from CSV data on startup.

    This is called during server initialization to make sure LookML
    definitions exist for all seeded CSV data.

    Args:
        model_name: Name for the generated model
        connection: Database connection name

    Returns:
        Generation result dict
    """
    return generate_all_lookml_from_csv_dir(
        model_name=model_name,
        connection=connection,
    )


def main() -> int:
    """CLI entry point for generating LookML from CSV files.

    Returns:
        Exit code (0 for success, 1 for error)
    """
    parser = argparse.ArgumentParser(
        description="Generate LookML view and model files from CSV data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Generate from default directories
    python lookml_generator.py

    # Generate from custom directories
    python lookml_generator.py --input-dir /.apps_data/looker --output-dir /.apps_data/looker/lookml

    # Custom model name
    python lookml_generator.py --input-dir ./data --model-name user_data
        """,
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=_CSV_DATA_DIR,
        help=f"Directory containing CSV files (default: {_CSV_DATA_DIR})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_LOOKML_OUTPUT_DIR,
        help=f"Directory to write LookML files (default: {_LOOKML_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--model-name",
        default="generated",
        help="Name for the generated model (default: generated)",
    )
    parser.add_argument(
        "--connection",
        default="mercor",
        help="Database connection name for the model (default: mercor)",
    )

    args = parser.parse_args()

    print(f"Generating LookML from: {args.input_dir}")
    print(f"Output directory: {args.output_dir}")

    result = generate_all_lookml_from_csv_dir(
        csv_dir=args.input_dir,
        output_dir=args.output_dir,
        model_name=args.model_name,
        connection=args.connection,
    )

    if "error" in result:
        print(f"Error: {result['error']}")
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    exit(main())
