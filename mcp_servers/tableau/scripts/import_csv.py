#!/usr/bin/env python3
"""CSV to Database Import Script

This script imports CSV files from a specified folder into the database.
- The CSV filename determines the table name (e.g., accounts.csv -> accounts table)
- If the table doesn't exist, it will be created dynamically
- If the table exists, data will be inserted
- If insertion fails, the script will error out

Usage:
    python import_csv.py [--dir DIRECTORY] [--db DATABASE] [csv_file1.csv csv_file2.csv ...]

    If no directory is specified, uses the script's directory.
    If no database is specified, uses /.apps_data/tableau/data.db.
    If no files are specified, all CSV files in the directory will be imported.

Examples:
    python import_csv.py                                    # imports all CSV files from current directory
    python import_csv.py accounts.csv customers.csv         # imports specific files from current directory
    python import_csv.py --dir /path/to/csvs               # imports all CSV files from specified directory
    python import_csv.py --dir ./data accounts.csv         # imports specific file from ./data directory
    python import_csv.py --db /path/to/custom.db           # use custom database path
"""

import argparse
import asyncio
import csv
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import Column, DateTime, Integer, Numeric, String, Table, Text, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

# Add parent directory to path to import db modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_servers.tableau.db.models import Base

# Configuration
SCRIPT_DIR = Path(__file__).parent


class CSVImportError(Exception):
    """Exception raised when CSV import fails."""

    pass


def infer_column_type(value: str) -> type:
    """Infer SQLAlchemy column type from a string value.

    Args:
        value: String value to analyze

    Returns:
        SQLAlchemy column type
    """
    if not value or value.strip() == "":
        return String(255)  # Default to string for empty values

    value = value.strip()

    # Try to parse as integer
    try:
        int(value)
        return Integer
    except ValueError:
        pass

    # Try to parse as decimal/float
    try:
        Decimal(value.replace("$", "").replace(",", ""))
        return Numeric(15, 2)
    except Exception:
        pass

    # Try to parse as datetime
    date_formats = [
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y",
        "%d/%m/%Y",
        "%Y/%m/%d",
    ]
    for fmt in date_formats:
        try:
            datetime.strptime(value, fmt)
            return DateTime
        except ValueError:
            continue

    # Default to Text for long strings, String for short ones
    if len(value) > 255:
        return Text
    return String(255)


def parse_value(value: str, column_type: type) -> Any:
    """Parse a string value into the appropriate Python type.

    Args:
        value: String value to parse
        column_type: SQLAlchemy column type

    Returns:
        Parsed value in appropriate Python type
    """
    if not value or value.strip() == "":
        return None

    value = value.strip()

    try:
        if (
            column_type == Integer
            or isinstance(column_type, type)
            and column_type.__name__ == "Integer"
        ):
            return int(value)
        elif isinstance(column_type, Numeric) or (
            isinstance(column_type, type) and column_type.__name__ == "Numeric"
        ):
            # Remove currency symbols and commas
            cleaned = value.replace("$", "").replace(",", "")
            # Convert to float for SQLite compatibility (aiosqlite doesn't support Decimal)
            return float(cleaned)
        elif column_type == DateTime or (
            isinstance(column_type, type) and column_type.__name__ == "DateTime"
        ):
            # Try multiple date formats
            date_formats = [
                "%Y-%m-%d",
                "%Y-%m-%d %H:%M:%S",
                "%m/%d/%Y",
                "%d/%m/%Y",
                "%Y/%m/%d",
            ]
            for fmt in date_formats:
                try:
                    return datetime.strptime(value, fmt)
                except ValueError:
                    continue
            raise ValueError(f"Could not parse date: {value}")
        else:
            return value
    except Exception as e:
        logger.warning(f"Failed to parse value '{value}' as {column_type}: {e}")
        return value  # Return as string if parsing fails


def read_csv_file(csv_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read CSV file and return headers and rows.

    Args:
        csv_path: Path to CSV file

    Returns:
        Tuple of (headers, rows)

    Raises:
        CSVImportError: If CSV file is invalid
    """
    try:
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames
            if not headers:
                raise CSVImportError(f"CSV file {csv_path.name} has no headers")

            # Lowercase all headers for consistency
            headers = [h.lower() for h in headers]

            # Read rows with original case, then normalize keys to lowercase
            rows = []
            for row in reader:
                rows.append({k.lower(): v for k, v in row.items()})

            if not rows:
                raise CSVImportError(f"CSV file {csv_path.name} has no data rows")

            logger.info(f"Read {len(rows)} rows from {csv_path.name}")
            return headers, rows

    except csv.Error as e:
        raise CSVImportError(f"Invalid CSV format in {csv_path.name}: {e}")
    except Exception as e:
        raise CSVImportError(f"Failed to read {csv_path.name}: {e}")


def infer_column_types(headers: list[str], rows: list[dict[str, str]]) -> dict[str, type]:
    """Infer column types by analyzing sample data.

    Args:
        headers: List of column names
        rows: List of data rows

    Returns:
        Dictionary mapping column names to SQLAlchemy types
    """
    column_types = {}

    # Sample first few rows to infer types
    sample_size = min(10, len(rows))
    sample_rows = rows[:sample_size]

    for header in headers:
        # Collect non-empty values for this column
        values = [row[header] for row in sample_rows if row[header].strip()]

        if not values:
            # No non-empty values, default to String
            column_types[header] = String(255)
            continue

        # Infer type from first non-empty value
        column_types[header] = infer_column_type(values[0])

    return column_types


async def create_table_from_csv(
    table_name: str,
    headers: list[str],
    column_types: dict[str, type],
    engine,
) -> Table:
    """Create a new table based on CSV structure.

    Args:
        table_name: Name of the table to create
        headers: List of column names
        column_types: Dictionary mapping column names to SQLAlchemy types
        engine: SQLAlchemy engine

    Returns:
        Created SQLAlchemy Table object

    Raises:
        CSVImportError: If table creation fails
    """
    try:
        # Create columns
        columns = []

        # Add id column as primary key if not present
        if "id" not in headers:
            columns.append(Column("id", Integer, primary_key=True, autoincrement=True))

        # Add other columns
        for header in headers:
            col_name = header.replace(" ", "_")
            col_type = column_types.get(header, String(255))

            # If column is named 'id', make it primary key
            if col_name == "id":
                columns.append(Column(col_name, col_type, primary_key=True))
            else:
                columns.append(Column(col_name, col_type, nullable=True))

        # Create table
        table = Table(table_name, Base.metadata, *columns, extend_existing=True)

        # Create table in database
        async with engine.begin() as conn:
            await conn.run_sync(table.create, checkfirst=True)

        logger.info(f"Created table '{table_name}' with {len(columns)} columns")
        return table

    except Exception as e:
        raise CSVImportError(f"Failed to create table '{table_name}': {e}")


async def table_exists(table_name: str, engine) -> bool:
    """Check if a table exists in the database.

    Args:
        table_name: Name of the table
        engine: SQLAlchemy engine

    Returns:
        True if table exists, False otherwise
    """
    async with engine.connect() as conn:
        result = await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table(table_name))
        return result


async def insert_rows(
    table_name: str,
    headers: list[str],
    column_types: dict[str, type],
    rows: list[dict[str, str]],
    engine,
) -> int:
    """Insert rows into the table.

    Args:
        table_name: Name of the table
        headers: List of column names
        column_types: Dictionary mapping column names to SQLAlchemy types
        rows: List of data rows
        engine: SQLAlchemy engine

    Returns:
        Number of rows inserted

    Raises:
        CSVImportError: If insertion fails
    """
    try:
        inserted_count = 0

        async with engine.begin() as conn:
            for row_num, row in enumerate(rows, start=2):  # Start at 2 (after header)
                try:
                    # Build column names and values
                    columns = []
                    values = []

                    for header in headers:
                        col_name = header.replace(" ", "_")
                        col_type = column_types.get(header, String(255))
                        parsed_value = parse_value(row[header], col_type)

                        columns.append(col_name)
                        values.append(parsed_value)

                    # Build INSERT OR IGNORE statement to handle duplicates gracefully
                    col_list = ", ".join(columns)
                    placeholders = ", ".join([f":{col}" for col in columns])
                    query = (
                        f"INSERT OR IGNORE INTO {table_name} ({col_list}) VALUES ({placeholders})"
                    )

                    # Execute insert
                    params = dict(zip(columns, values))
                    result = await conn.execute(text(query), params)
                    # Only count rows that were actually inserted (not ignored due to constraints)
                    inserted_count += result.rowcount

                except Exception as e:
                    error_msg = f"Row {row_num}: Failed to insert - {e}\nData: {row}"
                    logger.error(error_msg)
                    raise CSVImportError(error_msg)

        logger.info(f"Successfully inserted {inserted_count} rows into '{table_name}'")
        return inserted_count

    except CSVImportError:
        raise
    except Exception as e:
        raise CSVImportError(f"Failed to insert rows into '{table_name}': {e}")


async def find_matching_table(headers: list[str], engine) -> str | None:
    """Find an existing table that matches the CSV headers.

    Args:
        headers: List of column names from CSV (normalized to lowercase)
        engine: SQLAlchemy engine

    Returns:
        Table name if match found, None otherwise
    """
    async with engine.connect() as conn:
        # Get all table names and their columns
        def get_tables_and_columns(sync_conn):
            inspector = inspect(sync_conn)
            table_schemas = {}
            for table_name in inspector.get_table_names():
                columns = inspector.get_columns(table_name)
                # Get column names, normalized to lowercase
                col_names = [col["name"].lower() for col in columns]
                table_schemas[table_name] = set(col_names)
            return table_schemas

        table_schemas = await conn.run_sync(get_tables_and_columns)

        # Convert CSV headers to set for comparison (handle space to underscore conversion)
        csv_columns = set(h.replace(" ", "_") for h in headers)

        # Find exact match
        for table_name, table_columns in table_schemas.items():
            # Check if columns match (excluding auto-generated id if not in CSV)
            table_cols_to_match = table_columns.copy()

            # If CSV doesn't have 'id' but table does (and it's auto-generated), ignore it
            if "id" not in csv_columns and "id" in table_cols_to_match:
                table_cols_to_match.discard("id")

            if csv_columns == table_cols_to_match:
                logger.info(
                    f"Found matching table '{table_name}' for columns: {', '.join(sorted(csv_columns))}"
                )
                return table_name

        return None


async def import_csv_file(csv_path: Path, engine) -> dict[str, Any]:
    """Import a single CSV file into the database.

    Args:
        csv_path: Path to CSV file
        engine: SQLAlchemy engine

    Returns:
        Dictionary with import statistics

    Raises:
        CSVImportError: If import fails
    """
    logger.info(f"Starting import of {csv_path.name}")

    # Read CSV file
    headers, rows = read_csv_file(csv_path)
    logger.info(f"CSV has {len(headers)} columns: {', '.join(headers)}")

    # Try to find existing table with matching columns
    matching_table = await find_matching_table(headers, engine)

    if matching_table:
        table_name = matching_table
        logger.info(f"Using existing table: '{table_name}' (matched by column structure)")
    else:
        # No match found, use filename as table name
        table_name = csv_path.stem.lower()
        logger.info(f"No matching table found. Using filename-based table name: '{table_name}'")

    # Infer column types
    column_types = infer_column_types(headers, rows)
    for header, col_type in column_types.items():
        type_name = col_type.__class__.__name__ if hasattr(col_type, "__class__") else str(col_type)
        logger.debug(f"  {header}: {type_name}")

    # Check if table exists
    exists = await table_exists(table_name, engine)

    if not exists:
        logger.info(f"Table '{table_name}' does not exist. Creating...")
        await create_table_from_csv(table_name, headers, column_types, engine)
    else:
        logger.info(f"Table '{table_name}' already exists. Inserting data...")

    # Insert rows
    inserted_count = await insert_rows(table_name, headers, column_types, rows, engine)

    return {
        "file": csv_path.name,
        "table": table_name,
        "rows_inserted": inserted_count,
        "created": not exists,
    }


async def import_all_csv_files(
    csv_files: list[Path] | None = None,
    csv_dir: Path | None = None,
    db_path: Path | None = None,
):
    """Import all CSV files from the specified folder.

    Args:
        csv_files: Optional list of specific CSV files to import.
                   If None, imports all CSV files in csv_dir folder.
        csv_dir: Directory to look for CSV files. Defaults to SCRIPT_DIR.
        db_path: Path to database file. Defaults to /.apps_data/tableau/data.db.
    """
    # Set directory to search for CSV files
    if csv_dir is None:
        csv_dir = SCRIPT_DIR

    # Set database path
    if db_path is None:
        db_path = Path("/.apps_data/tableau/data.db")

    # Ensure the database directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    database_url = f"sqlite+aiosqlite:///{db_path}"

    logger.info(f"Using database: {db_path}")

    # Create database engine
    # echo=False to prevent SQL logs from mixing with output
    engine = create_async_engine(database_url, echo=False)

    try:
        # Get list of CSV files to import
        if csv_files is None:
            csv_files = sorted(csv_dir.glob("*.csv"))

        if not csv_files:
            logger.warning(f"No CSV files found in {csv_dir}")
            return

        logger.info(f"Found {len(csv_files)} CSV file(s) to import")

        # Import each file
        results = []
        errors = []

        # Files to skip (migration metadata, not actual data)
        skip_files = {"alembic_version.csv"}

        for csv_path in csv_files:
            # Skip migration metadata files
            if csv_path.name in skip_files:
                logger.info(f"Skipping {csv_path.name} (migration metadata)")
                continue

            try:
                result = await import_csv_file(csv_path, engine)
                results.append(result)
                logger.success(
                    f"✓ {result['file']}: {result['rows_inserted']} rows "
                    f"→ table '{result['table']}' "
                    f"({'created' if result['created'] else 'existing'})"
                )
            except CSVImportError as e:
                error_str = str(e)
                # Don't treat empty CSVs as fatal errors
                if "has no data rows" in error_str:
                    logger.warning(f"⊘ {csv_path.name}: empty file, skipping")
                else:
                    errors.append((csv_path.name, error_str))
                    logger.error(f"✗ {csv_path.name}: {e}")

        # Print summary
        logger.info(
            f"[IMPORT SUMMARY] Total files processed: {len(csv_files)}, successfully imported: {len(results)}, failed: {len(errors)}"
        )

        if results:
            for result in results:
                logger.info(
                    f"  • {result['file']} → {result['table']} ({result['rows_inserted']} rows)"
                )

        if errors:
            logger.error("\nFailed imports:")
            for filename, error in errors:
                logger.error(f"  • {filename}: {error}")
            sys.exit(1)

    finally:
        await engine.dispose()


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Import CSV files into the database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                    # Import all CSV files from script directory
  %(prog)s accounts.csv customers.csv         # Import specific files from script directory
  %(prog)s --dir /path/to/csvs               # Import all CSV files from specified directory
  %(prog)s --dir ./data accounts.csv         # Import specific file from ./data directory
  %(prog)s --db /path/to/custom.db           # Use custom database path
        """,
    )
    parser.add_argument(
        "--dir",
        "--directory",
        dest="directory",
        type=str,
        help="Directory containing CSV files to import (default: script directory)",
    )
    parser.add_argument(
        "--db",
        "--database",
        dest="database",
        type=str,
        help="Database file path (default: /.apps_data/tableau/data.db)",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Specific CSV files to import (if not specified, imports all CSV files in directory)",
    )
    return parser.parse_args()


def main():
    """Main entry point."""
    # Configure logging
    logger.remove()  # Remove default handler
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO",
    )

    # Parse command line arguments
    args = parse_args()

    # Determine the directory to search for CSV files
    csv_dir = Path(args.directory) if args.directory else SCRIPT_DIR
    if not csv_dir.is_absolute():
        csv_dir = Path.cwd() / csv_dir

    # Validate directory exists
    if not csv_dir.exists():
        logger.error(f"Directory not found: {csv_dir}")
        sys.exit(1)
    if not csv_dir.is_dir():
        logger.error(f"Not a directory: {csv_dir}")
        sys.exit(1)

    logger.info(f"Looking for CSV files in: {csv_dir}")

    # Determine database path
    db_path = None
    if args.database:
        db_path = Path(args.database)
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path

    # Parse specific files if provided
    csv_files = None
    if args.files:
        csv_files = []
        for filename in args.files:
            # Support both absolute paths and filenames
            csv_path = Path(filename)
            if not csv_path.is_absolute():
                csv_path = csv_dir / filename
            csv_files.append(csv_path)

        # Validate files exist
        for csv_path in csv_files:
            if not csv_path.exists():
                logger.error(f"File not found: {csv_path}")
                sys.exit(1)
            if csv_path.suffix.lower() != ".csv":
                logger.error(f"Not a CSV file: {csv_path}")
                sys.exit(1)

    # Run import
    try:
        asyncio.run(import_all_csv_files(csv_files, csv_dir, db_path))
    except KeyboardInterrupt:
        logger.warning("\nImport cancelled by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Import failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
