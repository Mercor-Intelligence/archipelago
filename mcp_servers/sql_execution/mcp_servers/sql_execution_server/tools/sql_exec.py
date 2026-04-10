import os
import sqlite3
from typing import Annotated

from loguru import logger
from pydantic import Field
from utils.config import DB_PATH
from utils.decorators import make_async_background


@make_async_background
def execute_sql(
    command: Annotated[
        str,
        Field(
            description=(
                "The SQL statement to execute against the SQLite database.\n"
                "\n"
                "SUPPORTED COMMANDS:\n"
                "- SELECT (with JOINs, subqueries, CTEs, window functions)\n"
                "- INSERT, UPDATE, DELETE\n"
                "- CREATE TABLE, DROP TABLE, ALTER TABLE, CREATE INDEX\n"
                "\n"
                "FORMAT RULES:\n"
                "- One statement per call (do not chain with semicolons)\n"
                "- Multi-line SQL is fully supported\n"
                "- Escape single quotes by doubling: 'O''Brien' for O'Brien\n"
                "\n"
                "SCHEMA DISCOVERY (run these first):\n"
                "- List tables: SELECT name FROM sqlite_master WHERE type='table'\n"
                "- Get columns: PRAGMA table_info(table_name)\n"
                "\n"
                "EXAMPLES:\n"
                "  SELECT * FROM users WHERE age > 30\n"
                "\n"
                "  INSERT INTO users (name, email) VALUES ('Alice', 'a@b.com')\n"
                "\n"
                "  SELECT u.name, COUNT(o.id) as orders\n"
                "  FROM users u\n"
                "  LEFT JOIN orders o ON u.id = o.user_id\n"
                "  GROUP BY u.id\n"
                "\n"
                "  CREATE TABLE products (\n"
                "    id INTEGER PRIMARY KEY,\n"
                "    name TEXT NOT NULL,\n"
                "    price REAL\n"
                "  )"
            )
        ),
    ],
) -> str:
    """Execute a SQL command against a SQLite database.

    SCHEMA DISCOVERY (run these first when exploring an unfamiliar database):
    - List all tables:
        SELECT name FROM sqlite_master WHERE type='table'
        Returns: One row per table with the table name

    - Get columns for a table:
        PRAGMA table_info(users)
        Returns: cid (index), name, type, notnull (1=required), dflt_value, pk (1=primary key)

    - Get foreign keys:
        PRAGMA foreign_key_list(table_name)

    - Get indexes:
        PRAGMA index_list(table_name)

    OUTPUT FORMATS:

    For SELECT queries:
    - Tab-delimited table format
    - Line 1: Column names separated by tabs
    - Line 2: 80 dashes as separator (--------)
    - Lines 3+: Row values separated by tabs
    - NULL values display as the text 'None'
    - Empty result: "Query executed successfully. No rows returned."

    For INSERT/UPDATE/DELETE:
    - "Query executed successfully. N row(s) affected."

    For DDL (CREATE/DROP/ALTER):
    - "Query executed successfully. 0 row(s) affected."

    ERROR MESSAGE PREFIXES:
    - "Database error:" - Database file not found or connectivity issues
    - "SQL execution failed:" - Table/column not found, database locked, operational errors
    - "SQL syntax or schema error:" - Invalid SQL syntax
    - "Constraint violation:" - UNIQUE, NOT NULL, FOREIGN KEY violations
    - "Unexpected error:" - Unknown issues

    AUTOMATIC RETRY:
    - "Database locked" errors are automatically retried with backoff
    - You do not need to implement retry logic for concurrency issues

    TRANSACTION BEHAVIOR:
    - Each call is a separate, auto-committed transaction
    - INSERT/UPDATE/DELETE changes are committed immediately on success
    - Failed queries do not affect previously committed data
    - No multi-statement transaction support (no BEGIN/COMMIT)

    PERFORMANCE TIPS:
    - Always use LIMIT for exploratory queries (e.g., SELECT * FROM table LIMIT 100)
    - Use COUNT(*) to check table size before fetching all rows
    - Large result sets may cause slow responses
    - Prefer aggregations over fetching all rows when possible

    DATA TYPE NOTES:
    - SQLite uses dynamic typing
    - Dates: Store as TEXT 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'
    - Booleans: INTEGER 0 (false) or 1 (true)
    - JSON: Store as TEXT, use json_extract() to query

    NULL VS EMPTY STRING:
    - NULL means 'no value' - use IS NULL or IS NOT NULL to test
    - '' (empty string) is a valid text value - use = '' to test
    - In output, NULL displays as 'None' (the text, without quotes)

    OUTPUT PARSING NOTES:
    - Tab characters within values are NOT escaped
    - Newline characters within values are NOT escaped
    - For values with special characters, use REPLACE() to sanitize
    """
    if not os.path.exists(DB_PATH):
        error_msg = f"Database not found at {DB_PATH}"
        logger.error(error_msg)
        return f"Database error: {error_msg}"

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        # Execute the SQL command
        _ = cursor.execute(command)

        # Check if this is a SELECT query or similar that returns results
        if cursor.description:
            # Fetch all results
            rows = cursor.fetchall()
            column_names = [description[0] for description in cursor.description]

            # If no rows returned
            if not rows:
                return "Query executed successfully. No rows returned."

            # Format results as a readable string
            result_lines = ["\t".join(column_names)]
            result_lines.append("-" * 80)
            for row in rows:
                result_lines.append("\t".join(str(value) for value in row))

            return "\n".join(result_lines)
        else:
            # For INSERT, UPDATE, DELETE, etc.
            conn.commit()
            affected_rows = cursor.rowcount
            return f"Query executed successfully. {affected_rows} row(s) affected."
    except sqlite3.OperationalError as e:
        # Database locked errors should be retried by middleware
        if "locked" in str(e).lower():
            logger.warning(f"Database is locked, will retry: {e}")
            raise
        # Other operational errors should not be retried
        error_msg = f"SQL operational error: {e}"
        logger.error(error_msg)
        return f"SQL execution failed: {e}"
    except sqlite3.IntegrityError as e:
        # Constraint violations should not be retried
        error_msg = f"SQL integrity constraint violation: {e}"
        logger.error(error_msg)
        return f"Constraint violation: {e}"
    except sqlite3.ProgrammingError as e:
        # Syntax/schema errors should not be retried
        error_msg = f"SQL programming error: {e}"
        logger.error(error_msg)
        return f"SQL syntax or schema error: {e}"
    except sqlite3.Error as e:
        # Other database errors should not be retried
        error_msg = f"SQL error: {e}"
        logger.error(error_msg)
        return f"Database error: {e}"
    except Exception as e:
        # Unexpected errors - log and return error message
        error_msg = f"Unexpected error executing SQL: {e}"
        logger.error(error_msg)
        return f"Unexpected error: {e}"
    finally:
        # Ensure connection is closed even if an exception occurs
        conn.close()
