#!/usr/bin/env python
"""
Populate the SQLite database from CSV files.

This script reads all CSV files from STATE_LOCATION and loads them
into the SQLite database as tables.
"""

import csv
import sqlite3
import sys
from pathlib import Path

from loguru import logger

# Add parent directory to path so we can import from utils
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.config import DB_PATH, STATE_LOCATION


def _infer_sqlite_type(value: str) -> str:
    """Infer SQLite type from a string value."""
    if not value:
        return "TEXT"
    try:
        int(value)
        return "INTEGER"
    except ValueError:
        pass
    try:
        float(value)
        return "REAL"
    except ValueError:
        pass
    return "TEXT"


def _load_csv_to_sqlite(
    csv_path: Path, table_name: str, conn: sqlite3.Connection
) -> int:
    """
    Load a CSV file into a SQLite table.

    Returns the number of rows loaded.
    """
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        headers = next(reader)

        if not headers:
            logger.warning(f"CSV file {csv_path} has no headers, skipping")
            return 0

        # Read all rows to infer types and prepare for insertion
        rows = list(reader)

        if not rows:
            # Create table with TEXT columns if no data rows
            col_defs = ", ".join(f'"{h}" TEXT' for h in headers)
        else:
            # Infer column types from first row with data
            col_types = [_infer_sqlite_type(val) for val in rows[0]]
            col_defs = ", ".join(
                f'"{h}" {t}' for h, t in zip(headers, col_types, strict=False)
            )

        # Drop existing table and create new one
        cursor = conn.cursor()
        cursor.execute(f'DROP TABLE IF EXISTS "{table_name}"')
        cursor.execute(f'CREATE TABLE "{table_name}" ({col_defs})')

        # Insert all rows
        if rows:
            placeholders = ", ".join("?" for _ in headers)
            cursor.executemany(
                f'INSERT INTO "{table_name}" VALUES ({placeholders})', rows
            )

        conn.commit()
        return len(rows)


def populate_database():
    """
    Initialize SQLite database and load CSV files as tables.
    """
    # Connect to SQLite database (creates it if it doesn't exist)
    conn = sqlite3.connect(DB_PATH)

    try:
        # Find all CSV files in the STATE_LOCATION directory (recursively)
        root_path = Path(STATE_LOCATION)

        # Load CSV files (include files in subdirectories)
        for csv_file in root_path.rglob("*.csv"):
            table_name = csv_file.stem  # filename without extension
            logger.info(f"Loading CSV file {csv_file} as table '{table_name}'")
            try:
                row_count = _load_csv_to_sqlite(csv_file, table_name, conn)
                logger.info(
                    f"Successfully loaded {row_count} rows into table '{table_name}'"
                )
            except Exception as e:
                logger.error(f"Error loading CSV file {csv_file}: {e}")

    finally:
        conn.close()

    logger.info(f"Database populated at {DB_PATH}")


if __name__ == "__main__":
    populate_database()
