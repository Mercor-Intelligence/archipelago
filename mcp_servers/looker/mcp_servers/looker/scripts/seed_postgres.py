#!/usr/bin/env python3
"""Seed PostgreSQL database from CSV files for offline mode.

This script reads all CSV files from data/csv/ and creates tables in PostgreSQL
with data matching the CSV contents. Column names are normalized to remove
Looker-style prefixes (e.g., 'orders.order_id' -> 'order_id').

Usage:
    OFFLINE_POSTGRES_URL='postgresql://...' python scripts/seed_postgres.py

Or set the URL in .env file and run:
    python scripts/seed_postgres.py
"""

import asyncio
import csv
import os
import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncpg


def normalize_column_name(col: str, table_name: str) -> str:
    """Remove Looker-style table prefix from column name."""
    prefix = f"{table_name}."
    if col.startswith(prefix):
        return col[len(prefix) :]
    return col


def infer_postgres_type(values: list[str]) -> str:
    """Infer PostgreSQL column type from sample values."""
    non_empty = [v for v in values if v.strip()]
    if not non_empty:
        return "TEXT"

    # Try integer
    try:
        for v in non_empty[:100]:
            int(v)
        return "BIGINT"
    except ValueError:
        pass

    # Try float
    try:
        for v in non_empty[:100]:
            float(v)
        return "DOUBLE PRECISION"
    except ValueError:
        pass

    return "TEXT"


def convert_value(val: str, col_type: str):
    """Convert string value to appropriate Python type."""
    if not val.strip():
        return None
    if col_type == "BIGINT":
        return int(val)
    if col_type == "DOUBLE PRECISION":
        return float(val)
    return val


async def seed_database(csv_dir: Path, postgres_url: str) -> None:
    """Seed PostgreSQL database from CSV files."""
    # Parse the connection URL - asyncpg needs special handling for some params
    # Remove channel_binding parameter as asyncpg doesn't support it directly
    clean_url = postgres_url.replace("&channel_binding=require", "")

    conn = await asyncpg.connect(clean_url)
    print("Connected to PostgreSQL")

    csv_files = sorted(csv_dir.glob("*.csv"))
    print(f"Found {len(csv_files)} CSV files")

    for csv_file in csv_files:
        table_name = csv_file.stem
        print(f"\nProcessing {table_name}...")

        with open(csv_file, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            raw_headers = next(reader)
            headers = [normalize_column_name(h, table_name) for h in raw_headers]
            rows = list(reader)
            print(f"  {len(rows)} rows, {len(headers)} columns")

        if not rows:
            print("  Skipping empty table")
            continue

        # Infer column types
        col_types = []
        for i, header in enumerate(headers):
            values = [row[i] for row in rows if i < len(row)]
            col_type = infer_postgres_type(values)
            col_types.append(col_type)

        # Drop existing table
        await conn.execute(f'DROP TABLE IF EXISTS "{table_name}" CASCADE')

        # Create table
        columns_def = ", ".join(f'"{h}" {t}' for h, t in zip(headers, col_types))
        create_sql = f'CREATE TABLE "{table_name}" ({columns_def})'
        await conn.execute(create_sql)
        print("  Created table")

        # Prepare data for bulk insert
        converted_rows = []
        for row in rows:
            converted = tuple(convert_value(val, col_type) for val, col_type in zip(row, col_types))
            converted_rows.append(converted)

        # Bulk insert using COPY
        await conn.copy_records_to_table(
            table_name,
            records=converted_rows,
            columns=headers,
        )
        print(f"  Inserted {len(converted_rows)} rows")

    await conn.close()
    print("\n✓ Database seeded successfully")


async def main():
    # Get Postgres URL from environment or .env
    postgres_url = os.getenv("OFFLINE_POSTGRES_URL")

    if not postgres_url:
        # Try loading from config
        try:
            from config import LookerSettings

            settings = LookerSettings()
            postgres_url = settings.offline_postgres_url
        except Exception:
            pass

    if not postgres_url:
        print("Error: OFFLINE_POSTGRES_URL not set")
        print("Set it via environment variable or in .env file")
        return 1

    script_dir = Path(__file__).parent
    base_dir = script_dir.parent
    csv_dir = base_dir / "data" / "csv"

    if not csv_dir.exists():
        print(f"Error: CSV directory not found: {csv_dir}")
        return 1

    await seed_database(csv_dir, postgres_url)
    return 0


if __name__ == "__main__":
    exit(asyncio.run(main()))
