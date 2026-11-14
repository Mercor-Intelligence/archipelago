#!/usr/bin/env python3
"""CSV to Database Import Script

This script imports CSV files from a specified folder into the database.
- The CSV filename determines the table name (e.g., accounts.csv -> accounts table)
- If the table doesn't exist, it will be created dynamically
- If the table exists, data will be inserted
- If insertion fails, the script will error out

Usage:
    python import_csv.py [--dir DIRECTORY] [csv_file1.csv csv_file2.csv ...]

    If no directory is specified, uses the script's directory.
    If no files are specified, all CSV files in the directory will be imported.

Examples:
    python import_csv.py                                 # imports all CSV files from current dir
    python import_csv.py accounts.csv customers.csv      # imports specific files from current dir
    python import_csv.py --dir /path/to/csvs             # imports all CSV files from specified dir
    python import_csv.py --dir ./data accounts.csv       # imports specific file from ./data dir
"""

import argparse
import asyncio
import csv
import re
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    insert,
    inspect,
)
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import declarative_base

# Create base for SQLAlchemy models
Base = declarative_base()

# Configuration
SCRIPT_DIR = Path(__file__).parent


class CSVImportError(Exception):
    """Exception raised when CSV import fails."""

    pass


def sanitize_sql_identifier(identifier: str, identifier_type: str = "identifier") -> str:
    """Sanitize and validate a SQL identifier (table or column name).

    This prevents SQL injection by ensuring identifiers contain only safe characters.
    Valid SQL identifiers must:
    - Start with a letter or underscore
    - Contain only alphanumeric characters and underscores
    - Not be empty

    Args:
        identifier: The identifier to sanitize
        identifier_type: Type of identifier (for error messages)

    Returns:
        The validated identifier

    Raises:
        CSVImportError: If identifier contains invalid characters
    """
    if not identifier:
        raise CSVImportError(f"Empty {identifier_type} is not allowed")

    # SQL identifiers should only contain alphanumeric characters and underscores
    # and must start with a letter or underscore
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", identifier):
        raise CSVImportError(
            f"Invalid {identifier_type} '{identifier}': SQL identifiers must start with "
            f"a letter or underscore and contain only alphanumeric characters and underscores. "
            f"This protects against SQL injection attacks."
        )

    # Additional check: prevent reserved SQL keywords (basic list)
    reserved_keywords = {
        "select",
        "insert",
        "update",
        "delete",
        "drop",
        "create",
        "alter",
        "table",
        "database",
        "index",
        "view",
        "trigger",
        "procedure",
        "function",
        "from",
        "where",
        "join",
        "union",
        "order",
        "group",
    }
    if identifier.lower() in reserved_keywords:
        raise CSVImportError(
            f"Invalid {identifier_type} '{identifier}': This is a reserved SQL keyword. "
            f"Please rename the column/file to avoid conflicts."
        )

    return identifier


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

            # Check for case-variant duplicate headers before lowercasing
            original_headers = list(headers)
            lowercase_headers = [h.lower() for h in headers]

            # Detect duplicates after lowercasing
            seen = set()
            duplicates = []
            for i, lower_h in enumerate(lowercase_headers):
                if lower_h in seen:
                    # Find the original header that conflicts
                    for j in range(i):
                        if lowercase_headers[j] == lower_h:
                            duplicates.append(
                                f"'{original_headers[j]}' and '{original_headers[i]}'"
                            )
                            break
                seen.add(lower_h)

            if duplicates:
                raise CSVImportError(
                    f"CSV file {csv_path.name} has headers that differ only in case: "
                    f"{', '.join(duplicates)}. This would cause data loss. "
                    f"Please ensure all column names are unique (case-insensitive)."
                )

            # Lowercase all headers for consistency
            headers = lowercase_headers

            # Sanitize all column names
            # (replace spaces/invalid chars with underscores, then validate)
            sanitized_headers = []
            original_to_sanitized = {}  # Track mapping for duplicate detection

            for i, header in enumerate(headers):
                # First, replace spaces and common invalid chars with underscores
                sanitized = header.replace(" ", "_").replace("-", "_").replace(".", "_")
                # Remove any other non-alphanumeric characters except underscores
                sanitized = re.sub(r"[^a-zA-Z0-9_]", "", sanitized)
                # Ensure it starts with letter or underscore
                if sanitized and not sanitized[0].isalpha() and sanitized[0] != "_":
                    sanitized = "_" + sanitized
                # Validate the sanitized identifier
                try:
                    sanitized = sanitize_sql_identifier(sanitized, f"column name (from '{header}')")
                except CSVImportError as e:
                    raise CSVImportError(
                        f"CSV file {csv_path.name} has invalid column header '{header}': {e}"
                    )

                # Track original to sanitized mapping
                original_to_sanitized[header] = (sanitized, i)
                sanitized_headers.append(sanitized)

            # Check for duplicate sanitized column names
            seen_sanitized = {}
            duplicates = []
            for orig_header, (san_header, idx) in original_to_sanitized.items():
                if san_header in seen_sanitized:
                    # Found a duplicate:
                    # get the original header that first used this sanitized name
                    first_orig = seen_sanitized[san_header]
                    duplicates.append(
                        f"'{first_orig}' and '{orig_header}' both sanitize to '{san_header}'"
                    )
                else:
                    seen_sanitized[san_header] = orig_header

            if duplicates:
                raise CSVImportError(
                    f"CSV file {csv_path.name} has column names that become identical "
                    f"after sanitization: {', '.join(duplicates)}. This would cause data loss. "
                    f"Please rename columns to ensure they remain unique after sanitization "
                    f"(spaces, dashes, dots, etc. are converted to underscores)."
                )

            headers = sanitized_headers

            # Read rows with original case, then normalize keys to lowercase
            rows = []
            original_to_sanitized = {
                orig.lower(): san for orig, san in zip(lowercase_headers, headers)
            }
            for row_num, row in enumerate(reader, start=2):  # Start at 2 (after header)
                # Check for extra columns (csv.DictReader stores them under None key)
                if None in row:
                    raise CSVImportError(
                        f"CSV file {csv_path.name} has a malformed row at line {row_num}: "
                        f"Row has more columns than headers. Extra data: {row[None]}"
                    )
                # Check for None keys explicitly (defensive programming)
                if any(k is None for k in row.keys()):
                    raise CSVImportError(
                        f"CSV file {csv_path.name} has a malformed row at line {row_num}: "
                        f"Row contains None as a key (usually caused by extra columns)"
                    )
                rows.append({original_to_sanitized[k.lower()]: v for k, v in row.items()})

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
        values = [
            row[header] for row in sample_rows if row[header] is not None and row[header].strip()
        ]

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

        # Add other columns (headers are already sanitized at this point)
        for header in headers:
            col_type = column_types.get(header, String(255))

            # If column is named 'id', make it primary key
            if header == "id":
                columns.append(Column(header, col_type, primary_key=True))
            else:
                columns.append(Column(header, col_type, nullable=True))

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
    table: Table,
    headers: list[str],
    column_types: dict[str, type],
    rows: list[dict[str, str]],
    engine,
) -> int:
    """Insert rows into the table.

    Args:
        table: SQLAlchemy Table object (provides safe identifier handling)
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
                    # Build row data dictionary with parsed values
                    row_data = {}
                    for header in headers:
                        col_type = column_types.get(header, String(255))
                        parsed_value = parse_value(row[header], col_type)
                        row_data[header] = parsed_value

                    # Use SQLAlchemy's insert() construct for safe identifier handling
                    # This properly quotes table and column names, preventing SQL injection
                    stmt = insert(table).values(**row_data)
                    await conn.execute(stmt)
                    inserted_count += 1

                except Exception as e:
                    error_msg = f"Row {row_num}: Failed to insert - {e}\nData: {row}"
                    logger.error(error_msg)
                    raise CSVImportError(error_msg)

        logger.info(f"Successfully inserted {inserted_count} rows into '{table.name}'")
        return inserted_count

    except CSVImportError:
        raise
    except Exception as e:
        raise CSVImportError(f"Failed to insert rows into '{table.name}': {e}")


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

    # Determine table name from filename and sanitize it
    table_name = csv_path.stem.lower()
    table_name = sanitize_sql_identifier(table_name, "table name")
    logger.info(f"Target table: {table_name}")

    # Read CSV file
    headers, rows = read_csv_file(csv_path)
    logger.info(f"CSV has {len(headers)} columns: {', '.join(headers)}")

    # Infer column types
    column_types = infer_column_types(headers, rows)
    for header, col_type in column_types.items():
        type_name = col_type.__class__.__name__ if hasattr(col_type, "__class__") else str(col_type)
        logger.debug(f"  {header}: {type_name}")

    # Check if table exists and get/create Table object
    # Table object is used for safe SQL identifier handling (prevents SQL injection)
    exists = await table_exists(table_name, engine)

    if not exists:
        logger.info(f"Table '{table_name}' does not exist. Creating...")
        table = await create_table_from_csv(table_name, headers, column_types, engine)
    else:
        logger.info(f"Table '{table_name}' already exists. Validating columns...")
        # For existing tables, reflect the actual table from the database
        # to validate that CSV columns exist in the database
        async with engine.connect() as conn:
            table = await conn.run_sync(
                lambda sync_conn: Table(table_name, Base.metadata, autoload_with=sync_conn)
            )

        # Check which CSV columns exist in the database table
        db_columns = {col.name for col in table.columns}
        csv_columns = set(headers)
        extra_columns = csv_columns - db_columns
        matching_columns = csv_columns & db_columns

        # If there are extra columns, warn and drop them
        if extra_columns:
            logger.warning(
                f"CSV file '{csv_path.name}' contains columns not in table '{table_name}': "
                f"{', '.join(sorted(extra_columns))}. These columns will be dropped."
            )
            # Filter headers to only include columns that exist in the database
            headers = [h for h in headers if h in db_columns]

        # Ensure at least one column matches
        if not matching_columns:
            raise CSVImportError(
                f"CSV file '{csv_path.name}' has no columns matching table '{table_name}'. "
                f"Available columns in table: {', '.join(sorted(db_columns))}. "
                f"CSV columns: {', '.join(sorted(csv_columns))}."
            )

        logger.info(
            f"Using {len(headers)} matching column(s) for table '{table_name}': "
            f"{', '.join(headers)}"
        )

        # IMPORTANT: Override column_types with actual database column types
        # This prevents type mismatches when CSV data differs from database schema
        column_types = {}
        for col in table.columns:
            if col.name in headers:
                # Extract the actual column type from the database
                column_types[col.name] = col.type.__class__
                logger.debug(f"  {col.name}: {col.type} (from database)")

        logger.info("Using database column types for type conversion")

    # Insert rows using Table object for safe identifier handling
    inserted_count = await insert_rows(table, headers, column_types, rows, engine)

    return {
        "file": csv_path.name,
        "table": table_name,
        "rows_inserted": inserted_count,
        "created": not exists,
    }


async def import_all_csv_files(csv_files: list[Path] | None = None, csv_dir: Path | None = None):
    """Import all CSV files from the specified folder.

    Args:
        csv_files: Optional list of specific CSV files to import.
                   If None, imports all CSV files in csv_dir folder.
        csv_dir: Directory to look for CSV files. Defaults to SCRIPT_DIR.
    """
    # Set directory to search for CSV files
    if csv_dir is None:
        csv_dir = SCRIPT_DIR

    # Create database URL pointing to data.db in the csv_dir
    db_path = csv_dir / "data.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"

    logger.info(f"Using database: {db_path}")

    # Create database engine
    engine = create_async_engine(database_url, echo=True)

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

        for csv_path in csv_files:
            try:
                result = await import_csv_file(csv_path, engine)
                results.append(result)
                logger.success(
                    f"✓ {result['file']}: {result['rows_inserted']} rows "
                    f"→ table '{result['table']}' "
                    f"({'created' if result['created'] else 'existing'})"
                )
            except CSVImportError as e:
                errors.append((csv_path.name, str(e)))
                logger.error(f"✗ {csv_path.name}: {e}")

        # Print summary
        logger.info(
            f"[IMPORT SUMMARY] Total files processed: {len(csv_files)}, \
                successfully imported: {len(results)}, \
                failed: {len(errors)}"
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
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> \
            | <level>{level: <8}</level> \
            | <level>{message}</level>",
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
        asyncio.run(import_all_csv_files(csv_files, csv_dir))
    except KeyboardInterrupt:
        logger.warning("\nImport cancelled by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Import failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
