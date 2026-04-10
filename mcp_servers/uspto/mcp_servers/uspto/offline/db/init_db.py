"""Database initialization for USPTO offline mode.

This module provides functionality to create and initialize the SQLite database
for offline USPTO patent data storage.
"""

import sqlite3
from pathlib import Path


def init_database(db_path: str) -> None:
    """Initialize the USPTO offline database.

    Creates the database file and executes the schema.sql to set up all tables,
    indexes, triggers, and views.

    Args:
        db_path: Path to the SQLite database file

    Raises:
        FileNotFoundError: If schema.sql file is not found
        sqlite3.Error: If database initialization fails
    """
    # Ensure parent directory exists
    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    # Get path to schema.sql (same directory as this file)
    schema_path = Path(__file__).parent / "schema.sql"

    if not schema_path.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    # Read schema SQL
    with open(schema_path) as f:
        schema_sql = f.read()

    # Create database and execute schema
    conn = sqlite3.connect(db_path)

    try:
        # Enable foreign keys (must be set per connection)
        conn.execute("PRAGMA foreign_keys = ON")

        # Execute schema (creates all tables, indexes, triggers, views)
        conn.executescript(schema_sql)

        # Commit changes
        conn.commit()

    except sqlite3.Error as e:
        raise RuntimeError(f"Database initialization failed: {e}") from e
    finally:
        conn.close()


def verify_schema(db_path: str) -> bool:
    """Verify that the database schema is complete.

    Args:
        db_path: Path to the SQLite database file

    Returns:
        bool: True if schema is valid, False otherwise
    """
    required_tables = [
        "patents",
        "patents_fts",
        "inventors",
        "assignees",
        "cpc_classifications",
        "patent_citations",
        "examiners",
        "ingestion_log",
    ]

    required_views = ["recent_patents", "patents_by_year", "stats_summary"]

    conn = None
    try:
        conn = sqlite3.connect(db_path)

        # Enable foreign keys for this connection
        conn.execute("PRAGMA foreign_keys = ON")

        # Check tables
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = {row[0] for row in cursor.fetchall()}

        missing_tables = set(required_tables) - tables
        if missing_tables:
            conn.close()
            return False

        # Check views
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")
        views = {row[0] for row in cursor.fetchall()}

        missing_views = set(required_views) - views
        if missing_views:
            conn.close()
            return False

        # Check for required columns in specific tables
        # Check assignees table has role column
        cursor = conn.execute("PRAGMA table_info(assignees)")
        assignee_columns = {row[1] for row in cursor.fetchall()}
        if "role" not in assignee_columns:
            conn.close()
            return False

        # Check foreign keys are enabled
        cursor = conn.execute("PRAGMA foreign_keys")
        fk_enabled = cursor.fetchone()[0]
        if not fk_enabled:
            conn.close()
            return False

        conn.close()
        return True

    except sqlite3.Error:
        if conn is not None:
            conn.close()
        return False


def migrate_schema(db_path: str) -> None:
    """Apply schema migrations to existing database.

    This function checks for missing columns and adds them if needed.
    Safe to run on any database - migrations are idempotent.

    Args:
        db_path: Path to the SQLite database file

    Raises:
        RuntimeError: If core tables are missing or migration fails
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.cursor()

        # First check if core tables exist
        # If tables are missing, database is corrupt and needs full reinit
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='assignees'")
        if not cursor.fetchone():
            conn.close()
            raise RuntimeError(
                f"Database is corrupt or incomplete (missing core tables): {db_path}. "
                "Please delete the database file and reinitialize."
            )

        # Check if assignees table has role column
        cursor.execute("PRAGMA table_info(assignees)")
        columns = {row[1] for row in cursor.fetchall()}

        if "role" not in columns:
            # Add role column with default value
            cursor.execute("ALTER TABLE assignees ADD COLUMN role TEXT")
            conn.commit()

        conn.close()

    except sqlite3.Error as e:
        if conn is not None:
            conn.rollback()
            conn.close()
        raise RuntimeError(f"Schema migration failed: {e}") from e
