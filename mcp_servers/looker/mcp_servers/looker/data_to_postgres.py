#!/usr/bin/env python3
"""Load CSV and JSON data into PostgreSQL database.

This script loads data files from data/csv/ and data/json/ into PostgreSQL,
with configurable table name mappings via data/tables.yaml.

Supports:
- CSV files (with Looker-style headers like view_name.field_name)
- JSON files (array of objects or single object with data array)
- YAML config for custom table name mappings

Usage:
    # Load all data files
    python data_to_postgres.py --database-url postgresql://user:pass@host/db

    # Specify schema
    python data_to_postgres.py --database-url ... --schema public

    # Load only specific files
    python data_to_postgres.py --database-url ... --files orders.csv events.json

    # List available files
    python data_to_postgres.py --list
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# Optional imports - fail gracefully if not installed
try:
    import pandas as pd

    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    pd = None  # type: ignore[assignment]

try:
    import yaml

    HAS_YAML = True
except ImportError:
    HAS_YAML = False

try:
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import execute_values

    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

# Data directories
DATA_DIR = Path(__file__).parent / "data"
CSV_DATA_DIR = DATA_DIR / "csv"
JSON_DATA_DIR = DATA_DIR / "json"
CONFIG_FILE = DATA_DIR / "tables.yaml"

# Date validation patterns
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

# Date formats to try when parsing
DATE_FORMATS = [
    # Date only
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%m-%d-%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%m.%d.%Y",
    # Datetime with seconds
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %I:%M:%S %p",
    "%d/%m/%Y %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%m-%d-%Y %H:%M:%S",
    "%d.%m.%Y %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    # Datetime without seconds
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y %I:%M %p",
    "%d/%m/%Y %H:%M",
    "%d-%m-%Y %H:%M",
    "%d.%m.%Y %H:%M",
    # ISO-ish formats
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
]


# =============================================================================
# Date Parsing
# =============================================================================


def _parse_date_to_iso(val: str) -> str | None:
    """Parse various date/datetime formats to ISO format for PostgreSQL.

    Returns: ISO date (YYYY-MM-DD) or datetime (YYYY-MM-DD HH:MM:SS) string,
             or None if invalid.
    """
    if not val:
        return None

    val = val.strip()
    if not val:
        return None

    # Already in ISO format - validate it's actually a valid date
    if DATE_PATTERN.match(val):
        try:
            datetime.strptime(val, "%Y-%m-%d")
            return val
        except ValueError:
            return None  # Invalid date like 2024-13-45

    if DATETIME_PATTERN.match(val):
        # Map formats to expected string lengths
        # Format string length != output length (e.g., %Y is 2 chars but produces 4)
        formats_with_lengths = [
            ("%Y-%m-%dT%H:%M:%S", 19),  # 2024-01-15T10:30:00
            ("%Y-%m-%d %H:%M:%S", 19),  # 2024-01-15 10:30:00
            ("%Y-%m-%dT%H:%M", 16),  # 2024-01-15T10:30
            ("%Y-%m-%d %H:%M", 16),  # 2024-01-15 10:30
        ]
        for fmt, expected_len in formats_with_lengths:
            try:
                val_truncated = val[:expected_len]
                datetime.strptime(val_truncated, fmt)
                return val
            except ValueError:
                continue
        return None

    # Try other formats
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(val, fmt)
            if "%H" in fmt or "%I" in fmt:
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            else:
                return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None


# =============================================================================
# Type Inference
# =============================================================================


def infer_postgres_type(field_name: str, sample_values: list[str]) -> str:
    """Infer PostgreSQL column type from field name and sample values."""
    field_lower = field_name.lower()

    # ID fields - verify with sample data
    if field_lower in ("id", "key", "count") or field_lower.endswith(
        ("_id", "_key", "_pk", "_count")
    ):
        non_empty = [v for v in sample_values[:10] if v and v.strip()]
        if non_empty:
            numeric_count = 0
            for val in non_empty:
                try:
                    int(float(val.replace(",", "")))
                    numeric_count += 1
                except ValueError:
                    pass
            if numeric_count < len(non_empty):
                return "TEXT"  # Has non-numeric values
        return "INTEGER"

    if any(x in field_lower for x in ("amount", "price", "cost", "revenue", "total")):
        return "NUMERIC(15,2)"

    if any(x in field_lower for x in ("score", "rating", "hours", "seconds", "minutes")):
        return "NUMERIC(10,2)"

    if field_lower.endswith(("_at", "_timestamp")):
        return "TIMESTAMP"

    if field_lower.endswith("_date") or field_lower in ("date", "created", "updated", "closed"):
        return "DATE"

    # Check sample values
    non_empty = [v for v in sample_values[:10] if v and v.strip()]
    if non_empty:
        numeric_count = 0
        has_decimal = False
        for val in non_empty:
            try:
                float(val.replace(",", ""))
                numeric_count += 1
                if "." in val:
                    has_decimal = True
            except ValueError:
                pass

        if numeric_count == len(non_empty):
            return "NUMERIC(15,2)" if has_decimal else "INTEGER"

        # Check for date patterns
        date_count = 0
        datetime_count = 0
        for val in non_empty[:5]:
            if DATETIME_PATTERN.match(val):
                datetime_count += 1
            elif DATE_PATTERN.match(val):
                date_count += 1

        if datetime_count >= len(non_empty[:5]) // 2 + 1:
            return "TIMESTAMP"
        if date_count >= len(non_empty[:5]) // 2 + 1:
            return "DATE"

    return "TEXT"


# =============================================================================
# Config Loading
# =============================================================================


def load_table_config() -> dict:
    """Load table name mappings from config file (data/tables.yaml)."""
    config = {"mappings": {}, "exclude": []}

    if not CONFIG_FILE.exists():
        return config

    if not HAS_YAML:
        print(f"Warning: {CONFIG_FILE} exists but PyYAML not installed. Using defaults.")
        return config

    try:
        with open(CONFIG_FILE) as f:
            loaded = yaml.safe_load(f) or {}
            config["mappings"] = loaded.get("mappings", {})
            config["exclude"] = loaded.get("exclude", [])
    except Exception as e:
        print(f"Warning: Failed to load {CONFIG_FILE}: {e}")

    return config


def get_table_name(filename: str, config: dict) -> str | None:
    """Get table name from config mapping, or None to use default.

    Returns the mapped table name if one exists in config, otherwise None
    to indicate the caller should use the default (CSV header or filename).
    """
    base_name = Path(filename).stem
    mappings = config.get("mappings") or {}
    if base_name in mappings:
        return mappings[base_name]
    return None  # No mapping - caller decides default


def get_table_name_with_fallback(filename: str, config: dict) -> str:
    """Get table name from config mapping, falling back to filename stem.

    Use this when you need a guaranteed table name (e.g., for model generation,
    listing files). For CSV loading, use get_table_name() to allow CSV headers
    to take precedence over filename.
    """
    mapped = get_table_name(filename, config)
    if mapped:
        return mapped
    return Path(filename).stem


def is_excluded(filename: str, config: dict) -> bool:
    """Check if a file should be excluded from loading."""
    base_name = Path(filename).stem
    exclude = config.get("exclude") or []
    return base_name in exclude


# =============================================================================
# CSV Parsing
# =============================================================================


def parse_csv_for_postgres(csv_path: Path) -> tuple[str, list[str], list[str], list[list]]:
    """Parse CSV file for PostgreSQL loading.

    Uses pandas for robust parsing (handles BOM, encoding, quoting).
    Falls back to stdlib csv if pandas not available.

    Returns: (table_name, field_names, column_types, rows)
    """
    if HAS_PANDAS:
        return _parse_csv_with_pandas(csv_path)
    else:
        return _parse_csv_with_stdlib(csv_path)


def _parse_csv_with_pandas(csv_path: Path) -> tuple[str, list[str], list[str], list[list]]:
    """Parse CSV using pandas for robust handling."""
    try:
        df = pd.read_csv(
            csv_path,
            encoding="utf-8-sig",  # Handles UTF-8 BOM
            dtype=str,
            keep_default_na=False,
            na_values=[],
        )
    except pd.errors.EmptyDataError:
        raise ValueError(f"CSV file is empty or has no headers: {csv_path}")

    if df.empty and len(df.columns) == 0:
        raise ValueError(f"CSV file is empty or has no headers: {csv_path}")

    headers = list(df.columns)

    def is_header_empty(h: str) -> bool:
        stripped = h.strip()
        if re.match(r"^\.\d+$", stripped):
            return True
        return stripped == ""

    if not headers or all(is_header_empty(h) for h in headers):
        raise ValueError(f"CSV file has empty headers: {csv_path}")

    # Extract table name from first header (format: view_name.field_name)
    if "." in headers[0]:
        table_name = headers[0].split(".")[0]
    else:
        table_name = csv_path.stem

    # Extract field names (strip view prefix)
    field_names = []
    for h in headers:
        if "." in h:
            field_names.append(h.split(".", 1)[1])
        else:
            field_names.append(h)

    df.columns = field_names

    # Collect samples for type inference
    field_samples: dict[str, list[str]] = {name: [] for name in field_names}
    for _, row in df.head(100).iterrows():
        for field_name in field_names:
            val = row[field_name]
            if val and str(val).strip():
                field_samples[field_name].append(str(val))

    column_types = [infer_postgres_type(name, field_samples.get(name, [])) for name in field_names]
    rows = df.values.tolist()

    return table_name, field_names, column_types, rows


def _parse_csv_with_stdlib(csv_path: Path) -> tuple[str, list[str], list[str], list[list]]:
    """Parse CSV using stdlib csv module (fallback)."""
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            headers = next(reader)
        except StopIteration:
            raise ValueError(f"CSV file is empty or has no headers: {csv_path}")

        if not headers or all(h.strip() == "" for h in headers):
            raise ValueError(f"CSV file has empty headers: {csv_path}")

        if "." in headers[0]:
            table_name = headers[0].split(".")[0]
        else:
            table_name = csv_path.stem

        field_names = []
        for h in headers:
            if "." in h:
                field_names.append(h.split(".", 1)[1])
            else:
                field_names.append(h)

        rows = list(reader)

        # Collect non-empty samples for type inference (matching pandas behavior)
        field_samples: dict[str, list[str]] = {name: [] for name in field_names}
        for row in rows[:100]:
            for i, val in enumerate(row):
                if i < len(field_names) and val and str(val).strip():
                    field_samples[field_names[i]].append(val)

        column_types = [
            infer_postgres_type(name, field_samples.get(name, [])) for name in field_names
        ]

    return table_name, field_names, column_types, rows


# =============================================================================
# JSON Parsing
# =============================================================================


def parse_json_for_postgres(
    json_path: Path, table_name: str | None = None
) -> tuple[str, list[str], list[str], list[list]]:
    """Parse JSON file for PostgreSQL loading.

    Supports:
    - Array of objects: [{"id": 1, "name": "foo"}, ...]
    - Object with data array: {"data": [...], "metadata": {...}}

    Returns: (table_name, field_names, column_types, rows)
    """
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        for key in ["data", "records", "items", "results", "rows"]:
            if key in data and isinstance(data[key], list):
                records = data[key]
                break
        else:
            raise ValueError(
                f"JSON object must have a 'data', 'records', 'items', 'results', "
                f"or 'rows' array: {json_path}"
            )
    else:
        raise ValueError(f"JSON must be array or object with data array: {json_path}")

    if not records:
        raise ValueError(f"JSON file has no records: {json_path}")

    if table_name is None:
        table_name = json_path.stem

    first_record = records[0]
    if not isinstance(first_record, dict):
        raise ValueError(f"JSON records must be objects: {json_path}")

    field_names = list(first_record.keys())

    field_samples: dict[str, list[str]] = {name: [] for name in field_names}
    for record in records[:100]:
        for field_name in field_names:
            val = record.get(field_name)
            if val is not None:
                field_samples[field_name].append(str(val))

    column_types = [infer_postgres_type(name, field_samples.get(name, [])) for name in field_names]

    rows = []
    for record in records:
        row = []
        for field in field_names:
            val = record.get(field)
            row.append(str(val) if val is not None else "")
        rows.append(row)

    return table_name, field_names, column_types, rows


# =============================================================================
# Database Operations
# =============================================================================


def create_table(
    conn, schema: str, table_name: str, field_names: list[str], column_types: list[str]
) -> None:
    """Create PostgreSQL table (drop and recreate if schema changed)."""
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))

        cur.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table_name),
        )
        existing_columns = {row[0]: row[1] for row in cur.fetchall()}
        table_exists = len(existing_columns) > 0

        if table_exists:
            type_map = {
                "INTEGER": "integer",
                "TEXT": "text",
                "NUMERIC(15,2)": "numeric",
                "NUMERIC(10,2)": "numeric",
                "DATE": "date",
                "TIMESTAMP": "timestamp without time zone",
            }

            schema_changed = False
            for name, col_type in zip(field_names, column_types):
                expected = type_map.get(col_type, "text")
                actual = existing_columns.get(name, "")
                if expected != actual:
                    schema_changed = True
                    break

            if schema_changed:
                cur.execute(
                    sql.SQL("DROP TABLE {}.{}").format(
                        sql.Identifier(schema), sql.Identifier(table_name)
                    )
                )
                table_exists = False
            else:
                cur.execute(
                    sql.SQL("TRUNCATE TABLE {}.{}").format(
                        sql.Identifier(schema), sql.Identifier(table_name)
                    )
                )

        if not table_exists:
            columns = []
            for name, col_type in zip(field_names, column_types):
                columns.append(sql.SQL("{} {}").format(sql.Identifier(name), sql.SQL(col_type)))

            create_stmt = sql.SQL("CREATE TABLE {}.{} ({})").format(
                sql.Identifier(schema),
                sql.Identifier(table_name),
                sql.SQL(", ").join(columns),
            )
            cur.execute(create_stmt)

        conn.commit()


def load_data(
    conn,
    schema: str,
    table_name: str,
    field_names: list[str],
    column_types: list[str],
    rows: list[list],
) -> int:
    """Load data into PostgreSQL table. Returns number of rows inserted."""
    if not rows:
        return 0

    with conn.cursor() as cur:
        converted_rows = []
        expected_cols = len(column_types)

        for row in rows:
            # Handle column count mismatches gracefully:
            # - Fewer columns: pad with empty values (become NULL)
            # - Extra columns: ignore (truncate to expected)
            if len(row) < expected_cols:
                # Pad with empty strings for missing columns
                row = list(row) + [""] * (expected_cols - len(row))
            # Extra columns are automatically ignored by only iterating expected_cols

            converted_row = []
            for i, col_type in enumerate(column_types):
                val = row[i] if i < len(row) else ""
                val_str = str(val) if val is not None else ""

                if not val_str or val_str.strip() == "":
                    converted_row.append(None)
                elif col_type == "INTEGER":
                    try:
                        converted_row.append(int(float(val_str.replace(",", ""))))
                    except ValueError:
                        converted_row.append(None)
                elif col_type.startswith("NUMERIC"):
                    try:
                        converted_row.append(float(val_str.replace(",", "")))
                    except ValueError:
                        converted_row.append(None)
                elif col_type in ("DATE", "TIMESTAMP"):
                    converted_row.append(_parse_date_to_iso(val_str))
                else:
                    converted_row.append(val_str)
            converted_rows.append(tuple(converted_row))

        if not converted_rows:
            return 0

        columns = sql.SQL(", ").join([sql.Identifier(name) for name in field_names])
        insert_stmt = sql.SQL("INSERT INTO {}.{} ({}) VALUES %s").format(
            sql.Identifier(schema),
            sql.Identifier(table_name),
            columns,
        )

        execute_values(cur, insert_stmt, converted_rows, page_size=1000)
        conn.commit()

        return len(converted_rows)


# =============================================================================
# LookML Generation
# =============================================================================

LOOKML_DIR = DATA_DIR / "lookml"


def _table_name_to_label(table_name: str) -> str:
    """Convert table_name to Title Case Label."""
    return " ".join(word.capitalize() for word in table_name.split("_"))


def generate_model_file(tables: list[str], model_name: str = "custom_model") -> str:
    """Generate LookML model file content for the given tables.

    Args:
        tables: List of table names to create explores for
        model_name: Name of the model (default: custom_model)

    Returns:
        LookML model file content as string
    """
    lines = [
        'connection: "@{database_connection}"',
        "",
        f'label: "{_table_name_to_label(model_name)}"',
        "",
        'include: "*.view.lkml"',
        "",
    ]

    # Deduplicate and sort tables
    for table in sorted(set(tables)):
        label = _table_name_to_label(table)
        lines.append(f"explore: {table} {{")
        lines.append(f'  label: "{label}"')
        lines.append("}")
        lines.append("")

    return "\n".join(lines)


def sync_model_file(tables: list[str] | None = None) -> Path:
    """Sync the LookML model file with current data files.

    If tables is None, discovers tables from CSV/JSON files.
    Writes to data/lookml/custom_model.model.lkml

    Args:
        tables: Optional list of table names (auto-discovers if None)

    Returns:
        Path to the generated model file
    """
    if tables is None:
        # Discover from data files
        config = load_table_config()
        tables = []
        for f in discover_data_files():
            if not is_excluded(f.name, config):
                tables.append(get_table_name_with_fallback(f.name, config))

    # Deduplicate table names (multiple files may map to same table)
    tables = list(dict.fromkeys(tables))

    content = generate_model_file(tables)

    model_path = LOOKML_DIR / "custom_model.model.lkml"
    LOOKML_DIR.mkdir(parents=True, exist_ok=True)
    model_path.write_text(content)

    return model_path


# =============================================================================
# File Discovery and Loading
# =============================================================================


def discover_data_files() -> list[Path]:
    """Discover all loadable data files (CSV and JSON)."""
    files = []
    if CSV_DATA_DIR.exists():
        files.extend(CSV_DATA_DIR.glob("*.csv"))
    if JSON_DATA_DIR.exists():
        files.extend(JSON_DATA_DIR.glob("*.json"))
    return sorted(files, key=lambda f: f.name)


def load_file(conn, file_path: Path, schema: str, config: dict) -> dict:
    """Load a single data file into PostgreSQL."""
    if is_excluded(file_path.name, config):
        return {"skipped": True, "reason": "excluded in config"}

    # Priority for table name:
    # 1. Config mapping (if exists)
    # 2. CSV header-derived name (for Looker-style headers like orders.id)
    # 3. Filename stem (fallback)
    config_table_name = get_table_name(file_path.name, config)

    suffix = file_path.suffix.lower()
    if suffix == ".csv":
        parsed_table, field_names, column_types, rows = parse_csv_for_postgres(file_path)
        # Use config mapping if set, otherwise use CSV header-derived name
        table_name = config_table_name if config_table_name else parsed_table
    elif suffix == ".json":
        # JSON uses config mapping or filename (no header-derived name)
        fallback_name = file_path.stem
        table_name = config_table_name if config_table_name else fallback_name
        _, field_names, column_types, rows = parse_json_for_postgres(file_path, table_name)
    else:
        return {"skipped": True, "reason": f"unsupported file type: {suffix}"}

    create_table(conn, schema, table_name, field_names, column_types)
    row_count = load_data(conn, schema, table_name, field_names, column_types, rows)

    return {
        "table": table_name,
        "rows": row_count,
        "columns": len(field_names),
        "source": file_path.name,
    }


def load_all_data(
    database_url: str,
    schema: str = "public",
    files: list[str] | None = None,
) -> dict:
    """Load all data files into PostgreSQL."""
    if not HAS_PSYCOPG2:
        return {"error": "psycopg2 not installed. Install with: pip install psycopg2-binary"}

    config = load_table_config()

    if files:
        all_files = []
        for f in files:
            csv_path = CSV_DATA_DIR / f
            json_path = JSON_DATA_DIR / f
            if csv_path.exists():
                all_files.append(csv_path)
            elif json_path.exists():
                all_files.append(json_path)
            else:
                print(f"Warning: File not found: {f}")
    else:
        all_files = discover_data_files()

    if not all_files:
        return {"error": "No data files found"}

    results = {"schema": schema, "tables": {}, "skipped": [], "total_rows": 0}

    conn = None
    try:
        conn = psycopg2.connect(database_url)

        for file_path in all_files:
            print(f"Processing {file_path.name}...")

            try:
                result = load_file(conn, file_path, schema, config)

                if result.get("skipped"):
                    results["skipped"].append({"file": file_path.name, "reason": result["reason"]})
                    print(f"  Skipped: {result['reason']}")
                else:
                    results["tables"][result["table"]] = {
                        "rows": result["rows"],
                        "columns": result["columns"],
                        "source": result["source"],
                    }
                    results["total_rows"] += result["rows"]
                    print(f"  Loaded {result['rows']} rows into {schema}.{result['table']}")

            except Exception as e:
                print(f"  Error: {e}")
                results["skipped"].append({"file": file_path.name, "reason": str(e)})

    except Exception as e:
        results["error"] = str(e)
    finally:
        if conn is not None:
            conn.close()

    return results


# =============================================================================
# CLI
# =============================================================================


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Load CSV and JSON data into PostgreSQL")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL connection URL (or set DATABASE_URL env var)",
    )
    parser.add_argument(
        "--schema",
        default=os.environ.get("DATABASE_SCHEMA", "public"),
        help="Database schema name (default: public)",
    )
    parser.add_argument(
        "--files",
        nargs="+",
        help="Specific files to load (default: all CSV and JSON files)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_files",
        help="List available data files and exit",
    )
    parser.add_argument(
        "--sync-model",
        action="store_true",
        dest="sync_model",
        help="Sync LookML model file with data files (no database required)",
    )

    args = parser.parse_args()

    if args.list_files:
        print("Available data files:")
        print()
        config = load_table_config()

        files = discover_data_files()
        if not files:
            print("  (none found)")
            return

        for f in files:
            mapped_name = get_table_name(f.name, config)
            excluded = is_excluded(f.name, config)
            status = " [excluded]" if excluded else ""
            mapping = f" -> {mapped_name}" if mapped_name else ""
            print(f"  {f.name}{mapping}{status}")

        if CONFIG_FILE.exists():
            print()
            print(f"Config: {CONFIG_FILE}")
        else:
            print()
            print(f"No config file. Create {CONFIG_FILE} to customize table names.")
        return

    if args.sync_model:
        print("Syncing LookML model file...")
        model_path = sync_model_file()
        print(f"Updated: {model_path}")
        return

    if not args.database_url:
        print("Error: DATABASE_URL not set")
        print("Usage: python data_to_postgres.py --database-url postgresql://user:pass@host/db")
        sys.exit(1)

    if not HAS_PSYCOPG2:
        print("Error: psycopg2 not installed")
        print("Install with: pip install psycopg2-binary")
        sys.exit(1)

    # Auto-sync LookML model before loading data
    print("Syncing LookML model...")
    model_path = sync_model_file()
    print(f"  Updated: {model_path}")
    print()

    print(f"Loading data files into PostgreSQL schema '{args.schema}'...")
    print("Data directories:")
    print(f"  CSV:  {CSV_DATA_DIR}")
    print(f"  JSON: {JSON_DATA_DIR}")
    if HAS_PANDAS:
        print("Using pandas for CSV parsing")
    if HAS_YAML and CONFIG_FILE.exists():
        print(f"Using config: {CONFIG_FILE}")
    print()

    results = load_all_data(args.database_url, args.schema, args.files)

    if "error" in results:
        print(f"Error: {results['error']}")
        sys.exit(1)

    print()
    print(f"Successfully loaded {results['total_rows']} total rows")
    print(f"Tables created in schema '{results['schema']}':")
    for table, info in results["tables"].items():
        print(f"  {table}: {info['rows']} rows ({info['source']})")

    if results["skipped"]:
        print()
        print("Skipped files:")
        for skip in results["skipped"]:
            print(f"  {skip['file']}: {skip['reason']}")


if __name__ == "__main__":
    main()
