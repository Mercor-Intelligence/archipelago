#!/usr/bin/env python3
"""Build the bundled DuckDB database from CSV files in data/csv/.

This script creates the pre-built DuckDB that ships with the repo.
Run this when the bundled CSV files change.

The bundled DB is copied to STATE_LOCATION at runtime by data_layer.py.
User uploads are added to that runtime copy, not this bundled DB.

Usage:
    python scripts/build_duckdb.py       # Build bundled DB
    python scripts/build_duckdb.py -f    # Force rebuild (delete and recreate)
"""

import csv
import re
import sys
from pathlib import Path

import duckdb


def sanitize_column_name(col: str) -> str:
    """Sanitize a column name to be a valid SQL identifier."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", col)
    sanitized = re.sub(r"_+", "_", sanitized)
    sanitized = sanitized.strip("_")
    if sanitized and sanitized[0].isdigit():
        sanitized = f"col_{sanitized}"
    if not sanitized:
        sanitized = "unnamed_column"
    return sanitized


def normalize_column_name(col: str, table_name: str) -> str:
    """Remove Looker-style table prefix and sanitize column name."""
    prefix = f"{table_name}."
    if col.startswith(prefix):
        col = col[len(prefix) :]
    return sanitize_column_name(col)


def _quote_ident(name: str) -> str:
    """Safely quote a SQL identifier by escaping embedded double quotes."""
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def load_csv_to_table(conn: duckdb.DuckDBPyConnection, csv_file: Path) -> bool:
    """Load a CSV file into the database as a table.

    Uses DuckDB's native read_csv_auto for fast, multithreaded CSV loading
    with automatic type inference. Column names are normalized to strip
    Looker-style prefixes and sanitize special characters.
    """
    table_name = csv_file.stem

    # Read just the header row for column name normalization
    # Use utf-8-sig to strip BOM if present, matching DuckDB's behavior
    with open(csv_file, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            raw_headers = next(reader)
        except StopIteration:
            print(f"  Skipping empty CSV: {csv_file.name}", file=sys.stderr)
            return False

    if not raw_headers:
        print(f"  Skipping empty CSV: {csv_file.name}", file=sys.stderr)
        return False

    normalized = [normalize_column_name(h, table_name) for h in raw_headers]

    # Build SELECT with aliases to map raw header names to normalized names
    aliases = ", ".join(
        f"{_quote_ident(raw)} AS {_quote_ident(norm)}" for raw, norm in zip(raw_headers, normalized)
    )

    # Use DuckDB's native CSV reader (C++ engine, multithreaded)
    # - Restrict type candidates to BIGINT/DOUBLE/VARCHAR to match the old Python loader
    #   (without this, DuckDB also infers DATE/TIMESTAMP which breaks ILIKE filters)
    # - null_padding=true treats whitespace-only values as NULL, matching old loader behavior
    csv_path_escaped = str(csv_file).replace("'", "''")
    conn.execute(f"""
        CREATE OR REPLACE TABLE {_quote_ident(table_name)} AS
        SELECT {aliases}
        FROM read_csv_auto('{csv_path_escaped}', header=true,
             auto_type_candidates=['BIGINT', 'DOUBLE', 'VARCHAR'],
             null_padding=true)
    """)

    # Verify non-empty
    count = conn.execute(f"SELECT COUNT(*) FROM {_quote_ident(table_name)}").fetchone()[0]
    if count == 0:
        conn.execute(f"DROP TABLE IF EXISTS {_quote_ident(table_name)}")
        print(f"  Skipping empty table: {table_name}", file=sys.stderr)
        return False

    print(f"  {count} rows, {len(normalized)} columns", file=sys.stderr)
    return True


def build_bundled_db(force_rebuild: bool = False) -> int:
    """Build the bundled DuckDB from data/csv/*.csv files."""
    base_dir = Path(__file__).parent.parent
    db_path = base_dir / "data" / "offline.duckdb"
    csv_dir = base_dir / "data" / "csv"

    csv_files = list(csv_dir.glob("*.csv")) if csv_dir.exists() else []

    print(f"Building bundled DuckDB: {db_path}", file=sys.stderr)
    print(f"Source: {csv_dir} ({len(csv_files)} files)", file=sys.stderr)

    if force_rebuild and db_path.exists():
        db_path.unlink()
        print("Deleted existing database", file=sys.stderr)

    conn = duckdb.connect(str(db_path))
    try:
        tables_added = 0
        for csv_file in sorted(csv_files):
            print(f"\nProcessing {csv_file.name}...", file=sys.stderr)
            if load_csv_to_table(conn, csv_file):
                tables_added += 1

        print(f"\n✓ Built {tables_added} tables", file=sys.stderr)
        print(f"  Database: {db_path}", file=sys.stderr)
        print(f"  Size: {db_path.stat().st_size / 1024 / 1024:.2f} MB", file=sys.stderr)
        return 0
    finally:
        conn.close()


def add_csvs_to_db(db_path: Path, csv_dir: Path) -> int:
    """Add CSV files to an existing DuckDB database.

    This is used at runtime to add user-uploaded CSVs to the runtime database.
    Unlike build_bundled_db, this operates on an existing database and only
    adds new tables (or replaces existing ones with same name).

    Args:
        db_path: Path to the DuckDB database to modify
        csv_dir: Directory containing CSV files to add (searched recursively)

    Returns:
        Number of tables added/updated
    """
    csv_files = list(csv_dir.rglob("*.csv"))  # Search recursively
    if not csv_files:
        return 0

    conn = duckdb.connect(str(db_path))
    try:
        tables_added = 0
        for csv_file in sorted(csv_files):
            if load_csv_to_table(conn, csv_file):
                tables_added += 1
        return tables_added
    finally:
        conn.close()


def main():
    force_rebuild = "--force" in sys.argv or "-f" in sys.argv
    return build_bundled_db(force_rebuild=force_rebuild)


if __name__ == "__main__":
    exit(main())
