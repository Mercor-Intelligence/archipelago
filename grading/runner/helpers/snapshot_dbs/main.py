"""Snapshot DBs helper - extracts and connects to SQLite databases.

Supports both native SQLite .db files and SQL dump files (.sql).
SQL dumps are automatically transpiled to SQLite using sqlglot,
with dialect auto-detection (MySQL/MariaDB, PostgreSQL, generic SQL).
"""

import io
import re
import sqlite3
import tempfile
import zipfile
from typing import Any

import sqlglot
from loguru import logger
from sqlglot.errors import ParseError, TokenError

from runner.models import AgentTrajectoryOutput

# NOTE: Temp files and DB connections are cleaned up when process exits.
# In Modal, each grading run is a separate process, so cleanup is automatic.
# For long-running processes, connections dict includes temp_path for manual cleanup.


def _preprocess_sql_for_sqlglot(sql_content: str) -> str:
    """Remove statements that sqlglot can't parse.

    Strips PRAGMA statements which are SQLite-specific and not understood by sqlglot.
    Transaction control (BEGIN/COMMIT/ROLLBACK) is filtered at the AST level after
    parsing to avoid corrupting trigger body definitions that use BEGIN/END.
    """
    # Remove PRAGMA statements (SQLite-specific, not understood by sqlglot)
    sql_content = re.sub(
        r"^\s*PRAGMA\s+[^;]+;", "", sql_content, flags=re.MULTILINE | re.IGNORECASE
    )

    # Strip PostgreSQL schema prefixes (e.g. "public.") so tables resolve in SQLite
    sql_content = re.sub(r"\bpublic\.", "", sql_content)

    # Remove PostgreSQL-specific commands that sqlglot can't parse
    # psql meta-commands (\connect, \., etc.)
    sql_content = re.sub(r"^\\[^\n]*$", "", sql_content, flags=re.MULTILINE)
    # SET statements, SELECT pg_catalog.*, ALTER ... OWNER TO, ALTER SEQUENCE ... OWNED BY
    _pg_noise = (
        r"^\s*("
        r"SET\s"
        r"|SELECT\s+pg_catalog\."
        r"|ALTER\s+\w+\s+\S+\s+OWNER\s+TO"
        r"|ALTER\s+SEQUENCE\s+\S+\s+OWNED\s+BY"
        r")[^;]*;"
    )
    sql_content = re.sub(_pg_noise, "", sql_content, flags=re.MULTILINE | re.IGNORECASE)
    # CREATE SEQUENCE statements (not supported in SQLite)
    sql_content = re.sub(
        r"^\s*CREATE\s+SEQUENCE\s[^;]*;",
        "",
        sql_content,
        flags=re.MULTILINE | re.IGNORECASE,
    )

    # Strip DEFAULT nextval(...) from CREATE TABLE column definitions
    sql_content = re.sub(
        r"\s+DEFAULT\s+nextval\s*\([^)]*\)(::\w+)?",
        "",
        sql_content,
        flags=re.IGNORECASE,
    )

    # Remove standalone ALTER TABLE ... SET DEFAULT nextval() statements
    # (pg_dump emits these separately from CREATE TABLE, e.g.:
    #  ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('users_id_seq'::regclass);)
    sql_content = re.sub(
        r"^\s*ALTER\s+TABLE\s+.*?\s+SET\s+DEFAULT\s+nextval\s*\([^)]*\)(::\w+)?\s*;",
        "",
        sql_content,
        flags=re.MULTILINE | re.IGNORECASE,
    )

    return sql_content


# Statement types to filter out (transaction control, not needed for fresh DB)
# These are filtered at AST level to safely handle trigger body BEGIN/END
_TRANSACTION_TYPES = (
    sqlglot.exp.Transaction,  # BEGIN [TRANSACTION]
    sqlglot.exp.Commit,  # COMMIT [TRANSACTION]
    sqlglot.exp.Rollback,  # ROLLBACK [TRANSACTION]
)


def _detect_sql_dialect(sql_content: str) -> str:
    """Detect the SQL dialect from raw dump content.

    Must be called on the raw content BEFORE preprocessing, since
    preprocessing may strip dialect markers (e.g., ::regclass in nextval()).

    MySQL/MariaDB: backticks, ENGINE=, AUTO_INCREMENT
    PostgreSQL: SERIAL (as type), :: casts
    """
    sql_lower = sql_content.lower()
    if "`" in sql_content or "engine=" in sql_lower or "auto_increment" in sql_lower:
        return "mysql"
    if re.search(r"\bserial\b", sql_lower) or "::" in sql_content:
        return "postgres"
    return "sqlite"


def _load_sql_dump_to_sqlite(sql_content: str) -> sqlite3.Connection:
    """Parse SQL dump and load into in-memory SQLite.

    Auto-detects the source SQL dialect (MySQL/MariaDB, PostgreSQL, or generic)
    and transpiles to SQLite using sqlglot.

    Args:
        sql_content: Raw SQL dump content (CREATE TABLE + INSERT statements)

    Returns:
        In-memory SQLite connection with loaded data
    """
    # Detect dialect from raw content BEFORE preprocessing strips markers
    dialect = _detect_sql_dialect(sql_content)

    # Preprocess to remove statements sqlglot can't handle
    sql_content = _preprocess_sql_for_sqlglot(sql_content)

    # Parse SQL into AST, filter transaction statements, then transpile to SQLite
    try:
        expressions = sqlglot.parse(sql_content, dialect=dialect)
    except (ParseError, TokenError) as e:
        logger.warning(f"Failed to parse SQL dump as {dialect}: {e}")
        return sqlite3.connect(":memory:")

    # Filter out transaction control at AST level (safe for trigger bodies)
    # and transpile each expression to SQLite
    statements = [
        expr.sql(dialect="sqlite")
        for expr in expressions
        if expr is not None and not isinstance(expr, _TRANSACTION_TYPES)
    ]

    conn = sqlite3.connect(":memory:")

    for stmt in statements:
        # Fix: SQLite requires PRIMARY KEY before AUTOINCREMENT,
        # but sqlglot outputs AUTOINCREMENT first. Since we're loading
        # existing data (not generating new IDs), just remove AUTOINCREMENT.
        # Use \b on both sides to avoid matching inside string literals.
        stmt = re.sub(r"\bAUTOINCREMENT\b\s*", "", stmt, flags=re.IGNORECASE)

        try:
            conn.execute(stmt)
        except sqlite3.Error as e:
            # Log but continue - some statements may not be supported
            # (triggers, stored procedures, etc.)
            logger.debug(f"Skipping unsupported SQL statement: {e}")

    conn.commit()

    # Warn if no tables were created (transpilation succeeded but all statements failed)
    cursor = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
    if cursor.fetchone()[0] == 0:
        logger.warning(f"SQL dump produced no tables (detected dialect: {dialect})")

    return conn


async def snapshot_dbs_helper(
    initial_snapshot_bytes: io.BytesIO,
    final_snapshot_bytes: io.BytesIO,
    trajectory: AgentTrajectoryOutput,
) -> dict[str, Any]:
    """
    Extract databases from final snapshot.

    Supports both native SQLite .db files and SQL dump files (.sql).
    SQL dumps are automatically transpiled to SQLite with dialect auto-detection.

    Returns dict of {alias: connection_info} for each database found.

    Alias generation:
        - "data/sales.db" → "data_sales"
        - ".apps_data/erpnext/database_dump.sql" → "apps_data_erpnext_database_dump"

    Note: DB connections and temp files are left open for the duration
    of the process. In Modal, each grading run is a separate process that
    exits after completion, automatically cleaning up resources.
    """
    connections = {}

    # Reset BytesIO position for reading
    final_snapshot_bytes.seek(0)

    with zipfile.ZipFile(final_snapshot_bytes, "r") as final_zip:
        # Find all .db files
        db_files = [f for f in final_zip.namelist() if f.endswith(".db")]

        for db_file in db_files:
            # Generate alias first (cheap) to check collision before resource allocation
            # e.g., "data/sales.db" → "data_sales"
            # e.g., ".apps_data/erpnext/test.db" → ".apps_data_erpnext_test"
            # Note: preserves leading dots for backwards compatibility with existing
            # verifier configs that reference these aliases
            alias = db_file.removesuffix(".db").replace("/", "_").replace("\\", "_")

            # Skip collision before allocating resources (first .db wins)
            if alias in connections:
                logger.warning(
                    f"Database alias collision: '{alias}' already exists. "
                    f"Skipping {db_file}, keeping {connections[alias]['path']}"
                )
                continue

            # Extract database bytes
            db_bytes = final_zip.read(db_file)

            # Write to temp file (SQLite needs file path)
            temp_file = tempfile.NamedTemporaryFile(
                suffix=".db", delete=False, mode="wb"
            )
            temp_file.write(db_bytes)
            temp_file.flush()
            temp_file.close()

            # Create SQLite connection
            # Note: Connection and temp file will be cleaned up when process exits
            conn = sqlite3.connect(temp_file.name)

            connections[alias] = {
                "connection": conn,
                "path": db_file,
                "temp_path": temp_file.name,
            }

        # Find all .sql files and load each one
        sql_files = [f for f in final_zip.namelist() if f.endswith(".sql")]

        for sql_file in sql_files:
            # Generate alias from path first (cheap operation)
            # e.g., ".apps_data/erpnext/database_dump.sql" → "apps_data_erpnext_database_dump"
            alias = (
                sql_file.removesuffix(".sql")
                .replace("/", "_")
                .replace("\\", "_")
                .lstrip("._")
            )

            # Skip files that produce empty alias (e.g., "_.sql", "._.sql")
            if not alias:
                logger.warning(f"Skipping SQL file with empty alias: {sql_file}")
                continue

            # Check for collision before expensive SQL loading
            if alias in connections:
                # Earlier files (including .db files) take precedence
                logger.warning(
                    f"Skipping SQL dump '{sql_file}': alias '{alias}' already "
                    f"exists from '{connections[alias]['path']}'"
                )
                continue

            try:
                sql_content = final_zip.read(sql_file).decode("utf-8", errors="replace")
            except (KeyError, OSError) as e:
                logger.warning(f"Failed to read SQL file {sql_file}: {e}")
                continue

            # Skip empty files
            if not sql_content.strip():
                continue

            conn = _load_sql_dump_to_sqlite(sql_content)

            connections[alias] = {
                "connection": conn,
                "path": sql_file,
                "temp_path": None,  # In-memory, no temp file
            }

            logger.info(f"Loaded SQL dump {sql_file} as '{alias}'")

    # Reset BytesIO position after use for potential reuse
    final_snapshot_bytes.seek(0)

    return connections
