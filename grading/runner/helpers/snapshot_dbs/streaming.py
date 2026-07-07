"""Streaming SQLite loader for SQL dumps, CSV files, and raw .db extraction.

Streams a SQL dump directly from a snapshot zip into an on-disk SQLite file
using the chunked parser from ``parsers.sql``.  Never loads the full dump
into memory.  Also supports loading CSV files (one table per file, table
name derived from the filename stem).

When the snapshot contains a raw ``.db`` file (e.g. ``workspace.db``),
:func:`extract_db_from_snapshot` can extract it directly — skipping the
expensive SQL-dump-to-SQLite round-trip entirely.

Schema is inferred from the first row per table (all columns use SQLite's
TEXT affinity; dynamic typing handles numeric comparisons transparently).
No sqlglot transpilation is needed — INSERT data is dialect-agnostic for
basic value types, and CREATE TABLE is auto-generated from column names.
"""

from __future__ import annotations

import csv
import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import tokenize
import zipfile
from pathlib import PurePosixPath
from typing import IO, Any

from loguru import logger

from runner.helpers.artifact_state.parsers.sql import (
    SQLInsertParser,
    iter_sql_dump_from_stream,
)

_BATCH_SIZE = 100_000
_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB

# The csv module caps individual fields at 128 KB by default, which real
# snapshot data exceeds (e.g. multi-hundred-KB email bodies in
# gmail/emails.csv). min() guards against OverflowError on platforms whose
# C long is 32-bit.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

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

# SQL keywords, data types, and common tokens that the regex heuristic in
# ``_extract_table_names_from_code`` might capture as table names.  Keeping
# this as a module-level frozenset avoids rebuilding on every call.
_SQL_KEYWORDS: frozenset[str] = frozenset(
    {
        # Original set ----------------------------------------------------------
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
        # SQL data types --------------------------------------------------------
        "decimal",
        "integer",
        "int",
        "varchar",
        "char",
        "float",
        "double",
        "text",
        "boolean",
        "bool",
        "date",
        "time",
        "timestamp",
        "json",
        "jsonb",
        "blob",
        "real",
        "numeric",
        # JOIN qualifiers / CTE -------------------------------------------------
        "left",
        "right",
        "inner",
        "outer",
        "full",
        "cross",
        "using",
        "with",
        # DDL / DML --------------------------------------------------------------
        "create",
        "insert",
        "delete",
        "alter",
        "drop",
        "primary",
        "key",
        "foreign",
        "constraint",
        "unique",
        "default",
        "check",
        "index",
        "references",
        "cascade",
        "trigger",
        "view",
        # Transaction / procedural -----------------------------------------------
        "begin",
        "commit",
        "rollback",
        "each",
        "row",
        # Aggregation / ordering -------------------------------------------------
        "asc",
        "desc",
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "coalesce",
        "cast",
        # Window functions -------------------------------------------------------
        "over",
        "partition",
        "recursive",
        # INSERT conflict / RETURNING --------------------------------------------
        "returning",
        "conflict",
        "nothing",
        "do",
        # Set operations ---------------------------------------------------------
        "except",
        "intersect",
        # SQLite-specific --------------------------------------------------------
        "replace",
        "autoincrement",
        "temp",
        "temporary",
        # Miscellaneous ----------------------------------------------------------
        "no",
        "action",
        "only",
    }
)


def _quote_ident(name: str) -> str:
    """Quote a SQL identifier (table/column) by doubling embedded quotes."""
    return '"' + name.replace('"', '""') + '"'


def _table_names(conn: sqlite3.Connection) -> set[str]:
    """Return the set of user table names currently in *conn*."""
    return {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _is_safe_zip_member_name(name: str) -> bool:
    """Reject path-traversal / absolute zip members without over-matching ``..``.

    A bare ``..`` substring is too broad — it rejects legitimate names like
    ``my..db``. Only an absolute path or a literal ``..`` *path segment* escapes
    the extraction root.
    """
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts


def _enumerate_db_entries(snapshot_bytes: IO[bytes]) -> list[str]:
    """All ``.db`` entry paths in a snapshot zip, best-first.

    Ordered by the same priority as the historical single-pick logic
    (``.apps_data/`` first, then shallowest, then largest) so the no-filter
    path still selects the same primary DB it always did. Suspicious
    (path-traversal) entries are dropped.
    """
    snapshot_bytes.seek(0)
    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            # (priority, depth, -size, path) — lower priority = preferred.
            # .apps_data/ entries get priority 0, everything else gets 1.
            candidates: list[tuple[int, int, int, str]] = []
            for info in zf.infolist():
                if info.filename.endswith("/") or not _is_safe_zip_member_name(
                    info.filename
                ):
                    continue
                basename = info.filename.rstrip("/").rsplit("/", 1)[-1]
                if basename.endswith(".db"):
                    in_apps_data = ".apps_data/" in info.filename
                    priority = 0 if in_apps_data else 1
                    depth = info.filename.count("/")
                    candidates.append((priority, depth, -info.file_size, info.filename))
            candidates.sort()
            return [c[3] for c in candidates]
    except (zipfile.BadZipFile, KeyError, OSError):
        return []


def _find_db_path_in_snapshot(snapshot_bytes: IO[bytes]) -> str | None:
    """The single best ``.db`` path (``.apps_data/`` first, shallowest, largest).

    Used by the no-filter extraction path. Returns ``None`` when no ``.db`` is
    present.
    """
    entries = _enumerate_db_entries(snapshot_bytes)
    return entries[0] if entries else None


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _copy_zip_member_to_temp(zf: zipfile.ZipFile, entry: str) -> str | None:
    """Extract a single zip member to a fresh temp file; return its path."""
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="srcdb_")
    os.close(fd)
    try:
        with zf.open(entry) as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        logger.warning(f"Failed to extract .db member {entry}: {exc}")
        _unlink_quiet(tmp)
        return None
    return tmp


def _copy_table_from_attached_db(out: sqlite3.Connection, table_name: str) -> None:
    """Copy one table from the attached ``src`` DB into *out*, schema and all.

    ``CREATE TABLE ... AS SELECT`` only copies rows + column names — it loses
    primary keys, UNIQUE/CHECK constraints, defaults, indexes and triggers. So
    we replay the source's own ``CREATE TABLE`` DDL, copy rows, then best-effort
    replay the table's user indexes/triggers. If the DDL is missing or the
    schema-preserving copy fails (e.g. a generated column ``SELECT *`` can't
    insert), we drop any partial table and fall back to ``AS SELECT``. ``src`` is
    expected to be ATTACHed by the caller.
    """
    q = _quote_ident(table_name)
    row = out.execute(
        "SELECT sql FROM src.sqlite_master "
        "WHERE type='table' AND name=? AND name NOT LIKE 'sqlite_%'",
        (table_name,),
    ).fetchone()
    ddl = row[0] if row else None

    if ddl:
        try:
            out.execute(ddl)
            out.execute(f"INSERT INTO {q} SELECT * FROM src.{q}")
        except sqlite3.Error as exc:
            logger.warning(
                f"Schema-preserving copy of {table_name!r} failed ({exc}); "
                "falling back to CREATE TABLE AS SELECT"
            )
            out.execute(f"DROP TABLE IF EXISTS {q}")
            out.execute(f"CREATE TABLE {q} AS SELECT * FROM src.{q}")
            return
    else:
        logger.warning(
            f"No CREATE TABLE DDL for {table_name!r} in source; "
            "using CREATE TABLE AS SELECT"
        )
        out.execute(f"DROP TABLE IF EXISTS {q}")
        out.execute(f"CREATE TABLE {q} AS SELECT * FROM src.{q}")
        return

    # Replay user-created indexes/triggers (skip internal/auto objects, which
    # have no SQL or a sqlite_-prefixed name and are recreated by the DDL). A
    # failure on one object must not abort the already-copied table data.
    aux = out.execute(
        "SELECT sql FROM src.sqlite_master "
        "WHERE type IN ('index', 'trigger') AND tbl_name=? "
        "AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL AND sql != ''",
        (table_name,),
    ).fetchall()
    for (obj_sql,) in aux:
        try:
            out.execute(obj_sql)
        except sqlite3.Error as exc:
            logger.warning(f"Skipping an index/trigger copy for {table_name!r}: {exc}")


def _merge_filtered_tables_from_dbs(
    snapshot_bytes: IO[bytes],
    db_path: str,
    table_filter: set[str] | None,
) -> list[str]:
    """Union tables across every ``.db`` in the snapshot.

    Fixes multi-app shadowing: a verifier's tables can live in a non-largest
    app DB that the old size-based single pick skipped. Copies tables into
    *db_path*, preferring the source with the most tables first and skipping
    names already loaded from an earlier source.

    With a *table_filter*, only matching tables are copied. With ``None`` (the
    no-filter path for multi-app worlds), every table from every app DB is
    unioned — so a verifier that discovers its table dynamically via
    ``list_tables()`` sees the whole set instead of one shadowed app's DB.
    A same-name collision across apps keeps the first (largest) source's copy
    and logs a warning, so a generic name can't silently load the wrong app's.
    """
    entries = _enumerate_db_entries(snapshot_bytes)
    if not entries:
        return []

    # First pass: materialize each source DB and record which filter tables it
    # has, so we can copy from the best-covering source first.
    sources: list[tuple[int, str, list[str]]] = []  # (match_count, tmp_path, tables)
    snapshot_bytes.seek(0)
    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            for entry in entries:
                tmp = _copy_zip_member_to_temp(zf, entry)
                if tmp is None:
                    continue
                # Unlink unless this tmp is handed off to `sources`, so it never
                # leaks even if an unexpected error escapes mid-iteration.
                kept = False
                try:
                    sconn = sqlite3.connect(tmp)
                    try:
                        names = [
                            str(r[0])
                            for r in sconn.execute(
                                "SELECT name FROM sqlite_master WHERE type='table' "
                                "AND name NOT LIKE 'sqlite_%'"
                            ).fetchall()
                        ]
                    finally:
                        sconn.close()
                    matching = (
                        names
                        if table_filter is None
                        else [n for n in names if n.lower() in table_filter]
                    )
                    if matching:
                        sources.append((len(matching), tmp, matching))
                        kept = True
                except sqlite3.Error as exc:
                    logger.warning(f"Skipping non-SQLite .db {entry}: {exc}")
                finally:
                    if not kept:
                        _unlink_quiet(tmp)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        logger.warning(f"Failed to scan .db files in snapshot: {exc}")

    if not sources:
        return []

    sources.sort(key=lambda s: -s[0])  # most filter coverage first
    open(db_path, "wb").close()  # clean output before merging
    loaded: list[str] = []
    loaded_lower: set[str] = set()
    out = sqlite3.connect(db_path)
    try:
        for _count, tmp, matching in sources:
            try:
                out.execute("ATTACH DATABASE ? AS src", (tmp,))
            except sqlite3.Error as exc:
                logger.warning(f"ATTACH failed for a source DB: {exc}")
                continue
            try:
                for name in matching:
                    if name.lower() in loaded_lower:
                        # Same table name in two app DBs: keep the first
                        # (largest source) copy and flag it, so a generic name
                        # can't silently resolve to the wrong app's data.
                        logger.warning(
                            f"Table {name!r} present in multiple app DBs; "
                            "keeping the first (largest source) copy"
                        )
                        continue
                    # Commit per table and only record it as loaded after the
                    # commit succeeds: a later table failing must not roll back
                    # (and thus un-persist) an earlier one while its name still
                    # tells the caller to skip the dump/CSV fallback.
                    try:
                        _copy_table_from_attached_db(out, name)
                        out.commit()
                    except sqlite3.Error as exc:
                        logger.warning(
                            f"Copying table {name!r} from a source DB failed: {exc}"
                        )
                        try:
                            out.rollback()
                        except sqlite3.Error:
                            pass
                        continue
                    loaded.append(name)
                    loaded_lower.add(name.lower())
            finally:
                try:
                    out.execute("DETACH DATABASE src")
                except sqlite3.Error:
                    pass
    finally:
        out.close()
        for _count, tmp, _matching in sources:
            _unlink_quiet(tmp)

    if loaded:
        scope = "filter-matching " if table_filter is not None else ""
        logger.info(
            f"Loaded {len(loaded)} {scope}table(s) across "
            f"{len(entries)} app DB(s): {sorted(loaded)}"
        )
    return sorted(loaded)


def extract_db_from_snapshot(
    snapshot_bytes: IO[bytes],
    db_path: str,
    table_filter: set[str] | None = None,
) -> list[str]:
    """Build *db_path* from the snapshot's SQLite ``.db`` file(s).

    With a *table_filter*, unions matching tables across all app ``.db`` files
    (so tables in a non-largest DB still load — the multi-app shadowing fix);
    without one, copies the single best ``.db``. Returns loaded table names, or
    ``[]`` when no usable ``.db`` is found (caller falls through to the dump).
    """
    if table_filter:
        merged = _merge_filtered_tables_from_dbs(snapshot_bytes, db_path, table_filter)
        if not merged:
            # No app DB held the referenced tables — leave a clean file so the
            # SQL-dump / CSV loaders start fresh.
            open(db_path, "wb").close()
        return merged

    # No filter (e.g. a verifier that discovers its table via list_tables()):
    # in a multi-app world the single-best pick would shadow the other app DBs,
    # so union every app DB's tables. Single-app (0 or 1 .db) keeps the existing
    # fast raw-copy path below.
    if len(_enumerate_db_entries(snapshot_bytes)) > 1:
        merged = _merge_filtered_tables_from_dbs(snapshot_bytes, db_path, None)
        if not merged:
            open(db_path, "wb").close()
        return merged

    entry_path = _find_db_path_in_snapshot(snapshot_bytes)
    if not entry_path:
        return []

    logger.info(f"Extracting .db file from snapshot: {entry_path}")

    snapshot_bytes.seek(0)
    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            # Validate the entry path doesn't escape the extraction target
            # (defence against zip-slip / path traversal).
            member = zf.getinfo(entry_path)
            if member.filename != entry_path or not _is_safe_zip_member_name(
                entry_path
            ):
                logger.warning(f"Skipping suspicious zip entry: {entry_path}")
                return []

            with zf.open(entry_path) as src, open(db_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        logger.warning(f"Failed to extract .db from snapshot: {exc}")
        open(db_path, "wb").close()
        return []

    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        logger.warning(f"Extracted .db is not a valid SQLite database: {exc}")
        open(db_path, "wb").close()
        return []

    try:
        tables = sorted(
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        )
    except sqlite3.Error as exc:
        logger.warning(f"Failed to read tables from extracted .db: {exc}")
        conn.close()
        open(db_path, "wb").close()
        return []
    else:
        conn.close()

    if tables:
        logger.info(f"Extracted {len(tables)} table(s) from .db file: {tables}")
    return tables


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


def _strip_comments(code: str) -> str:
    """Blank ``#`` comments, leaving all other source byte-for-byte intact.

    Comments are pure prose, so mining them for SQL invents phantom tables
    (``# pick the issues table`` -> ``table``). Blanking only the comment spans
    keeps string literals — where the real SQL lives, including f-strings and
    implicitly concatenated strings — exactly as written so they're still
    scanned. Code is AST-gate-valid here; fall back to raw if tokenizing fails.
    """
    try:
        comments = [
            tok
            for tok in tokenize.generate_tokens(io.StringIO(code).readline)
            if tok.type == tokenize.COMMENT
        ]
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError) as exc:
        # Should be rare — code is AST-gate-valid here. Log so that if comment
        # stripping is skipped (and phantom-table extraction can resurface),
        # it's diagnosable rather than a mystery table.
        logger.warning(f"Comment-stripping failed; scanning raw code: {exc}")
        return code
    if not comments:
        return code
    lines = code.splitlines(keepends=True)
    for tok in comments:  # comments are single-line: start/end share a row
        (row, scol), (_, ecol) = tok.start, tok.end
        line = lines[row - 1]
        lines[row - 1] = line[:scol] + " " * (ecol - scol) + line[ecol:]
    return "".join(lines)


# Filename extensions used to discard ``from report.xlsx``-style prose that the
# FROM pattern would otherwise read as a table named ``report``.
_FILE_EXTENSIONS: frozenset[str] = frozenset(
    {
        "xlsx",
        "xls",
        "csv",
        "tsv",
        "json",
        "jsonl",
        "xml",
        "yaml",
        "yml",
        "txt",
        "log",
        "pdf",
        "doc",
        "docx",
        "html",
        "htm",
        "md",
        "rst",
        "sql",
        "db",
        "sqlite",
        "sqlite3",
        "py",
        "js",
        "ts",
        "jsx",
        "tsx",
        "css",
        "scss",
        "zip",
        "gz",
        "tar",
        "bz2",
        "xz",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "svg",
        "ico",
    }
)


def _extract_table_names_from_code(code: str) -> set[str]:
    """Heuristically extract SQL table names referenced in verifier code.

    Scans comment-stripped source (so prose in ``#`` comments can't invent
    phantom tables) and omits the ``INSERT INTO`` / ``UPDATE`` / ``CREATE TABLE``
    patterns — the verifier DB is read-only, so those never appear in real
    verifier SQL and only ever matched English (``"...table loaded"`` -> ``loaded``).
    Returns lowercased names; still best-effort (prose inside a non-SQL string
    can leak the odd false positive).
    """
    text = _strip_comments(code)
    tables: set[str] = set()
    for pattern in (
        r"FROM\s+[`'\"]?(?:\w+\.)?(\w+)",
        r"JOIN\s+[`'\"]?(?:\w+\.)?(\w+)",
        # ctx API methods that take a table name as a literal string argument.
        r"table_row_count\(\s*[\"'](\w+)[\"']",
        r"table_columns\(\s*[\"'](\w+)[\"']",
    ):
        for m in re.finditer(pattern, text, re.IGNORECASE):
            tables.add(m.group(1).lower())

    # Remove SQL keywords / data types the regex might capture.
    tables -= _SQL_KEYWORDS

    # Remove Python import targets: ``from decimal import Decimal``.
    for m in re.finditer(r"\bfrom\s+(\w+)\s+import\b", text, re.IGNORECASE):
        tables.discard(m.group(1).lower())

    # Remove file-reference stems: ``from report.xlsx`` (not a schema-qualified table).
    for m in re.finditer(r"\bfrom\s+(\w+)\.(\w+)", text, re.IGNORECASE):
        if m.group(2).lower() in _FILE_EXTENSIONS:
            tables.discard(m.group(1).lower())
            tables.discard(m.group(2).lower())

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


def _enumerate_sql_dump_entries(snapshot_bytes: IO[bytes]) -> list[str]:
    """All SQL-dump entry paths in a snapshot, best-first (shallowest, largest).

    Matches the same names as the historical single-dump finder
    (``*_dump.sql`` / ``database_dump.sql``). Best-first ordering keeps the
    no-filter path selecting the same dump it always did.
    """
    snapshot_bytes.seek(0)
    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            candidates: list[tuple[int, int, str]] = []
            for info in zf.infolist():
                if info.filename.endswith("/") or not _is_safe_zip_member_name(
                    info.filename
                ):
                    continue
                basename = info.filename.rstrip("/").rsplit("/", 1)[-1]
                if basename.endswith("_dump.sql") or basename == "database_dump.sql":
                    depth = info.filename.count("/")
                    candidates.append((depth, -info.file_size, info.filename))
            candidates.sort()
            return [c[2] for c in candidates]
    except (zipfile.BadZipFile, KeyError, OSError):
        return []


def _stream_one_dump_into_conn(
    zf: zipfile.ZipFile,
    dump_path: str,
    conn: sqlite3.Connection,
    table_filter: set[str] | None,
) -> set[str]:
    """Stream a single dump entry into *conn*; return the table names created."""
    table_columns: dict[str, tuple[str, ...]] = {}
    pending: dict[str, list[list[Any]]] = {}
    pending_count = 0
    total_rows = 0
    with zf.open(dump_path) as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
        row_iter = (
            _iter_filtered_rows_fast(stream, table_filter)
            if table_filter
            else iter_sql_dump_from_stream(stream)
        )
        for table_name, row in row_iter:
            if table_name not in table_columns:
                columns = tuple(row.keys())
                table_columns[table_name] = columns
                col_defs = ", ".join(f'"{c}"' for c in columns)
                conn.execute(f'CREATE TABLE IF NOT EXISTS "{table_name}" ({col_defs})')
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
    if total_rows:
        logger.info(
            f"Streamed {total_rows:,} rows from {dump_path} into "
            f"{len(table_columns)} table(s)"
        )
    return set(table_columns.keys())


def load_sql_dump_to_sqlite_streaming(
    snapshot_bytes: IO[bytes],
    db_path: str,
    table_filter: set[str] | None = None,
) -> list[str]:
    """Stream SQL dump(s) from a snapshot zip into SQLite.

    With a *table_filter*, scans every ``*_dump.sql`` and loads each table from
    the first dump that has it (multi-app shadowing fix); without one, streams
    the single best dump. Uses C-speed scanning to skip non-matching INSERTs
    when filtered. Returns the sorted list of loaded table names.
    """
    dump_paths = _enumerate_sql_dump_entries(snapshot_bytes)
    if not dump_paths:
        logger.info("No SQL dump found in snapshot")
        return []

    snapshot_bytes.seek(0)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-65536")  # 64 MB

    created: set[str] = set()
    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            if not table_filter:
                logger.info(
                    f"Streaming SQL dump into SQLite: {dump_paths[0]} (all tables)"
                )
                created |= _stream_one_dump_into_conn(zf, dump_paths[0], conn, None)
            else:
                # Load each filter table from the FIRST dump that has it; stop
                # once the filter is satisfied. First-dump-wins avoids merging
                # rows for a same-named table across apps.
                remaining = {t.lower() for t in table_filter}
                logger.info(
                    f"Streaming up to {len(dump_paths)} SQL dump(s) "
                    f"(filter={sorted(remaining)})"
                )
                for dump_path in dump_paths:
                    if not remaining:
                        break
                    before = _table_names(conn)
                    try:
                        newly = _stream_one_dump_into_conn(
                            zf, dump_path, conn, remaining
                        )
                    except (
                        sqlite3.Error,
                        ValueError,
                        OSError,
                        zipfile.BadZipFile,
                    ) as exc:
                        # A later dump failing must not discard tables already
                        # loaded+committed from earlier dumps. But _flush commits
                        # per batch, so this dump may have left a partially-loaded
                        # table; first-dump-wins guarantees any table it touched
                        # is new, so drop those (and roll back its uncommitted
                        # rows) — the name stays in `remaining` and a later
                        # dump/CSV reloads it cleanly instead of INSERTing on top.
                        logger.warning(
                            f"Skipping unreadable SQL dump {dump_path}: {exc}"
                        )
                        try:
                            conn.rollback()
                            for tbl in _table_names(conn) - before:
                                conn.execute(
                                    f"DROP TABLE IF EXISTS {_quote_ident(tbl)}"
                                )
                            conn.commit()
                        except sqlite3.Error:
                            pass
                        continue
                    created |= newly
                    remaining -= {t.lower() for t in newly}
    except Exception:
        conn.close()
        raise

    conn.close()
    tables = sorted(created)
    if tables:
        logger.info(f"Loaded {len(tables)} table(s) into {db_path}: {tables}")
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
    snapshot_bytes: IO[bytes],
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
    # Tables fully loaded by previous entries. Lets a failing entry whose
    # stem collides with an already-loaded table preserve that table.
    completed_tables: set[str] = set()

    try:
        for entry in csv_entries:
            table_name = PurePosixPath(entry).stem.lower().replace("-", "_")
            if table_filter and table_name not in table_filter:
                continue

            entry_rows = 0
            prev_columns = table_columns.get(table_name)
            try:
                with zf.open(entry) as raw:
                    text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
                    reader = csv.DictReader(text)
                    if reader.fieldnames is None:
                        continue

                    columns = tuple(reader.fieldnames)
                    table_columns[table_name] = columns
                    col_defs = ", ".join(f'"{c}"' for c in columns)
                    conn.execute(
                        f'CREATE TABLE IF NOT EXISTS "{table_name}" ({col_defs})'
                    )

                    for row in reader:
                        values = [row.get(c) for c in columns]
                        pending.setdefault(table_name, []).append(values)
                        pending_count += 1
                        entry_rows += 1
                        total_rows += 1

                        if pending_count >= _BATCH_SIZE:
                            _flush(conn, table_columns, pending)
                            pending.clear()
                            pending_count = 0

                # Flush at entry boundaries so ``pending`` never mixes rows
                # from different entries: a ``_flush`` failure is then always
                # attributable to the entry being processed.
                _flush(conn, table_columns, pending)
                pending.clear()
                pending_count = 0
                completed_tables.add(table_name)
            except (csv.Error, sqlite3.Error, OSError, zipfile.BadZipFile) as exc:
                # A malformed entry must not abort the remaining CSVs, but
                # grading against a partially loaded table would emit false
                # fails — undo this entry and let the caller surface
                # "no tables" for it instead. Roll back first: a failure
                # inside ``_flush`` leaves uncommitted rows in an open
                # transaction that a later commit would otherwise leak in.
                conn.rollback()
                committed_rows = entry_rows - len(pending.get(table_name) or [])
                pending.pop(table_name, None)
                pending_count = 0
                total_rows -= entry_rows
                if table_name in completed_tables and committed_rows == 0:
                    # A previous entry fully loaded this table and none of
                    # this entry's rows reached SQLite — keep the table.
                    if prev_columns is not None:
                        table_columns[table_name] = prev_columns
                    logger.warning(
                        f"Skipping CSV entry {entry} ({exc}); keeping table "
                        f"{table_name!r} loaded from a previous entry"
                    )
                else:
                    table_columns.pop(table_name, None)
                    completed_tables.discard(table_name)
                    conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
                    logger.warning(
                        f"Skipping CSV entry {entry} ({exc}); dropping partial "
                        f"table {table_name!r}"
                    )

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


def load_snapshot_tables(
    snapshot_bytes: IO[bytes],
    db_path: str,
    table_filter: set[str] | None = None,
    log_prefix: str = "[DB_CODE_VERIFIER]",
) -> list[str]:
    """Load a verifier's tables from a snapshot into a single SQLite *db_path*.

    Runs the ``.db`` → dump → CSV cascade. With a *table_filter* it accumulates
    across all three (each loads only the still-missing tables) so tables split
    across formats/apps all load; without one, the first source that yields
    anything wins. ``extract_db_from_snapshot`` runs first and truncates
    *db_path*; the dump/CSV loaders append. Returns the sorted union.
    """
    loaders = (
        (extract_db_from_snapshot, "extract .db"),
        (load_sql_dump_to_sqlite_streaming, "load SQL dump"),
        (load_csvs_to_sqlite, "load CSVs"),
    )
    collected: set[str] = set()
    # Lowercase the filter once: lower-level loaders compare ``name.lower() in
    # table_filter`` and we subtract lowercased loaded names, so a mixed-case
    # filter (e.g. {"Drive_Files"}) must be normalized to behave consistently.
    remaining: set[str] | None = (
        {t.lower() for t in table_filter} if table_filter else None
    )

    for loader, label in loaders:
        if table_filter is not None:
            if not remaining:  # every referenced table already loaded
                break
            arg: set[str] | None = remaining
        else:
            if collected:  # no filter: first source that loads anything wins
                break
            arg = None
        try:
            snapshot_bytes.seek(0)
            got = loader(snapshot_bytes, db_path, arg)
        except Exception as exc:  # noqa: BLE001 — degrade to the next source
            logger.warning(f"{log_prefix} {label} failed: {exc}")
            continue
        if got:
            collected.update(got)
            if remaining is not None:
                remaining -= {t.lower() for t in got}

    return sorted(collected)
