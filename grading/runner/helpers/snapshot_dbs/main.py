"""Snapshot DBs helper - extracts and connects to SQLite databases.

Supports both native SQLite .db files and SQL dump files (.sql).
SQL dumps are automatically transpiled to SQLite using sqlglot,
with dialect auto-detection (MySQL/MariaDB, PostgreSQL, generic SQL).
"""

import re
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import IO, Any

import sqlglot
from loguru import logger
from sqlglot.errors import SqlglotError

from runner.helpers.artifact_state.parsers.sql import (
    PostgreSQLCopyParser,
    detect_sql_format,
)
from runner.models import AgentTrajectoryOutput

from .streaming import fold_in_wal

# NOTE: Temp files and DB connections are cleaned up when process exits.
# In Modal, each grading run is a separate process, so cleanup is automatic.
# For long-running processes, connections dict includes temp_path for manual cleanup.


_POSTGRES_COMMENT_ON_RE = re.compile(r"\bCOMMENT\s+ON\b", re.IGNORECASE)
_POSTGRES_DOLLAR_QUOTE_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def _strip_postgres_comment_statements(sql_content: str) -> str:
    """Remove standalone PostgreSQL COMMENT ON statements."""
    if not _POSTGRES_COMMENT_ON_RE.search(sql_content):
        return sql_content

    output: list[str] = []
    last_end = 0
    index = 0
    statement_has_code = False
    length = len(sql_content)

    def skip_span(start: int) -> int:
        char = sql_content[start]
        next_char = sql_content[start + 1] if start + 1 < length else ""
        if char == "-" and next_char == "-":
            newline = sql_content.find("\n", start + 2)
            return length if newline == -1 else newline + 1
        if char == "/" and next_char == "*":
            end = sql_content.find("*/", start + 2)
            return length if end == -1 else end + 2
        if char in "'\"":
            quote = char
            scan_index = start + 1
            while scan_index < length:
                if sql_content[scan_index] == quote:
                    if scan_index + 1 < length and sql_content[scan_index + 1] == quote:
                        scan_index += 2
                        continue
                    return scan_index + 1
                scan_index += 1
            return length
        if char == "$":
            match = _POSTGRES_DOLLAR_QUOTE_RE.match(sql_content, start)
            if match:
                delimiter = match.group(0)
                end = sql_content.find(delimiter, match.end())
                return length if end == -1 else end + len(delimiter)
        return start

    def find_statement_end(start: int) -> int:
        scan_index = start
        while scan_index < length:
            char = sql_content[scan_index]
            span_end = skip_span(scan_index)
            if span_end != scan_index:
                scan_index = span_end
                continue
            if char == ";":
                return scan_index + 1
            scan_index += 1
        return length

    while index < length:
        char = sql_content[index]
        span_end = skip_span(index)
        if span_end != index:
            index = span_end
            continue
        if char.isspace():
            index += 1
            continue
        if char == ";":
            statement_has_code = False
            index += 1
            continue
        if not statement_has_code:
            match = _POSTGRES_COMMENT_ON_RE.match(sql_content, index)
            if match:
                end = find_statement_end(index)
                output.append(sql_content[last_end:index])
                output.append(re.sub(r"[^\r\n]", " ", sql_content[index:end]))
                last_end = end
                index = end
                statement_has_code = False
                continue
        statement_has_code = True
        index += 1

    if not output:
        return sql_content
    output.append(sql_content[last_end:])
    return "".join(output)


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

    sql_content = _strip_postgres_comment_statements(sql_content)

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
    # MySQL session directives have no meaning when loading into SQLite.
    sql_content = re.sub(
        r"^\s*(?:LOCK\s+TABLES\b[^;]*|UNLOCK\s+TABLES)\s*;",
        "",
        sql_content,
        flags=re.MULTILINE | re.IGNORECASE,
    )
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


def _mask_sql_comments_and_literals(
    sql_content: str, mysql_backslash_escapes: bool = False
) -> str:
    """Replace comments and literals with whitespace while preserving DDL."""
    output: list[str] = []
    index = 0
    length = len(sql_content)

    def append_masked(start: int, end: int) -> None:
        output.extend("\n" if char == "\n" else " " for char in sql_content[start:end])

    while index < length:
        char = sql_content[index]
        next_char = sql_content[index + 1] if index + 1 < length else ""

        if char == "-" and next_char == "-":
            end = sql_content.find("\n", index + 2)
            end = length if end == -1 else end
            append_masked(index, end)
            index = end
            continue
        if char == "/" and next_char == "*":
            end = sql_content.find("*/", index + 2)
            end = length if end == -1 else end + 2
            append_masked(index, end)
            index = end
            continue
        if char in "'\"":
            quote = char
            start = index
            index += 1
            while index < length:
                if (
                    mysql_backslash_escapes
                    and sql_content[index] == "\\"
                    and index + 1 < length
                ):
                    index += 2
                    continue
                if sql_content[index] == quote:
                    if index + 1 < length and sql_content[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            append_masked(start, index)
            continue
        if char == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql_content[index:])
            if match:
                delimiter = match.group(0)
                end = sql_content.find(delimiter, index + len(delimiter))
                if end != -1:
                    end += len(delimiter)
                    append_masked(index, end)
                    index = end
                    continue

        output.append(char)
        index += 1

    return "".join(output)


def _mask_postgres_copy_data(sql_content: str) -> str:
    """Mask PostgreSQL COPY payloads before inspecting SQL dialect markers."""
    copy_header = PostgreSQLCopyParser.COPY_HEADER_RE
    masked_lines: list[str] = []
    in_copy_block = False
    for line in sql_content.splitlines(keepends=True):
        if not in_copy_block and copy_header.match(line.strip()):
            in_copy_block = True
            masked_lines.append(line)
        elif in_copy_block:
            if line.rstrip("\r\n") == r"\.":
                in_copy_block = False
                masked_lines.append(line)
            else:
                masked_lines.append(
                    "".join(char if char in "\r\n" else " " for char in line)
                )
        else:
            masked_lines.append(line)
    return "".join(masked_lines)


def _detect_sql_dialect(sql_content: str) -> str:
    """Detect the SQL dialect from DDL rather than row values."""
    sql = _mask_sql_comments_and_literals(_mask_postgres_copy_data(sql_content))
    sql_lower = sql.lower()
    if (
        "`" in sql
        or re.search(r"\bengine\s*=", sql_lower)
        or re.search(r"\bauto_increment\b", sql_lower)
    ):
        return "mysql"
    if (
        re.search(r"\bserial\b", sql_lower)
        or "::" in sql
        or re.search(r"\bowner\s+to\b", sql_lower)
        or "pg_catalog" in sql_lower
    ):
        return "postgres"
    return "sqlite"


def _is_transaction_begin(sql_content: str, index: int) -> bool:
    match = re.match(
        r"\s*(TRANSACTION|WORK|IMMEDIATE|EXCLUSIVE|DEFERRED)\b",
        sql_content[index:],
        re.I,
    )
    return bool(match) or sql_content[index:].lstrip().startswith(";")


def _split_sql_statements(
    sql_content: str, mysql_backslash_escapes: bool = False
) -> list[str]:
    """Split SQL while keeping quoted, commented, and procedural bodies intact."""
    statements: list[str] = []
    start = 0
    index = 0
    length = len(sql_content)
    parentheses = 0
    begin_depth = 0

    while index < length:
        char = sql_content[index]
        next_char = sql_content[index + 1] if index + 1 < length else ""

        if char == "-" and next_char == "-":
            end = sql_content.find("\n", index + 2)
            index = length if end == -1 else end
            continue
        if char == "/" and next_char == "*":
            end = sql_content.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        if char in "'\"`":
            quote = char
            index += 1
            while index < length:
                if (
                    mysql_backslash_escapes
                    and sql_content[index] == "\\"
                    and index + 1 < length
                ):
                    index += 2
                    continue
                if sql_content[index] == quote:
                    if index + 1 < length and sql_content[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if char == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql_content[index:])
            if match:
                delimiter = match.group(0)
                end = sql_content.find(delimiter, index + len(delimiter))
                if end != -1:
                    index = end + len(delimiter)
                    continue

        if char.isalpha() or char == "_":
            end = index + 1
            while end < length and (
                sql_content[end].isalnum() or sql_content[end] == "_"
            ):
                end += 1
            word = sql_content[index:end].upper()
            if word == "BEGIN" and not _is_transaction_begin(sql_content, end):
                begin_depth += 1
            elif word == "END" and begin_depth:
                begin_depth -= 1
            index = end
            continue
        if char == "(":
            parentheses += 1
        elif char == ")" and parentheses:
            parentheses -= 1
        elif char == ";" and parentheses == 0 and begin_depth == 0:
            statements.append(sql_content[start : index + 1])
            start = index + 1
        index += 1

    if sql_content[start:].strip():
        statements.append(sql_content[start:])
    return statements


def _iter_sql_lines(sql_content: str) -> Iterator[str]:
    """Yield lines from SQL content without copying the whole input."""
    position = 0
    content_length = len(sql_content)
    while position < content_length:
        newline = sql_content.find("\n", position)
        if newline == -1:
            yield sql_content[position:]
            return
        yield sql_content[position : newline + 1]
        position = newline + 1


_COPY_BLOCK_INTERRUPTION_RE = re.compile(
    r"^(?:CREATE\s+TABLE|ALTER\s+TABLE|CREATE\s+INDEX)\b", re.IGNORECASE
)


def _copy_block_interrupted(line: str) -> bool:
    """Return whether a line starts the next SQL statement."""
    stripped_line = line.strip()
    return bool(
        PostgreSQLCopyParser.COPY_HEADER_RE.match(stripped_line)
        or _COPY_BLOCK_INTERRUPTION_RE.match(stripped_line)
    )


def _unterminated_copy_block_ordinals(sql_content: str) -> set[int]:
    """Return COPY block ordinals that have no terminator before the next header."""
    if detect_sql_format(sql_content) != "postgresql_copy":
        return set()

    unterminated: set[int] = set()
    current_ordinal: int | None = None
    next_ordinal = 0
    for line in _iter_sql_lines(sql_content):
        stripped_line = line.strip()
        if PostgreSQLCopyParser.COPY_HEADER_RE.match(stripped_line):
            if current_ordinal is not None:
                unterminated.add(current_ordinal)
            current_ordinal = next_ordinal
            next_ordinal += 1
        elif current_ordinal is not None and line.rstrip("\n\r") == r"\.":
            current_ordinal = None
    if current_ordinal is not None:
        unterminated.add(current_ordinal)
    return unterminated


def _strip_postgres_copy_blocks(sql_content: str) -> str:
    """Remove PostgreSQL COPY payloads before passing a dump to sqlglot."""
    if detect_sql_format(sql_content) != "postgresql_copy":
        return sql_content

    unterminated_ordinals = _unterminated_copy_block_ordinals(sql_content)
    stripped_lines: list[str] = []
    lines = _iter_sql_lines(sql_content)
    pending: str | None = None
    copy_ordinal = 0
    while True:
        line = pending if pending is not None else next(lines, None)
        pending = None
        if line is None:
            break
        if not PostgreSQLCopyParser.COPY_HEADER_RE.match(line.strip()):
            stripped_lines.append(line)
            continue

        block_unterminated = copy_ordinal in unterminated_ordinals
        copy_ordinal += 1
        stripped_lines.append("".join(char for char in line if char in "\r\n"))
        while True:
            data_line = next(lines, None)
            if data_line is None:
                break
            if data_line.rstrip("\n\r") == r"\.":
                stripped_lines.append(
                    "".join(char for char in data_line if char in "\r\n")
                )
                break
            if block_unterminated and _copy_block_interrupted(data_line):
                pending = data_line
                break
            stripped_lines.append("".join(char for char in data_line if char in "\r\n"))

    return "".join(stripped_lines)


def _count_nonempty_postgres_copy_blocks(sql_content: str) -> int:
    """Count PostgreSQL COPY blocks that contain at least one data row."""
    if detect_sql_format(sql_content) != "postgresql_copy":
        return 0

    unterminated_ordinals = _unterminated_copy_block_ordinals(sql_content)
    lines = _iter_sql_lines(sql_content)
    pending: str | None = None
    nonempty_blocks = 0
    copy_ordinal = 0
    while True:
        line = pending if pending is not None else next(lines, None)
        pending = None
        if line is None:
            break
        if not PostgreSQLCopyParser.COPY_HEADER_RE.match(line.strip()):
            continue

        block_unterminated = copy_ordinal in unterminated_ordinals
        copy_ordinal += 1
        has_data = False
        while True:
            data_line = next(lines, None)
            if data_line is None:
                break
            if data_line.rstrip("\n\r") == r"\.":
                break
            if block_unterminated and _copy_block_interrupted(data_line):
                pending = data_line
                break
            has_data = True
        if has_data:
            nonempty_blocks += 1

    return nonempty_blocks


def _quote_sqlite_identifier(identifier: str) -> str:
    """Quote an SQLite identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def _insert_copy_batch(
    conn: sqlite3.Connection,
    insert_sql: str,
    rows: list[tuple[Any, ...]],
    table_name: str,
) -> tuple[int, bool]:
    """Insert a COPY batch, retrying individual rows after a batch failure."""
    conn.execute("SAVEPOINT copy_batch")
    try:
        conn.executemany(insert_sql, rows)
        conn.execute("RELEASE copy_batch")
        return len(rows), False
    except sqlite3.Error as error:
        conn.execute("ROLLBACK TO copy_batch")
        conn.execute("RELEASE copy_batch")
        logger.debug(
            f"Falling back to row-by-row COPY inserts for {table_name}: {error}"
        )
        inserted = 0
        failed = False
        for row in rows:
            try:
                conn.execute(insert_sql, row)
            except sqlite3.Error:
                failed = True
            else:
                inserted += 1
        return inserted, failed


def _load_postgres_copy_rows(
    conn: sqlite3.Connection, sql_content: str
) -> tuple[int, list[str]]:
    """Load PostgreSQL COPY rows into tables created by the SQL loader."""
    if detect_sql_format(sql_content) != "postgresql_copy":
        return 0, []

    unterminated_ordinals = _unterminated_copy_block_ordinals(sql_content)
    existing_tables = {
        name.lower(): name
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    failed_blocks = 0
    flattened_tables: list[str] = []

    lines = _iter_sql_lines(sql_content)
    pending: str | None = None
    copy_ordinal = 0
    while True:
        line = pending if pending is not None else next(lines, None)
        pending = None
        if line is None:
            break
        match = PostgreSQLCopyParser.COPY_HEADER_RE.match(line.strip())
        if not match:
            continue

        block_unterminated = copy_ordinal in unterminated_ordinals
        copy_ordinal += 1
        table_name = (match.group(2) or match.group(3)).lower()
        columns = tuple(c.strip().strip('"') for c in match.group(4).split(","))
        sqlite_table = existing_tables.get(table_name)
        skip_block = sqlite_table is None
        block_failed = False
        has_rows = False
        inserted_rows = 0
        batch: list[tuple[Any, ...]] = []

        if sqlite_table is None:
            logger.debug(f"Skipping COPY block for missing table {table_name}")
        else:
            table_info = conn.execute(
                f"PRAGMA table_info({_quote_sqlite_identifier(sqlite_table)})"
            ).fetchall()
            sqlite_columns = {str(row[1]).lower() for row in table_info}
            missing_columns = [
                column for column in columns if column.lower() not in sqlite_columns
            ]
            added_text_column = False
            if missing_columns:
                logger.debug(
                    f"Adding missing COPY columns for {table_name}: "
                    f"{', '.join(missing_columns)}"
                )
                for column in missing_columns:
                    try:
                        conn.execute(
                            f"ALTER TABLE {_quote_sqlite_identifier(sqlite_table)} "
                            f"ADD COLUMN {_quote_sqlite_identifier(column)} TEXT"
                        )
                        added_text_column = True
                    except sqlite3.Error as error:
                        logger.debug(
                            f"Failed to add COPY column {column} to {table_name}: "
                            f"{error}"
                        )
                        skip_block = True
                        block_failed = True
                        break
            if added_text_column and sqlite_table not in flattened_tables:
                flattened_tables.append(sqlite_table)

        insert_sql = ""
        if not skip_block and sqlite_table is not None:
            insert_sql = (
                f"INSERT INTO {_quote_sqlite_identifier(sqlite_table)} ("
                f"{', '.join(_quote_sqlite_identifier(column) for column in columns)}"
                f") VALUES ({', '.join('?' for _ in columns)})"
            )

        while True:
            data_line = next(lines, None)
            if data_line is None:
                break
            if data_line.rstrip("\n\r") == r"\.":
                break
            if block_unterminated and _copy_block_interrupted(data_line):
                pending = data_line
                block_failed = True
                break

            data_line = data_line.rstrip("\n\r")
            has_rows = True
            values = data_line.split("\t")
            if len(values) != len(columns):
                block_failed = True
                logger.debug(
                    f"Skipping malformed row in COPY block for {table_name}: "
                    f"expected {len(columns)} fields, got {len(values)}"
                )
                continue
            if skip_block:
                continue

            batch.append(
                tuple(PostgreSQLCopyParser.unescape(value) for value in values)
            )
            if len(batch) == 1000:
                inserted, batch_failed = _insert_copy_batch(
                    conn, insert_sql, batch, table_name
                )
                inserted_rows += inserted
                block_failed = block_failed or batch_failed
                batch.clear()

        if not skip_block:
            inserted, batch_failed = _insert_copy_batch(
                conn, insert_sql, batch, table_name
            )
            inserted_rows += inserted
            block_failed = block_failed or batch_failed

        if has_rows and (skip_block or block_failed):
            failed_blocks += 1
        logger.debug(f"Loaded {inserted_rows} PostgreSQL COPY row(s) into {table_name}")

    conn.commit()
    return failed_blocks, flattened_tables


def _count_sqlite_rows(conn: sqlite3.Connection) -> int:
    """Count rows across all tables in a loaded SQLite database."""
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    total = 0
    for (name,) in tables:
        escaped_name = name.replace('"', '""')
        total += conn.execute(f'SELECT COUNT(*) FROM "{escaped_name}"').fetchone()[0]
    return total


@dataclass
class SqlDumpLoadResult:
    connection: sqlite3.Connection
    dialect: str
    parsed_statement_count: int
    failed_statement_count: int
    table_count: int
    load_error: str | None = None
    flattened_tables: list[str] = field(default_factory=list)


def _sql_dump_load_error(
    conn: sqlite3.Connection, table_count: int, copy_blocks_with_rows: int
) -> str | None:
    """Describe SQL dumps whose loaded database cannot support grading."""
    if copy_blocks_with_rows and _count_sqlite_rows(conn) == 0:
        return (
            "SQL dump contains COPY-format row data in "
            f"{copy_blocks_with_rows} block(s), but no rows were loaded"
        )
    if table_count == 0:
        return "SQL dump produced no tables"
    return None


def _execute_transpiled_statements(
    conn: sqlite3.Connection,
    transpiled: list[str],
    expressions: list[sqlglot.exp.Expr] | None = None,
    mysql_fallback: bool = False,
) -> tuple[bool, list[str]]:
    """Execute one parsed statement and report whether SQLite rejected any part."""
    statement_failed = False
    flattened_tables: list[str] = []
    for index, stmt in enumerate(transpiled):
        expression = (
            expressions[index] if expressions and index < len(expressions) else None
        )
        if _execute_statement(conn, stmt, expression, mysql_fallback, flattened_tables):
            recovered, flattened_table = (
                _recover_create_table(conn, expression)
                if expression is not None
                else (False, None)
            )
            if recovered:
                if flattened_table is not None:
                    flattened_tables.append(flattened_table)
                continue
            statement_failed = True
    return statement_failed, flattened_tables


def _extend_unique_table_names(target: list[str], names: list[str]) -> None:
    """Append table names without changing their first-seen order."""
    for name in names:
        if name not in target:
            target.append(name)


def _reduced_create_table_sql(
    expression: sqlglot.exp.Expr, text_types: bool = False
) -> str | None:
    """Build a SQLite CREATE TABLE retaining columns and primary keys."""
    if (
        not isinstance(expression, sqlglot.exp.Create)
        or expression.args.get("kind") != "TABLE"
    ):
        return None
    schema = expression.this
    if not isinstance(schema, sqlglot.exp.Schema):
        return None

    reduced_expressions: list[sqlglot.exp.Expr] = []
    for item in schema.expressions:
        if isinstance(item, sqlglot.exp.ColumnDef):
            kind = item.args.get("kind")
            if text_types:
                kind = sqlglot.exp.DataType.build("TEXT")
            if kind is None:
                kind = sqlglot.exp.DataType.build("TEXT")
            # Read-only grading input does not need constraints that cost rows.
            primary_key_constraints = [
                constraint
                for constraint in item.args.get("constraints") or []
                if isinstance(constraint, sqlglot.exp.ColumnConstraint)
                and isinstance(
                    constraint.args.get("kind"),
                    sqlglot.exp.PrimaryKeyColumnConstraint,
                )
            ]
            reduced_expressions.append(
                sqlglot.exp.ColumnDef(
                    this=item.this.copy(),
                    kind=kind.copy(),
                    constraints=[
                        constraint.copy() for constraint in primary_key_constraints
                    ],
                )
            )
        elif isinstance(item, sqlglot.exp.PrimaryKey):
            reduced_expressions.append(item.copy())
        elif isinstance(item, sqlglot.exp.Constraint):
            primary_key = next(
                (
                    constraint
                    for constraint in item.expressions
                    if isinstance(constraint, sqlglot.exp.PrimaryKey)
                ),
                None,
            )
            if primary_key is not None:
                reduced_expressions.append(
                    sqlglot.exp.Constraint(
                        this=item.this.copy() if item.this is not None else None,
                        expressions=[primary_key.copy()],
                    )
                )

    reduced = expression.copy()
    reduced.set(
        "this",
        sqlglot.exp.Schema(
            this=schema.this.copy(),
            expressions=reduced_expressions,
        ),
    )
    reduced.set("properties", None)
    reduced.set("indexes", [])
    return reduced.sql(dialect="sqlite", identify=True)


def _recover_create_table(
    conn: sqlite3.Connection, expression: sqlglot.exp.Expr
) -> tuple[bool, str | None]:
    """Retry a rejected CREATE TABLE with progressively simpler DDL."""
    if not isinstance(expression, sqlglot.exp.Create) or not isinstance(
        expression.this, sqlglot.exp.Schema
    ):
        return False, None
    table = expression.this.this
    table_name = table.name if isinstance(table, sqlglot.exp.Table) else None
    for text_types in (False, True):
        reduced_sql = _reduced_create_table_sql(expression, text_types=text_types)
        if reduced_sql is None:
            return False, None
        try:
            conn.execute(reduced_sql)
        except sqlite3.Error as error:
            logger.debug(f"Reduced CREATE TABLE failed: {error}")
        else:
            logger.debug("Loaded CREATE TABLE with reduced constraints")
            return True, table_name if text_types else None
    return False, None


def _is_non_data_statement(expression: sqlglot.exp.Expr) -> bool:
    """Return whether a failed statement cannot remove dumped row data."""
    if isinstance(expression, (sqlglot.exp.Create, sqlglot.exp.Drop)):
        return expression.args.get("kind") in {"INDEX", "SEQUENCE"}
    if isinstance(expression, sqlglot.exp.Alter):
        if expression.args.get("kind") == "SEQUENCE":
            return True
        actions = expression.args.get("actions") or []
        return bool(actions) and all(
            isinstance(action, sqlglot.exp.AddConstraint) for action in actions
        )
    if isinstance(expression, sqlglot.exp.Command):
        command = str(expression.args.get("this", "")).strip().upper()
        command_expression = expression.args.get("expression")
        if isinstance(command_expression, sqlglot.exp.Literal):
            command_expression = command_expression.this
        command_expression = str(command_expression or "").strip().upper()
        return command == "DO" and command_expression.startswith("SETVAL(")
    return False


def _strip_mysql_ddl_clauses(
    expression: sqlglot.exp.Expr,
) -> sqlglot.exp.Expr:
    """Remove MySQL-only schema clauses that SQLite cannot execute."""
    if (
        not isinstance(expression, sqlglot.exp.Create)
        or expression.args.get("kind") != "TABLE"
    ):
        return expression

    schema = expression.this
    if not isinstance(schema, sqlglot.exp.Schema):
        return expression

    schema.set(
        "expressions",
        [
            item
            for item in schema.expressions
            if not isinstance(
                item,
                (
                    sqlglot.exp.IndexColumnConstraint,
                    sqlglot.exp.UniqueColumnConstraint,
                ),
            )
        ],
    )
    for column in schema.find_all(sqlglot.exp.ColumnDef):
        column.set(
            "constraints",
            [
                constraint
                for constraint in column.args.get("constraints", [])
                if not isinstance(
                    constraint.args.get("kind"),
                    (
                        sqlglot.exp.CharacterSetColumnConstraint,
                        sqlglot.exp.CollateColumnConstraint,
                    ),
                )
            ],
        )
    return expression


def _reduce_mysql_create_table(
    expression: sqlglot.exp.Expr, text_types: bool = False
) -> sqlglot.exp.Expr:
    """Reduce a rejected MySQL CREATE TABLE to columns and primary keys."""
    reduced = expression.copy()
    schema = reduced.this
    if not isinstance(schema, sqlglot.exp.Schema):
        return reduced

    reduced_expressions: list[sqlglot.exp.Expr] = []
    for item in schema.expressions:
        if isinstance(item, sqlglot.exp.ColumnDef):
            column = item.copy()
            # A generated column becomes an ordinary nullable column here.
            # Explicit-column INSERTs can omit it, leaving it NULL.
            column.set(
                "constraints",
                [
                    constraint
                    for constraint in column.args.get("constraints", [])
                    if isinstance(
                        constraint.args.get("kind"),
                        sqlglot.exp.PrimaryKeyColumnConstraint,
                    )
                ],
            )
            if text_types:
                column.set(
                    "kind",
                    sqlglot.exp.DataType.build("TEXT", dialect="sqlite"),
                )
            reduced_expressions.append(column)
        elif isinstance(item, sqlglot.exp.PrimaryKey):
            reduced_expressions.append(item.copy())
    schema.set("expressions", reduced_expressions)
    return reduced


def _mysql_create_table_fallbacks(
    expression: sqlglot.exp.Expr,
) -> list[str]:
    """Render progressively simpler MySQL CREATE TABLE fallbacks."""
    if (
        not isinstance(expression, sqlglot.exp.Create)
        or expression.args.get("kind") != "TABLE"
    ):
        return []

    return [
        _reduce_mysql_create_table(expression).sql(dialect="sqlite"),
        _reduce_mysql_create_table(expression, text_types=True).sql(dialect="sqlite"),
    ]


def _execute_statement(
    conn: sqlite3.Connection,
    stmt: str,
    expression: sqlglot.exp.Expr | None = None,
    mysql_fallback: bool = False,
    flattened_tables: list[str] | None = None,
) -> bool:
    """Execute a statement, retrying rejected MySQL CREATE TABLE statements."""
    # SQLite requires PRIMARY KEY before AUTOINCREMENT, but sqlglot outputs
    # AUTOINCREMENT first. Since we're loading existing data (not generating
    # new IDs), just remove AUTOINCREMENT.
    stmt = re.sub(r"\bAUTOINCREMENT\b\s*", "", stmt, flags=re.IGNORECASE)
    try:
        conn.execute(stmt)
        return False
    except sqlite3.Error as e:
        if mysql_fallback and expression is not None:
            for fallback_index, fallback in enumerate(
                _mysql_create_table_fallbacks(expression)
            ):
                fallback = re.sub(
                    r"\bAUTOINCREMENT\b\s*", "", fallback, flags=re.IGNORECASE
                )
                try:
                    conn.execute(fallback)
                    logger.debug(
                        "Loaded MySQL CREATE TABLE after reducing unsupported clauses"
                    )
                    if fallback_index == 1 and flattened_tables is not None:
                        table = expression.this.this
                        if isinstance(table, sqlglot.exp.Table):
                            flattened_tables.append(table.name)
                    return False
                except sqlite3.Error:
                    continue
        if expression is not None and _is_non_data_statement(expression):
            logger.debug(f"Skipping unsupported schema-only SQL statement: {e}")
            return False
        logger.debug(f"Skipping unsupported SQL statement: {e}")
        return True


def _load_sql_dump_to_sqlite(sql_content: str) -> SqlDumpLoadResult:
    """Parse SQL dump and load into in-memory SQLite.

    Auto-detects the source SQL dialect (MySQL/MariaDB, PostgreSQL, or generic)
    and transpiles to SQLite using sqlglot.

    Args:
        sql_content: Raw SQL dump content (CREATE TABLE + INSERT statements)

    Returns:
        Connection and statement/table load statistics.
    """
    dialect = _detect_sql_dialect(sql_content)
    copy_blocks_with_rows = _count_nonempty_postgres_copy_blocks(sql_content)
    conn = sqlite3.connect(":memory:")
    parsed_statement_count = 0
    failed_statement_count = 0
    flattened_tables: list[str] = []
    dialects = [
        dialect,
        *[
            candidate
            for candidate in ("mysql", "postgres", "sqlite")
            if candidate != dialect
        ],
    ]

    sql_for_parsing = _strip_postgres_copy_blocks(sql_content)
    preprocessed_sql = _preprocess_sql_for_sqlglot(sql_for_parsing)
    try:
        expressions = sqlglot.parse(preprocessed_sql, dialect=dialect)
    except (SqlglotError, RecursionError) as e:
        logger.debug(f"Failed to parse SQL dump as {dialect}: {e}")
    else:
        for expression in expressions:
            if expression is None or isinstance(expression, _TRANSACTION_TYPES):
                continue
            if dialect == "mysql":
                expression = _strip_mysql_ddl_clauses(expression)
            parsed_statement_count += 1
            statement_failed, recovered_tables = _execute_transpiled_statements(
                conn,
                [expression.sql(dialect="sqlite")],
                [expression],
                dialect == "mysql",
            )
            if statement_failed:
                failed_statement_count += 1
            _extend_unique_table_names(flattened_tables, recovered_tables)
        conn.commit()
        table_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        if table_count:
            copy_failures, copy_flattened_tables = _load_postgres_copy_rows(
                conn, sql_content
            )
            failed_statement_count += copy_failures
            _extend_unique_table_names(flattened_tables, copy_flattened_tables)
            return SqlDumpLoadResult(
                connection=conn,
                dialect=dialect,
                parsed_statement_count=parsed_statement_count,
                failed_statement_count=failed_statement_count,
                table_count=table_count,
                load_error=_sql_dump_load_error(
                    conn, table_count, copy_blocks_with_rows
                ),
                flattened_tables=flattened_tables,
            )
        conn.close()
        conn = sqlite3.connect(":memory:")
        parsed_statement_count = 0
        failed_statement_count = 0
        flattened_tables = []

    for statement in _split_sql_statements(sql_for_parsing, dialect == "mysql"):
        statement = _preprocess_sql_for_sqlglot(statement)
        if not statement.strip():
            continue
        if not _mask_sql_comments_and_literals(statement, dialect == "mysql").strip():
            continue

        transpiled: list[str] | None = None
        parsed_expressions: list[sqlglot.exp.Expr] = []
        parsed_candidate: str | None = None
        for candidate in dialects:
            try:
                parsed = sqlglot.parse(statement, dialect=candidate)
                parsed_expressions = [
                    expr
                    for expr in parsed
                    if expr is not None and not isinstance(expr, _TRANSACTION_TYPES)
                ]
                transpiled = [
                    (
                        _strip_mysql_ddl_clauses(expr) if candidate == "mysql" else expr
                    ).sql(dialect="sqlite")
                    for expr in parsed_expressions
                ]
                parsed_candidate = candidate
                break
            except (SqlglotError, RecursionError) as e:
                logger.debug(f"Failed to parse SQL statement as {candidate}: {e}")
        if transpiled is None:
            failed_statement_count += 1
            continue

        parsed_statement_count += 1
        statement_failed, recovered_tables = _execute_transpiled_statements(
            conn,
            transpiled,
            parsed_expressions,
            parsed_candidate == "mysql",
        )
        if statement_failed:
            failed_statement_count += 1
        _extend_unique_table_names(flattened_tables, recovered_tables)

    conn.commit()
    copy_failures, copy_flattened_tables = _load_postgres_copy_rows(conn, sql_content)
    failed_statement_count += copy_failures
    _extend_unique_table_names(flattened_tables, copy_flattened_tables)
    table_count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]
    return SqlDumpLoadResult(
        connection=conn,
        dialect=dialect,
        parsed_statement_count=parsed_statement_count,
        failed_statement_count=failed_statement_count,
        table_count=table_count,
        load_error=_sql_dump_load_error(conn, table_count, copy_blocks_with_rows),
        flattened_tables=flattened_tables,
    )


async def snapshot_dbs_helper(
    initial_snapshot_bytes: IO[bytes],
    final_snapshot_bytes: IO[bytes],
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

            # Write to temp file (SQLite needs file path). Streamed rather than
            # read() into memory: these are app databases, and the one that
            # prompted the WAL fix is 10.4 GB, which no grading container should
            # be asked to hold in RAM to copy it to disk.
            temp_file = tempfile.NamedTemporaryFile(
                suffix=".db", delete=False, mode="wb"
            )
            with final_zip.open(db_file) as src:
                shutil.copyfileobj(src, temp_file)
            temp_file.flush()
            temp_file.close()

            # Carry the write-ahead log across before anything reads the copy.
            # This helper feeds the agentic verifier's database tools, so
            # without it that verifier inspects a database missing every
            # transaction still sitting in the sidecar.
            fold_in_wal(final_zip, db_file, temp_file.name)

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

            load_result = _load_sql_dump_to_sqlite(sql_content)
            connections[alias] = {
                "connection": load_result.connection,
                "path": sql_file,
                "temp_path": None,  # In-memory, no temp file
                "load_error": load_result.load_error,
                "failed_statement_count": load_result.failed_statement_count,
                "flattened_tables": load_result.flattened_tables,
                "table_count": load_result.table_count,
            }

            if load_result.load_error:
                logger.warning(
                    f"{load_result.load_error} (detected dialect: {load_result.dialect})"
                )
            if load_result.failed_statement_count:
                logger.warning(
                    f"SQL dump {sql_file} had "
                    f"{load_result.parsed_statement_count} parsed and "
                    f"{load_result.failed_statement_count} failed statements "
                    f"(detected dialect: {load_result.dialect})"
                )
            if load_result.flattened_tables:
                logger.warning(
                    f"SQL dump {sql_file} loaded all-text tables: "
                    f"{', '.join(load_result.flattened_tables)}"
                )
            logger.info(f"Loaded SQL dump {sql_file} as '{alias}'")

    # Reset BytesIO position after use for potential reuse
    final_snapshot_bytes.seek(0)

    return connections
