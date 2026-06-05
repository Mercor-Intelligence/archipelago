"""Streaming SQLite loader for SQL dumps and CSV files.

Streams a SQL dump directly from a snapshot zip into an on-disk SQLite file
using the chunked parser from ``parsers.sql``.  Never loads the full dump
into memory.  Also supports loading CSV files (one table per file, table
name derived from the filename stem).

Schema is inferred from the first row per table (all columns use SQLite's
TEXT affinity; dynamic typing handles numeric comparisons transparently).
No sqlglot transpilation is needed — INSERT data is dialect-agnostic for
basic value types, and CREATE TABLE is auto-generated from column names.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import zipfile
from pathlib import PurePosixPath
from typing import Any

from loguru import logger

from runner.helpers.artifact_state.parsers.sql import (
    SQLInsertParser,
    iter_sql_dump_from_stream,
)
from runner.helpers.db_diff.main import _find_sql_dump_path_in_snapshot

_BATCH_SIZE = 100_000
_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB

# Regex to find INSERT INTO headers and extract the table name.
# Matches: INSERT INTO `table`, INSERT INTO "table", INSERT INTO table
_INSERT_TABLE_RE = re.compile(
    r"INSERT\s+INTO\s+"
    r"(?:(?:\[([^\]]+)\]|`([^`]+)`|'([^']+)'|\"([^\"]+)\"|(\w+))\.)?"  # optional schema
    r"(?:\[([^\]]+)\]|`([^`]+)`|'([^']+)'|\"([^\"]+)\"|(\w+))",
    re.IGNORECASE,
)


# Authoritative table manifest emitted by codegen as a comment header, e.g.
#   # DB_TABLES: invoices, customers, line_items
# Preferred over the regex heuristic because it is not fooled by dynamic SQL
# or aliases. Matches the directive anywhere on its own line, case-insensitive.
# Horizontal whitespace only ([ \t]) around the directive — never \s, which
# would span newlines and let an empty `# DB_TABLES:` swallow the next line.
# ``(.*)`` is the rest of that line (newline excluded), possibly empty.
_DB_TABLES_HEADER_RE = re.compile(
    r"^[ \t]*#[ \t]*DB_TABLES[ \t]*:[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE
)


def extract_declared_tables_from_code(code: str) -> set[str]:
    """Parse the authoritative ``# DB_TABLES:`` manifest header from code.

    Codegen is instructed to emit every table the verifier reads as a
    comma-separated comment header. Returns lowercased names, or an empty
    set when no header is present (callers fall back to the regex heuristic).
    """
    tables: set[str] = set()
    for m in _DB_TABLES_HEADER_RE.finditer(code):
        for token in m.group(1).split(","):
            name = token.strip().strip("`'\"").lower()
            if name:
                tables.add(name)
    return tables


def load_ddl_to_sqlite(ddl: str, db_path: str) -> list[str]:
    """Materialise an empty, schema-correct SQLite DB from DDL text.

    Used to validate db_code_verifier code when there is no golden DB
    snapshot: the verifier runs its queries against correctly-named (but
    empty) tables, surfacing no-such-table / no-such-column errors that
    would otherwise only appear in production grading.

    Executes statements one at a time and skips any that error (PRAGMAs,
    index/trigger DDL, non-SQLite-dialect constructs), so a clean SQLite
    schema loads fully while a mixed-dialect one loads what it can. Returns
    the names of the tables created.
    """
    import sqlite3

    # Strip `--` line comments — including the `-- Service:` / `-- Database
    # file:` headers that schema resolution prepends — before splitting on
    # `;` into individual statements.
    no_comments = re.sub(r"--[^\n]*", "", ddl)
    conn = sqlite3.connect(db_path)
    try:
        for stmt in no_comments.split(";"):
            s = stmt.strip()
            if not s:
                continue
            try:
                conn.execute(s)
            except sqlite3.Error:
                continue
        conn.commit()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def resolve_table_filter(code: str) -> set[str]:
    """Resolve the set of tables to load for a verifier.

    Unions the authoritative ``# DB_TABLES:`` manifest emitted by codegen with
    the regex heuristic. Either source alone can under-count — an incomplete
    manifest, or regex missing an unusual reference — and an under-count means
    a referenced table never loads and the verifier crashes with
    ``no such table``. The union can only over-count, which merely loads a few
    extra (or non-existent, harmlessly ignored) tables. An empty result is
    treated by callers as "no filter" → load every table.
    """
    return extract_declared_tables_from_code(code) | _extract_table_names_from_code(
        code
    )


def _extract_table_names_from_code(code: str) -> set[str]:
    """Heuristically extract SQL table names referenced in verifier code.

    Scans for common SQL keywords followed by a table name token.
    Returns lowercased names.  This is best-effort — it may include false
    positives (SQL keywords that look like table names) but should never
    miss a real table name in well-formed SQL embedded in Python strings.
    """
    tables: set[str] = set()
    for pattern in (
        r"FROM\s+[`'\"]?(\w+)",
        r"JOIN\s+[`'\"]?(\w+)",
        r"INTO\s+[`'\"]?(\w+)",
        r"UPDATE\s+[`'\"]?(\w+)",
        r"TABLE\s+[`'\"]?(\w+)",
        # ctx API methods that take a table name as a string argument
        r"table_row_count\(\s*[\"'](\w+)[\"']",
        r"table_columns\(\s*[\"'](\w+)[\"']",
        r"query_db\(\s*[\"'].*?\bFROM\s+[`\"']?(\w+)",
    ):
        for m in re.finditer(pattern, code, re.IGNORECASE):
            tables.add(m.group(1).lower())
    # Remove SQL keywords that the regex might capture
    tables -= {
        "select",
        "where",
        "values",
        "set",
        "from",
        "join",
        "into",
        "update",
        "table",
        "if",
        "exists",
        "not",
        "null",
        "true",
        "false",
        "as",
        "on",
        "and",
        "or",
        "order",
        "group",
        "by",
        "having",
        "limit",
        "offset",
        "union",
        "all",
        "distinct",
        "case",
        "when",
        "then",
        "else",
        "end",
        "like",
        "in",
        "between",
        "is",
    }
    return tables


def _extract_table_from_match(m: re.Match[str]) -> str:
    """Extract the lowercased table name from an _INSERT_TABLE_RE match."""
    # Groups 6-10 are the table name in different quoting styles
    raw = m.group(6) or m.group(7) or m.group(8) or m.group(9) or m.group(10)
    return (raw or "").lower()


def _iter_filtered_rows_fast(
    stream: io.TextIOWrapper,
    table_filter: set[str],
) -> Any:
    """C-speed filtered streaming: skip non-matching tables at regex speed.

    Uses ``re.search`` and ``str.find`` (C-implemented) to scan through
    non-matching INSERT statements at ~100 MB/s instead of the Python
    character-level parser's ~3 MB/s.  Only enters the slow Python parser
    for INSERT statements belonging to tables in *table_filter*.

    For mysqldump output, ``;\n`` reliably marks statement boundaries
    because newlines inside string values are escaped as ``\\n``.
    """
    parser = SQLInsertParser()
    buf = ""
    pos = 0
    eof = False

    def _refill() -> bool:
        nonlocal buf, eof
        if eof:
            return False
        chunk = stream.read(_CHUNK_SIZE)
        if not chunk:
            eof = True
            return False
        buf += chunk
        return True

    def _trim(up_to: int) -> None:
        nonlocal buf, pos
        if up_to > 0:
            buf = buf[up_to:]
            pos -= up_to

    _refill()

    while True:
        # ---- Find next INSERT header at C-speed ----
        m = _INSERT_TABLE_RE.search(buf, pos)
        if m is None:
            if not _refill():
                break
            # Keep a small overlap for headers split across chunks
            if len(buf) > _CHUNK_SIZE + 4096:
                _trim(len(buf) - 4096)
            continue

        table_name = _extract_table_from_match(m)

        if table_name not in table_filter:
            # ---- SKIP: C-speed scan for ;\n to jump past this statement ----
            skip_from = m.end()
            while True:
                semi = buf.find(";\n", skip_from)
                if semi >= 0:
                    pos = semi + 2
                    break
                # Not found — need more data. Keep the tail for boundary.
                _trim(max(0, len(buf) - 2))
                skip_from = 0
                if not _refill():
                    pos = len(buf)
                    break
            # Trim past the skipped statement
            _trim(pos)
            continue

        # ---- MATCH: extract the full INSERT statement and parse it ----
        # Find the statement end (;\n) to bound the text we feed to the parser.
        stmt_start = m.start()
        search_from = m.end()
        while True:
            semi = buf.find(";\n", search_from)
            if semi >= 0:
                # Include the semicolon in the statement text
                stmt_text = buf[stmt_start : semi + 1]
                pos = semi + 2
                break
            # Need more data
            search_from = max(0, len(buf) - 2)
            if not _refill():
                # EOF — take everything remaining as the last statement
                stmt_text = buf[stmt_start:]
                pos = len(buf)
                break

        # Feed the single statement to the existing parser
        yield from parser.iter_rows(stmt_text)
        _trim(pos)


def load_sql_dump_to_sqlite_streaming(
    snapshot_bytes: io.BytesIO,
    db_path: str,
    table_filter: set[str] | None = None,
) -> list[str]:
    """Stream a SQL dump from a snapshot zip into an on-disk SQLite file.

    When *table_filter* is set, uses C-speed regex scanning (~100 MB/s) to
    skip non-matching INSERT statements, only entering the Python parser
    for matching tables.  Without a filter, falls back to the full
    ``iter_sql_dump_from_stream`` parser.

    Args:
        snapshot_bytes: Snapshot zip (BytesIO).
        db_path: Path for the output SQLite file.
        table_filter: If provided, only load tables whose lowercased name
            is in this set.

    Returns:
        Sorted list of table names that were loaded.
    """
    dump_path = _find_sql_dump_path_in_snapshot(snapshot_bytes)
    if not dump_path:
        logger.info("No SQL dump found in snapshot")
        return []

    logger.info(
        f"Streaming SQL dump into SQLite: {dump_path}"
        + (f" (filter={sorted(table_filter)})" if table_filter else " (all tables)")
    )

    snapshot_bytes.seek(0)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-65536")  # 64 MB

    table_columns: dict[str, tuple[str, ...]] = {}
    pending: dict[str, list[list[Any]]] = {}
    pending_count = 0
    total_rows = 0

    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            with zf.open(dump_path) as raw:
                stream = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")

                if table_filter:
                    row_iter = _iter_filtered_rows_fast(stream, table_filter)
                else:
                    row_iter = iter_sql_dump_from_stream(stream)

                for table_name, row in row_iter:
                    if table_name not in table_columns:
                        columns = tuple(row.keys())
                        table_columns[table_name] = columns
                        col_defs = ", ".join(f'"{c}"' for c in columns)
                        conn.execute(
                            f'CREATE TABLE IF NOT EXISTS "{table_name}" ({col_defs})'
                        )

                    columns = table_columns[table_name]
                    values = [row.get(c) for c in columns]
                    pending.setdefault(table_name, []).append(values)
                    pending_count += 1
                    total_rows += 1

                    if pending_count >= _BATCH_SIZE:
                        _flush(conn, table_columns, pending)
                        pending.clear()
                        pending_count = 0

        _flush(conn, table_columns, pending)
        conn.commit()

    except Exception:
        conn.close()
        raise

    tables = sorted(table_columns.keys())
    logger.info(
        f"Loaded {total_rows:,} rows across {len(tables)} tables into {db_path}"
    )
    conn.close()
    return tables


def _flush(
    conn: sqlite3.Connection,
    table_columns: dict[str, tuple[str, ...]],
    pending: dict[str, list[list[Any]]],
) -> None:
    for table_name, rows in pending.items():
        if not rows:
            continue
        n = len(table_columns[table_name])
        ph = ",".join(["?"] * n)
        conn.executemany(f'INSERT INTO "{table_name}" VALUES ({ph})', rows)
    conn.commit()


def load_csvs_to_sqlite(
    snapshot_bytes: io.BytesIO,
    db_path: str,
    table_filter: set[str] | None = None,
) -> list[str]:
    """Load CSV files from a snapshot zip into an on-disk SQLite file.

    Each ``.csv`` file becomes a table named after its filename stem
    (lowercased).  Schema is inferred from the CSV header row.

    Args:
        snapshot_bytes: Snapshot zip (BytesIO).
        db_path: Path for the output SQLite file.
        table_filter: If provided, only load CSVs whose lowercased stem
            is in this set.

    Returns:
        Sorted list of table names that were loaded.
    """
    snapshot_bytes.seek(0)
    try:
        zf = zipfile.ZipFile(snapshot_bytes, "r")
    except zipfile.BadZipFile:
        logger.info("Snapshot is not a valid zip file; skipping CSV loading")
        return []

    csv_entries = [
        name
        for name in zf.namelist()
        if name.lower().endswith(".csv") and not name.startswith("__MACOSX")
    ]
    if not csv_entries:
        zf.close()
        return []

    logger.info(
        f"Loading {len(csv_entries)} CSV file(s) into SQLite"
        + (f" (filter={sorted(table_filter)})" if table_filter else "")
    )

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-65536")  # 64 MB

    table_columns: dict[str, tuple[str, ...]] = {}
    pending: dict[str, list[list[Any]]] = {}
    pending_count = 0
    total_rows = 0

    try:
        for entry in csv_entries:
            table_name = PurePosixPath(entry).stem.lower().replace("-", "_")
            if table_filter and table_name not in table_filter:
                continue

            with zf.open(entry) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
                reader = csv.DictReader(text)
                if reader.fieldnames is None:
                    continue

                columns = tuple(reader.fieldnames)
                table_columns[table_name] = columns
                col_defs = ", ".join(f'"{c}"' for c in columns)
                conn.execute(f'CREATE TABLE IF NOT EXISTS "{table_name}" ({col_defs})')

                for row in reader:
                    values = [row.get(c) for c in columns]
                    pending.setdefault(table_name, []).append(values)
                    pending_count += 1
                    total_rows += 1

                    if pending_count >= _BATCH_SIZE:
                        _flush(conn, table_columns, pending)
                        pending.clear()
                        pending_count = 0

        _flush(conn, table_columns, pending)
        conn.commit()
    except Exception:
        conn.close()
        zf.close()
        raise

    tables = sorted(table_columns.keys())
    logger.info(
        f"Loaded {total_rows:,} rows across {len(tables)} CSV table(s) into {db_path}"
    )
    conn.close()
    zf.close()
    return tables
