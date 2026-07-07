"""DB Diff helper - extracts and diffs databases between snapshots.

Supports SQLite (.db) files, MySQL/MariaDB INSERT-format SQL dumps,
PostgreSQL COPY-format SQL dumps, and JSON data files.  Auto-detects the
database type and uses the appropriate parsing strategy.
"""

import codecs
import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import IO, Any

from loguru import logger

from runner.helpers.artifact_state.parsers.sql import iter_sql_dump_from_stream
from runner.models import AgentTrajectoryOutput

# Maximum number of rows to include in diff output to avoid token limits
MAX_ROWS_PER_TABLE = 100

# Page cache (KiB) for the diff connection. Memory is bounded by this, NOT by
# the size of the attached database files, so large (multi-GB) snapshots are
# paged from disk rather than loaded into RAM. Negative = KiB (SQLite syntax).
DB_DIFF_CACHE_KIB = 262144  # 256 MB

# I/O chunk size for streaming a DB out of a snapshot zip without loading the
# whole (potentially multi-GB) file into memory.
_EXTRACT_CHUNK_BYTES = 8 * 1024 * 1024

# System/internal tables to filter out from SQL dumps
# Note: SQLInsertParser lowercases table names, so use lowercase here
SQL_DUMP_SYSTEM_TABLES = {
    # Frappe / ERPNext
    "__auth",
    "__usersettings",
    "__global_search",
    "tabversion",
    "tabactivity log",
    "tabcomment",
    "tabview log",
    "tabaccess log",
    "tabenergy point log",
    "tabsession default",
    # Liquibase (Apache Fineract and other Java apps)
    "databasechangelog",
    "databasechangeloglock",
    # Spring Batch (standard schema used by Fineract's job framework)
    "batch_job_execution",
    "batch_job_execution_context",
    "batch_job_execution_params",
    "batch_job_instance",
    "batch_step_execution",
    "batch_step_execution_context",
    # Fineract scheduler (only clearly-prefixed names to avoid collisions
    # with user-facing "job" tables in other apps)
    "batch_custom_job_parameters",
    "scheduled_job_detail",
    "scheduler_detail",
}

# Filenames to skip when scanning for JSON data files (config/tooling files)
JSON_DENYLIST_FILENAMES = {
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "jsconfig.json",
    ".eslintrc.json",
    "babel.config.json",
    "manifest.json",
    "composer.json",
    "composer.lock",
    "appsettings.json",
    "launch.json",
    "settings.json",
}

# Fields to try (in order) when auto-detecting a row identity key in JSON arrays
_JSON_ID_FIELD_HEURISTICS = ["id", "Id", "ID", "_id", "uuid", "key", "name", "slug"]


@dataclass
class DbConnection:
    """Wrapper for SQLite connection with temp file tracking for cleanup."""

    conn: sqlite3.Connection
    temp_path: str

    def close(self) -> None:
        """Close connection and clean up temp file."""
        try:
            self.conn.close()
        finally:
            try:
                os.unlink(self.temp_path)
            except OSError:
                pass  # Best effort cleanup


# Every SQLite database file begins with this 16-byte magic string. Multi-app
# snapshots ship .db files from other engines (e.g. Metabase's H2 ``.mv.db``,
# XML exports named ``database.db``). ATTACH-ing those raises "file is not a
# database", which — in the unguarded per-db diff loop — aborts the entire diff.
# We skip any .db whose header isn't SQLite.
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _db_connection_from_stream(
    src: IO[bytes], name: str | None = None
) -> DbConnection | None:
    """Stream a binary source into a temp SQLite file and open a connection.

    Shared by the zip-based ``_extract_db_from_snapshot`` and the file-level
    ``diff_sqlite_artifact``. ``copyfileobj`` copies in fixed-size chunks, so
    peak memory is the buffer size, not the whole database — important for
    multi-GB .db files that would otherwise be read fully into memory.

    Returns None (after unlinking the temp file) when the bytes aren't a SQLite
    database (e.g. H2 ``.mv.db`` or an XML export named ``database.db``), which
    would otherwise fail at ATTACH and abort the whole diff. ``name`` is used
    only for the skip log message.
    """
    temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False, mode="wb")
    temp_path = temp_file.name
    try:
        shutil.copyfileobj(src, temp_file, length=_EXTRACT_CHUNK_BYTES)
        temp_file.flush()
    finally:
        temp_file.close()

    # Skip .db files that aren't SQLite (H2, XML exports, etc.). They'd fail at
    # ATTACH ("file is not a database") and abort the whole diff; returning None
    # makes the per-db loops treat it as "no DB this side".
    with open(temp_path, "rb") as fh:
        header = fh.read(16)
    if header != _SQLITE_MAGIC:
        label = f" {name}" if name else ""
        logger.warning(f"Skipping non-SQLite .db file{label} (header={header!r})")
        os.unlink(temp_path)
        return None

    return DbConnection(conn=sqlite3.connect(temp_path), temp_path=temp_path)


def _extract_db_from_snapshot(
    snapshot_bytes: IO[bytes], db_path: str
) -> DbConnection | None:
    """
    Extract a specific database file from a snapshot zip.

    Args:
        snapshot_bytes: Snapshot zip as BytesIO
        db_path: Path to the database file within the zip

    Returns:
        DbConnection wrapper if found, None otherwise
    """
    snapshot_bytes.seek(0)

    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            names = set(zf.namelist())
            # Accept the exact path, or with a leading slash stripped/added.
            # Only names that exist in the archive are used.
            resolved_path = next(
                (
                    candidate
                    for candidate in (
                        db_path,
                        db_path.lstrip("/"),
                        "/" + db_path.lstrip("/"),
                    )
                    if candidate in names
                ),
                None,
            )
            if resolved_path is None:
                return None

            with zf.open(resolved_path) as src:
                return _db_connection_from_stream(src, name=db_path)
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        logger.warning(f"Failed to extract database {db_path}: {e}")
        return None


def _find_all_dbs_in_snapshot(snapshot_bytes: IO[bytes]) -> list[str]:
    """Find all .db files in a snapshot, scoped to .apps_data/ only.

    Legitimate app databases live under .apps_data/<appname>/ by design.
    filesystem/ contains the agent's working Linux FS and may include real
    but irrelevant SQLite files (e.g. Chromium profile DBs from Playwright
    MCP) that would otherwise trigger the opened_any_sqlite early return and
    prevent the SQL dump path from running.
    """
    snapshot_bytes.seek(0)
    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            return [
                f
                for f in zf.namelist()
                if f.endswith(".db") and f.startswith(".apps_data/")
            ]
    except zipfile.BadZipFile:
        return []


def _q(identifier: str) -> str:
    """Quote a SQL identifier (table/column name) by doubling embedded quotes.

    Identifiers always originate from the SQLite catalog (PRAGMA/sqlite_master),
    never from user input, so this is defensive quoting rather than trust
    boundary enforcement.
    """
    return '"' + identifier.replace('"', '""') + '"'


def _open_attached(
    initial_path: str | None, final_path: str | None
) -> sqlite3.Connection:
    """Open one read-only connection with both snapshot DBs attached.

    The initial snapshot is attached as ``initdb`` and the final as ``findb`` so
    the entire diff runs inside the SQLite engine (set operations over the PK
    index) rather than materializing whole tables in Python. Peak memory is
    bounded by the page cache (``DB_DIFF_CACHE_KIB``), independent of file size.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(f"PRAGMA cache_size=-{DB_DIFF_CACHE_KIB}")
    # ATTACH takes the path as a bound parameter (a value, not an identifier).
    if initial_path:
        conn.execute("ATTACH DATABASE ? AS initdb", (initial_path,))
    if final_path:
        conn.execute("ATTACH DATABASE ? AS findb", (final_path,))
    conn.execute("PRAGMA query_only=ON")  # belt-and-suspenders: never write
    return conn


def _schema_table_names(conn: sqlite3.Connection, schema: str) -> list[str]:
    """Get user table names from an attached schema (``initdb``/``findb``)."""
    cursor = conn.execute(
        f"SELECT name FROM {schema}.sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return [row[0] for row in cursor.fetchall()]


def _schema_columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    """Column names for a table in an attached schema (catalog-derived)."""
    cursor = conn.execute(f"PRAGMA {schema}.table_info({_q(table)})")
    return [row[1] for row in cursor.fetchall()]


def _schema_pk(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    """Primary-key columns (in key order) for a table in an attached schema."""
    cursor = conn.execute(f"PRAGMA {schema}.table_info({_q(table)})")
    # row[5] is the 1-based position within the PK (0 = not part of the PK).
    pk = [(row[5], row[1]) for row in cursor.fetchall() if row[5] and row[5] > 0]
    return [name for _, name in sorted(pk)]


def _safe_value(v: Any) -> Any:
    """Convert a value to a JSON-serializable type."""
    if isinstance(v, (bytes, memoryview)):
        return bytes(v).decode("utf-8", errors="replace")
    return v


def _dict_from_row(columns: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
    """Convert a row tuple to a dict with column names."""
    return {col: _safe_value(val) for col, val in zip(columns, row, strict=False)}


def _table_has_null_pk(
    conn: sqlite3.Connection, quoted_table: str, pk_cols: list[str]
) -> bool:
    """True if any PK column contains NULL in either attached snapshot.

    A NULL in a PK column makes key-based matching unsound (``NULL = NULL`` is
    false and multiple NULL-PK rows are indistinguishable), so callers fall back
    to a key-less full-row diff. Cheap, index-assisted existence probe.
    """
    cond = " OR ".join(f"{_q(c)} IS NULL" for c in pk_cols)
    for schema in ("initdb", "findb"):
        hit = conn.execute(
            f"SELECT 1 FROM {schema}.{quoted_table} WHERE {cond} LIMIT 1"
        ).fetchone()
        if hit is not None:
            return True
    return False


def _empty_table_diff() -> dict[str, Any]:
    return {
        "rows_added": [],
        "rows_deleted": [],
        "rows_modified": [],
        # Exact change counts (independent of how many row bodies are
        # materialized below). The summary/judge read these, so they stay
        # truthful even when the row lists are capped.
        "counts": {"added": 0, "deleted": 0, "modified": 0},
        "truncated": False,
        "error": None,
    }


def _fetch_capped(
    conn: sqlite3.Connection, sql: str
) -> tuple[list[tuple[Any, ...]], int, bool]:
    """Run ``sql`` and return (rows, total_count, truncated).

    Materializes at most ``MAX_ROWS_PER_TABLE`` rows for the judge. Fetches one
    extra row to detect truncation cheaply; only runs a COUNT(*) when the cap is
    actually exceeded, so the common (sparse) case avoids a second scan.
    """
    rows = conn.execute(f"{sql} LIMIT {MAX_ROWS_PER_TABLE + 1}").fetchall()
    if len(rows) <= MAX_ROWS_PER_TABLE:
        return rows, len(rows), False
    total = conn.execute(f"SELECT COUNT(*) FROM ({sql})").fetchone()[0]
    return rows[:MAX_ROWS_PER_TABLE], total, True


def _diff_table_no_key(
    conn: sqlite3.Connection,
    qt: str,
    final_cols: list[str],
    initial_cols: list[str],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Fallback for tables without a primary key.

    Uses full-row set difference inside SQLite (still no whole-table Python
    materialization). Without a key we cannot distinguish a modification from a
    delete+insert, so differing rows surface as added/deleted.
    """
    result["no_stable_key"] = True
    logger.warning(f"Table {qt} has no primary key; using full-row set diff")
    try:
        rows, total, trunc = _fetch_capped(
            conn, f"SELECT * FROM findb.{qt} EXCEPT SELECT * FROM initdb.{qt}"
        )
        result["rows_added"] = [_dict_from_row(final_cols, r) for r in rows]
        result["counts"]["added"] = total
        result["truncated"] = result["truncated"] or trunc

        rows, total, trunc = _fetch_capped(
            conn, f"SELECT * FROM initdb.{qt} EXCEPT SELECT * FROM findb.{qt}"
        )
        result["rows_deleted"] = [_dict_from_row(initial_cols, r) for r in rows]
        result["counts"]["deleted"] = total
        result["truncated"] = result["truncated"] or trunc
    except sqlite3.OperationalError as e:
        # EXCEPT requires matching column shapes; schema drift on a key-less
        # table lands here. Surface rather than silently report "no changes".
        result["error"] = f"key-less diff failed: {e}"
    return result


def _diff_table(
    conn: sqlite3.Connection,
    table_name: str,
    in_initial: bool,
    in_final: bool,
) -> dict[str, Any]:
    """Diff one table between the attached ``initdb`` and ``findb`` snapshots
    using three SQL set operations.

    Detection is complete — there is no row-read cap, so an appended row (e.g. a
    newly-sent email at the highest rowid in a 100k-row table) is always found.
    Only the row *bodies* handed to the judge are bounded by
    ``MAX_ROWS_PER_TABLE``; ``counts`` always reflects the true totals.
    """
    result = _empty_table_diff()

    if not in_initial and not in_final:
        result["error"] = "Table not found in either snapshot"
        return result

    # Column/PK names come straight from the SQLite catalog (allow-listed by
    # definition) and are quoted before being spliced into SQL.
    final_cols = _schema_columns(conn, "findb", table_name) if in_final else []
    initial_cols = _schema_columns(conn, "initdb", table_name) if in_initial else []
    if not (final_cols or initial_cols):
        result["error"] = f"Could not get schema for table {table_name}"
        return result

    qt = _q(table_name)

    # Table present on only one side -> every row added / deleted.
    if in_final and not in_initial:
        rows, total, trunc = _fetch_capped(conn, f"SELECT * FROM findb.{qt}")
        result["rows_added"] = [_dict_from_row(final_cols, r) for r in rows]
        result["counts"]["added"] = total
        result["truncated"] = trunc
        return result
    if in_initial and not in_final:
        rows, total, trunc = _fetch_capped(conn, f"SELECT * FROM initdb.{qt}")
        result["rows_deleted"] = [_dict_from_row(initial_cols, r) for r in rows]
        result["counts"]["deleted"] = total
        result["truncated"] = trunc
        return result

    # Surface schema drift. A column present on only one side has no
    # before/after pair to value-compare, so per-row "modified" detection below
    # can only run over the column intersection. Record the drift explicitly so
    # it is visible to the judge rather than silently dropped.
    added_columns = [c for c in final_cols if c not in set(initial_cols)]
    removed_columns = [c for c in initial_cols if c not in set(final_cols)]
    if added_columns or removed_columns:
        result["schema_changed"] = {
            "added_columns": added_columns,
            "removed_columns": removed_columns,
        }

    # Present on both sides — key on the declared PK (stable across the
    # snapshot re-export; rowids are NOT stable, so never key on them).
    pk_cols = _schema_pk(conn, "findb", table_name) or _schema_pk(
        conn, "initdb", table_name
    )
    # A NULL in a PK column makes the key non-unique/ambiguous (`NULL = NULL`
    # is false, and several NULL-PK rows are indistinguishable), so keyed
    # diffing is unsound. SQLite only permits this on legacy rowid tables; when
    # it occurs, fall back to the key-less full-row set diff, which is correct
    # without a usable key.
    if not pk_cols or _table_has_null_pk(conn, qt, pk_cols):
        return _diff_table_no_key(conn, qt, final_cols, initial_cols, result)

    qpk = [_q(c) for c in pk_cols]
    join_on = " AND ".join(f"a.{c} = b.{c}" for c in qpk)

    # 1. Added: key present in findb, absent in initdb (anti-join, PK index).
    rows, total, trunc = _fetch_capped(
        conn,
        f"SELECT a.* FROM findb.{qt} a "
        f"WHERE NOT EXISTS (SELECT 1 FROM initdb.{qt} b WHERE {join_on})",
    )
    result["rows_added"] = [_dict_from_row(final_cols, r) for r in rows]
    result["counts"]["added"] = total
    result["truncated"] = result["truncated"] or trunc

    # 2. Deleted: key present in initdb, absent in findb.
    rows, total, trunc = _fetch_capped(
        conn,
        f"SELECT b.* FROM initdb.{qt} b "
        f"WHERE NOT EXISTS (SELECT 1 FROM findb.{qt} a WHERE {join_on})",
    )
    result["rows_deleted"] = [_dict_from_row(initial_cols, r) for r in rows]
    result["counts"]["deleted"] = total
    result["truncated"] = result["truncated"] or trunc

    # 3. Modified: key in both, any shared non-PK column differs (null-safe
    #    `IS NOT`, which short-circuits and needs no large signature string).
    common = [c for c in final_cols if c in set(initial_cols)]
    compare_cols = [c for c in common if c not in set(pk_cols)]
    if compare_cols:
        diff_pred = " OR ".join(f"a.{_q(c)} IS NOT b.{_q(c)}" for c in compare_cols)
        n = len(common)
        sel = ", ".join(
            [f"a.{_q(c)} AS {_q(f'a_{i}')}" for i, c in enumerate(common)]
            + [f"b.{_q(c)} AS {_q(f'b_{i}')}" for i, c in enumerate(common)]
        )
        rows, total, trunc = _fetch_capped(
            conn,
            f"SELECT {sel} FROM findb.{qt} a "
            f"JOIN initdb.{qt} b ON {join_on} WHERE {diff_pred}",
        )
        for r in rows:
            result["rows_modified"].append(
                {
                    "after": _dict_from_row(common, r[:n]),
                    "before": _dict_from_row(common, r[n:]),
                }
            )
        result["counts"]["modified"] = total
        result["truncated"] = result["truncated"] or trunc

    return result


def _diff_sqlite_db(
    initial_conn: DbConnection | None,
    final_conn: DbConnection | None,
) -> tuple[dict[str, Any], dict[str, int], list[str]]:
    """Diff every table of one SQLite database (already extracted to temp files).

    Returns ``(db_result, totals, changed_table_names)`` where ``db_result`` is
    ``{"tables": {name: table_diff}}`` and ``totals`` has added/deleted/modified
    sums for the summary.
    """
    init_path = initial_conn.temp_path if initial_conn else None
    fin_path = final_conn.temp_path if final_conn else None

    db_result: dict[str, Any] = {"tables": {}}
    totals = {"added": 0, "deleted": 0, "modified": 0}
    changed: list[str] = []

    diff_conn = _open_attached(init_path, fin_path)
    try:
        init_tables = (
            set(_schema_table_names(diff_conn, "initdb")) if init_path else set()
        )
        fin_tables = set(_schema_table_names(diff_conn, "findb")) if fin_path else set()
        for table_name in sorted(init_tables | fin_tables):
            try:
                table_diff = _diff_table(
                    diff_conn,
                    table_name,
                    table_name in init_tables,
                    table_name in fin_tables,
                )
            except sqlite3.Error as e:
                # Isolate per-table failures (e.g. an unreadable/virtual table)
                # so one bad table doesn't abort the whole database diff.
                logger.warning(f"Error diffing table {table_name}: {e}")
                table_diff = _empty_table_diff()
                table_diff["error"] = str(e)
            db_result["tables"][table_name] = table_diff
            # A table that errored (incl. a partial key-less EXCEPT failure) may
            # carry stale partial counts; don't fold those into the header
            # totals or changed list, or the totals would disagree with the
            # per-table breakdown. The error itself is surfaced separately.
            if table_diff.get("error"):
                continue
            counts = table_diff["counts"]
            totals["added"] += counts["added"]
            totals["deleted"] += counts["deleted"]
            totals["modified"] += counts["modified"]
            if (
                counts["added"]
                or counts["deleted"]
                or counts["modified"]
                or table_diff.get("schema_changed")
            ):
                changed.append(table_name)
    finally:
        diff_conn.close()

    return db_result, totals, changed


def diff_sqlite_artifact(
    initial_db: IO[bytes] | None, final_db: IO[bytes] | None
) -> dict[str, Any] | None:
    """Diff a single SQLite ``.db`` between its two snapshot versions.

    File-level entry point: callers pass the two versions of one database as
    byte streams (``io.BytesIO(blob)``), or ``None`` for a created/deleted file.

    Memory-bounded: the diff runs inside SQLite over the PK index with peak
    memory capped by the page cache (``DB_DIFF_CACHE_KIB``), independent of file
    size, so multi-GB ``.db`` files are paged from disk rather than loaded into
    RAM. Shares the same core (``_diff_sqlite_db``) that the snapshot-level
    ``db_diff_helper`` uses; intended for per-file callers such as the
    ``snapshot_diff`` content-diff path.

    Returns ``{"tables": {name: table_diff}, "summary": {...}}`` (a valid SQLite
    pair with no changes yields zero counts, not None). Returns ``None`` when a
    side that was provided couldn't be opened as SQLite (a non-SQLite payload
    such as an H2 ``.mv.db``, including the case where only one side opens), or
    both sides are absent, so the caller can fall back instead of emitting a
    misleading diff.
    """
    # Open both inside the try (pre-set to None) so that if building the second
    # connection raises, the finally still cleans up the first.
    initial_conn: DbConnection | None = None
    final_conn: DbConnection | None = None
    try:
        if initial_db is not None:
            initial_conn = _db_connection_from_stream(initial_db)
        if final_db is not None:
            final_conn = _db_connection_from_stream(final_db)
        # A side that was provided but didn't open as SQLite can't be faithfully
        # diffed: treating it as a missing DB would report every row as
        # added/deleted. Fall back instead (this covers the both-failed case too).
        if (initial_db is not None and initial_conn is None) or (
            final_db is not None and final_conn is None
        ):
            return None
        if not (initial_conn or final_conn):
            return None
        db_result, totals, changed = _diff_sqlite_db(initial_conn, final_conn)
        return {
            "tables": db_result["tables"],
            "summary": {
                "rows_added": totals["added"],
                "rows_deleted": totals["deleted"],
                "rows_modified": totals["modified"],
                "tables_changed": changed,
            },
        }
    finally:
        if initial_conn:
            initial_conn.close()
        if final_conn:
            final_conn.close()


# Rows inserted per executemany when loading a CSV into SQLite — bounds peak
# memory to a batch rather than the whole file.
_CSV_INSERT_BATCH = 50_000

# Bytes sniffed to pick a decode encoding. Bounded because the file may be
# multi-GB; the chosen codec is still read with errors="replace".
_CSV_ENCODING_PROBE_BYTES = 1 << 20  # 1 MiB


def _detect_csv_encoding(src: IO[bytes]) -> str:
    """Pick a decode encoding for a CSV stream, mirroring the small-CSV text
    path (``local_extractor._extract_csv``) so an artifact doesn't decode
    differently — or show a spurious BOM/encoding change — just because it
    crossed the size cap onto this row-level path.

    Sniffs a bounded prefix then rewinds (needs a seekable stream; defaults to
    ``utf-8-sig`` otherwise, which still strips a BOM and reads plain UTF-8):
    ``utf-8-sig`` when a BOM is present, ``utf-8`` when the prefix decodes
    cleanly (tolerating one multi-byte char truncated at the probe boundary),
    else ``latin-1`` — which, like the small path, accepts any byte sequence.
    """
    if not src.seekable():
        return "utf-8-sig"
    probe = src.read(_CSV_ENCODING_PROBE_BYTES)
    src.seek(0)
    if probe.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    # Only when we actually hit the probe limit might the tail be a multi-byte
    # char truncated mid-sequence (not a real decode failure). For a whole small
    # file read in full, a trailing undecodable byte is a genuine non-UTF-8 signal.
    truncated = len(probe) == _CSV_ENCODING_PROBE_BYTES
    try:
        probe.decode("utf-8")
    except UnicodeDecodeError as e:
        if not (truncated and e.start >= len(probe) - 3):
            return "latin-1"
    return "utf-8"


def _csv_stream_to_db_connection(
    src: IO[bytes], table_name: str = "data"
) -> DbConnection | None:
    """Stream a CSV into a one-table temp SQLite file (all columns TEXT).

    The header row defines the columns; rows are inserted in batches so peak
    memory is a batch, not the whole file — multi-GB CSVs load bounded. Returns
    None for an empty/headerless CSV.
    """
    encoding = _detect_csv_encoding(src)
    text = io.TextIOWrapper(src, encoding=encoding, errors="replace", newline="")
    reader = csv.reader(text)
    try:
        header = next(reader)
    except StopIteration:
        return None

    # Build unique, non-empty column names (CSV headers can be blank/duplicated).
    cols: list[str] = []
    seen: dict[str, int] = {}
    for i, raw in enumerate(header):
        name = (raw or "").strip() or f"col{i}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        cols.append(name)

    temp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    temp_path = temp_file.name
    temp_file.close()
    conn = sqlite3.connect(temp_path)
    try:
        conn.execute(
            f"CREATE TABLE {_q(table_name)} ({', '.join(f'{_q(c)} TEXT' for c in cols)})"
        )
        placeholders = ", ".join("?" * len(cols))
        insert_sql = f"INSERT INTO {_q(table_name)} VALUES ({placeholders})"
        width = len(cols)
        batch: list[list[Any]] = []
        for row in reader:
            if len(row) != width:  # tolerate ragged rows
                row = (row + [None] * width)[:width]
            batch.append(row)
            if len(batch) >= _CSV_INSERT_BATCH:
                conn.executemany(insert_sql, batch)
                batch.clear()
        if batch:
            conn.executemany(insert_sql, batch)
        conn.commit()
    except Exception:
        # A malformed CSV (e.g. csv.Error mid-iteration) must not leak the temp
        # DB or its connection.
        conn.close()
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return DbConnection(conn=conn, temp_path=temp_path)


def diff_csv_artifact(
    initial_csv: IO[bytes] | None, final_csv: IO[bytes] | None
) -> dict[str, Any] | None:
    """Row-level diff of a single CSV between its two snapshot versions.

    File-level entry mirroring :func:`diff_sqlite_artifact`: each version is
    streamed into a temp SQLite table (bounded memory) and diffed via the same
    ``_diff_sqlite_db`` core. CSVs carry no declared primary key, so the
    key-less full-row set diff is used (rows added/removed by content). Returns
    ``{"tables": {name: table_diff}, "summary": {...}}``, or ``None`` when
    neither side could be parsed (empty/headerless both sides) so the caller can
    fall back.
    """
    # Open both inside the try (pre-set to None) so that if building the second
    # connection raises, the finally still cleans up the first.
    initial_conn: DbConnection | None = None
    final_conn: DbConnection | None = None
    try:
        if initial_csv is not None:
            initial_conn = _csv_stream_to_db_connection(initial_csv)
        if final_csv is not None:
            final_conn = _csv_stream_to_db_connection(final_csv)
        if not (initial_conn or final_conn):
            return None
        db_result, totals, changed = _diff_sqlite_db(initial_conn, final_conn)
        return {
            "tables": db_result["tables"],
            "summary": {
                "rows_added": totals["added"],
                "rows_deleted": totals["deleted"],
                "rows_modified": totals["modified"],
                "tables_changed": changed,
            },
        }
    finally:
        if initial_conn:
            initial_conn.close()
        if final_conn:
            final_conn.close()


# =============================================================================
# SQL Dump Parsing Functions (MySQL/MariaDB INSERT + PostgreSQL COPY)
# =============================================================================


def _find_sql_dump_path_in_snapshot(snapshot_bytes: IO[bytes]) -> str | None:
    """Find the best SQL dump *path* in a snapshot zip without extracting content.

    Same priority logic as :func:`_find_sql_dump_in_snapshot` (shallowest +
    largest), but only returns the zip entry path string — no decompression,
    no memory allocation for the dump content.
    """
    snapshot_bytes.seek(0)
    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            candidates: list[tuple[int, int, str]] = []
            for info in zf.infolist():
                basename = info.filename.rstrip("/").rsplit("/", 1)[-1]
                if basename.endswith("_dump.sql") or basename == "database_dump.sql":
                    depth = info.filename.count("/")
                    candidates.append((depth, -info.file_size, info.filename))
            if not candidates:
                return None
            candidates.sort()
            return candidates[0][2]
    except (zipfile.BadZipFile, KeyError, OSError):
        return None


def _canonicalize_value(v: Any) -> Any:
    """Coerce numeric strings to typed numbers so SQL-dump rows hash
    equivalently regardless of whether the source dump was COPY format
    (parser yields strings) or INSERT format (parser yields Python literals).

    Only coerces when the round-trip is lossless. Zero-padded forms ('01'),
    arbitrary string IDs ('SO-00001'), and special tokens ('nan', 'inf')
    are preserved as strings. The first-char pre-check on the int path
    avoids paying ValueError on every non-numeric column.
    """
    if not isinstance(v, str) or not v:
        return v
    c0 = v[0]
    if c0.isdigit() or (c0 == "-" and len(v) > 1 and v[1].isdigit()):
        try:
            i = int(v)
            if str(i) == v:
                return i
        except ValueError:
            pass
    if "." in v and not v.startswith(".") and not v.endswith("."):
        try:
            return float(v)
        except ValueError:
            pass
    return v


def _sql_row_hash(row: dict[str, Any]) -> str:
    """Create a hash for a SQL dump row dict for comparison."""
    sorted_items = sorted((k, _canonicalize_value(v)) for k, v in row.items())
    return hashlib.md5(str(sorted_items).encode()).hexdigest()


def _get_sql_row_key(row: dict[str, Any], pk_columns: list[str] | None = None) -> str:
    """
    Get a unique key for a row, using primary key if available.

    For SQL dumps without schema info, we use the first column as a heuristic PK
    (first column is often the ID), or fall back to full row hash.

    Note: ERPNext uses string IDs like 'SO-00001', so we accept any non-null
    value as a potential primary key, not just integers.
    """
    if pk_columns:
        return str(tuple(_canonicalize_value(row.get(col)) for col in pk_columns))

    # Heuristic: use first column as ID. Canonicalize so '1' and 1 produce
    # the same key across COPY-format and INSERT-format dumps (otherwise
    # the same row gets classified as added+deleted instead of modified
    # for any non-int PK whose str() repr differs across types).
    if row:
        first_key = next(iter(row.keys()), None)
        if first_key:
            val = row[first_key]
            if val is not None:
                return f"pk_{_canonicalize_value(val)}"

    return _sql_row_hash(row)


# -- Tuple-based variants for memory-efficient lookup storage ----------------


def _sql_row_hash_from_tuple(columns: tuple[str, ...], values: tuple[Any, ...]) -> str:
    """Hash a row stored as a ``(columns, values)`` pair."""
    sorted_items = sorted(
        (k, _canonicalize_value(v)) for k, v in zip(columns, values, strict=False)
    )
    return hashlib.md5(str(sorted_items).encode()).hexdigest()


def _get_sql_row_key_from_tuple(
    columns: tuple[str, ...], values: tuple[Any, ...]
) -> str:
    """Row key for a ``(columns, values)`` pair (mirrors ``_get_sql_row_key``)."""
    if columns and values:
        val = values[0]
        if val is not None:
            return f"pk_{_canonicalize_value(val)}"
    return _sql_row_hash_from_tuple(columns, values)


def _diff_sql_table_data(
    initial_rows: list[dict[str, Any]],
    final_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Diff table data between initial and final states for SQL dumps.

    Returns dict matching DB_DIFF output format:
    {
        "rows_added": [...],
        "rows_deleted": [...],
        "rows_modified": [{"before": {...}, "after": {...}}],
        "error": None
    }
    """
    result: dict[str, Any] = {
        "rows_added": [],
        "rows_deleted": [],
        "rows_modified": [],
        "error": None,
    }

    # Build lookup dicts by row key
    initial_by_key: dict[str, dict[str, Any]] = {}
    for row in initial_rows:
        key = _get_sql_row_key(row)
        initial_by_key[key] = row

    final_by_key: dict[str, dict[str, Any]] = {}
    for row in final_rows:
        key = _get_sql_row_key(row)
        final_by_key[key] = row

    initial_keys = set(initial_by_key.keys())
    final_keys = set(final_by_key.keys())

    # Added rows (in final but not initial)
    for key in final_keys - initial_keys:
        if len(result["rows_added"]) < MAX_ROWS_PER_TABLE:
            result["rows_added"].append(final_by_key[key])

    # Deleted rows (in initial but not final)
    for key in initial_keys - final_keys:
        if len(result["rows_deleted"]) < MAX_ROWS_PER_TABLE:
            result["rows_deleted"].append(initial_by_key[key])

    # Modified rows (same key but different values)
    for key in initial_keys & final_keys:
        initial_row = initial_by_key[key]
        final_row = final_by_key[key]
        if _sql_row_hash(initial_row) != _sql_row_hash(final_row):
            if len(result["rows_modified"]) < MAX_ROWS_PER_TABLE:
                result["rows_modified"].append(
                    {
                        "before": initial_row,
                        "after": final_row,
                    }
                )

    return result


class _DiskBackedDiffStore:
    """Disk-backed row storage for diffing large SQL dumps.

    Uses a temporary SQLite database to store rows from both initial (``i``)
    and final (``f``) snapshots.  Column values are stored natively (no JSON
    serialisation overhead).  The diff is computed via SQL JOINs on the row
    key, with output bounded by ``MAX_ROWS_PER_TABLE``.

    Memory usage is ~64 MB (SQLite page cache) regardless of dump size.
    """

    _BATCH_SIZE: int = 100_000

    def __init__(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=OFF")
        self._conn.execute("PRAGMA synchronous=OFF")
        self._conn.execute("PRAGMA cache_size=-65536")  # 64 MB
        self._conn.execute("PRAGMA temp_store=FILE")
        self._table_cols: dict[str, tuple[str, ...]] = {}
        self._pending: dict[str, list[list[Any]]] = {}
        self._pending_count = 0

    # -- write API -------------------------------------------------------------

    def _ensure_table(self, table_id: str, columns: tuple[str, ...]) -> None:
        if table_id in self._table_cols:
            return
        self._table_cols[table_id] = columns
        col_defs = ", ".join(f'"{c}"' for c in columns)
        self._conn.execute(
            f'CREATE TABLE "{table_id}" '
            f"(__dbd_rk TEXT PRIMARY KEY, __dbd_rh TEXT, {col_defs}) WITHOUT ROWID"
        )

    def add_row(
        self,
        phase: str,
        table_name: str,
        row_key: str,
        row_hash: str,
        columns: tuple[str, ...],
        values: tuple[Any, ...],
    ) -> None:
        table_id = f"{phase}:{table_name}"
        self._ensure_table(table_id, columns)
        self._pending.setdefault(table_id, []).append([row_key, row_hash, *values])
        self._pending_count += 1
        if self._pending_count >= self._BATCH_SIZE:
            self._flush()

    def _flush(self) -> None:
        for table_id, rows in self._pending.items():
            if not rows:
                continue
            n = len(self._table_cols[table_id]) + 2
            ph = ",".join(["?"] * n)
            self._conn.executemany(
                f'INSERT OR REPLACE INTO "{table_id}" VALUES ({ph})', rows
            )
        self._conn.commit()
        self._pending.clear()
        self._pending_count = 0

    # -- read API --------------------------------------------------------------

    def get_tables(self, phase: str) -> list[str]:
        self._flush()
        return [
            tid.split(":", 1)[1]
            for tid in self._table_cols
            if tid.startswith(f"{phase}:")
        ]

    def _data_select(self, table_id: str, alias: str = "") -> str:
        """Build SELECT clause for the data columns (excluding __dbd_rk, __dbd_rh)."""
        pfx = f"{alias}." if alias else ""
        return ", ".join(f'{pfx}"{c}"' for c in self._table_cols[table_id])

    def _row_to_dict(self, table_id: str, row: tuple[Any, ...]) -> dict[str, Any]:
        return dict(zip(self._table_cols[table_id], row, strict=False))

    def diff_table(self, table_name: str) -> dict[str, Any]:
        """Compute the diff for a single table between initial and final."""
        self._flush()
        i_tid = f"i:{table_name}"
        f_tid = f"f:{table_name}"
        has_i = i_tid in self._table_cols
        has_f = f_tid in self._table_cols

        result: dict[str, Any] = {
            "rows_added": [],
            "rows_deleted": [],
            "rows_modified": [],
            "error": None,
        }

        if not has_i and not has_f:
            return result

        limit = MAX_ROWS_PER_TABLE

        # --- Only final exists: all rows are added ---
        if has_f and not has_i:
            for row in self._conn.execute(
                f'SELECT {self._data_select(f_tid)} FROM "{f_tid}" LIMIT ?',
                (limit,),
            ):
                result["rows_added"].append(self._row_to_dict(f_tid, row))
            return result

        # --- Only initial exists: all rows are deleted ---
        if has_i and not has_f:
            for row in self._conn.execute(
                f'SELECT {self._data_select(i_tid)} FROM "{i_tid}" LIMIT ?',
                (limit,),
            ):
                result["rows_deleted"].append(self._row_to_dict(i_tid, row))
            return result

        # --- Both exist: diff via JOINs (PRIMARY KEY on __dbd_rk provides index) ---

        # Added: in final but not initial
        for row in self._conn.execute(
            f'SELECT {self._data_select(f_tid, "f")} FROM "{f_tid}" f '
            f'LEFT JOIN "{i_tid}" i ON i.__dbd_rk = f.__dbd_rk '
            f"WHERE i.__dbd_rk IS NULL LIMIT ?",
            (limit,),
        ):
            result["rows_added"].append(self._row_to_dict(f_tid, row))

        # Deleted: in initial but not final
        for row in self._conn.execute(
            f'SELECT {self._data_select(i_tid, "i")} FROM "{i_tid}" i '
            f'LEFT JOIN "{f_tid}" f ON f.__dbd_rk = i.__dbd_rk '
            f"WHERE f.__dbd_rk IS NULL LIMIT ?",
            (limit,),
        ):
            result["rows_deleted"].append(self._row_to_dict(i_tid, row))

        # Modified: in both but hash differs
        i_sel = self._data_select(i_tid, "i")
        f_sel = self._data_select(f_tid, "f")
        i_cols = self._table_cols[i_tid]
        f_cols = self._table_cols[f_tid]
        n_i = len(i_cols)
        for row in self._conn.execute(
            f'SELECT {i_sel}, {f_sel} FROM "{i_tid}" i '
            f'JOIN "{f_tid}" f ON f.__dbd_rk = i.__dbd_rk '
            f"WHERE i.__dbd_rh != f.__dbd_rh LIMIT ?",
            (limit,),
        ):
            result["rows_modified"].append(
                {
                    "before": dict(zip(i_cols, row[:n_i], strict=False)),
                    "after": dict(zip(f_cols, row[n_i:], strict=False)),
                }
            )

        return result

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()
        try:
            os.unlink(self._db_path)
        except OSError:
            pass


def _stream_dump_to_store(
    snapshot_bytes: IO[bytes],
    phase: str,
    store: _DiskBackedDiffStore,
    skip_tables: set[str] | None = None,
) -> None:
    """Stream a SQL dump from a snapshot zip into *store*."""
    dump_path = _find_sql_dump_path_in_snapshot(snapshot_bytes)
    if not dump_path:
        return
    logger.info(f"Phase ({phase}): Streaming SQL dump from zip: {dump_path}")
    snapshot_bytes.seek(0)
    _skip = skip_tables or set()
    # Track first-seen columns per table so all rows use a consistent order,
    # even if a later INSERT has different column ordering or count.
    table_columns: dict[str, tuple[str, ...]] = {}
    with zipfile.ZipFile(snapshot_bytes, "r") as zf:
        with zf.open(dump_path) as raw:
            stream = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
            for table_name, row in iter_sql_dump_from_stream(stream):
                if table_name in _skip:
                    continue
                if table_name not in table_columns:
                    table_columns[table_name] = tuple(row.keys())
                columns = table_columns[table_name]
                values = tuple(row.get(c) for c in columns)
                key = _get_sql_row_key_from_tuple(columns, values)
                row_hash = _sql_row_hash_from_tuple(columns, values)
                store.add_row(phase, table_name, key, row_hash, columns, values)


async def _diff_sql_dumps(
    initial_snapshot_bytes: IO[bytes],
    final_snapshot_bytes: IO[bytes],
) -> dict[str, Any]:
    """
    Parse and diff SQL dump files between initial and final snapshots.

    Uses a disk-backed SQLite store so memory is bounded to ~64 MB regardless
    of dump size.  Supports dumps up to 20+ GB.

    Args:
        initial_snapshot_bytes: Task snapshot (initial state after populate hook)
        final_snapshot_bytes: Trajectory snapshot (final state after agent)

    Returns:
        Dict with structure matching SQLite diff output
    """
    result: dict[str, Any] = {
        "databases": {},
        "summary": {
            "total_rows_added": 0,
            "total_rows_deleted": 0,
            "total_rows_modified": 0,
            "tables_changed": [],
            "databases_found": [],
        },
    }

    store = _DiskBackedDiffStore()
    try:
        # ---- Phase 1: stream initial SQL into disk-backed store ----
        _stream_dump_to_store(
            initial_snapshot_bytes,
            "i",
            store,
            skip_tables=SQL_DUMP_SYSTEM_TABLES,
        )

        # ---- Phase 2: stream final SQL into disk-backed store ----
        _stream_dump_to_store(
            final_snapshot_bytes,
            "f",
            store,
            skip_tables=SQL_DUMP_SYSTEM_TABLES,
        )

        i_tables = set(store.get_tables("i"))
        f_tables = set(store.get_tables("f"))

        if not i_tables and not f_tables:
            logger.warning("No SQL dump files found in either snapshot")
            return result

        logger.info(
            f"Parsed tables: initial={sorted(i_tables)}, final={sorted(f_tables)}"
        )

        # ---- Phase 3: diff via SQL joins ----
        db_path = "sql_dump"
        result["summary"]["databases_found"] = [db_path]
        all_tables = i_tables | f_tables
        db_result: dict[str, Any] = {"tables": {}}

        for table_name in sorted(all_tables):
            table_diff = store.diff_table(table_name)
            db_result["tables"][table_name] = table_diff

            added = len(table_diff.get("rows_added", []))
            deleted = len(table_diff.get("rows_deleted", []))
            modified = len(table_diff.get("rows_modified", []))

            result["summary"]["total_rows_added"] += added
            result["summary"]["total_rows_deleted"] += deleted
            result["summary"]["total_rows_modified"] += modified

            if added or deleted or modified:
                result["summary"]["tables_changed"].append(f"{db_path}:{table_name}")

        result["databases"][db_path] = db_result

        logger.info(
            f"SQL dump diff complete: "
            f"{result['summary']['total_rows_added']} added, "
            f"{result['summary']['total_rows_deleted']} deleted, "
            f"{result['summary']['total_rows_modified']} modified"
        )

    finally:
        store.close()

    return result


# =============================================================================
# JSON File Parsing Functions
# =============================================================================


def _find_json_files_in_snapshot(snapshot_bytes: IO[bytes]) -> dict[str, str]:
    """Find and return all valid JSON data files from a snapshot zip.

    Scans all entries for .json files, skips denylist filenames, validates
    that each file contains parseable JSON.

    Returns:
        Dict mapping zip path → JSON content string.
    """
    snapshot_bytes.seek(0)
    found: dict[str, str] = {}
    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if not info.filename.endswith(".json"):
                    continue
                basename = PurePosixPath(info.filename).name
                if basename in JSON_DENYLIST_FILENAMES:
                    continue
                try:
                    raw = zf.read(info.filename).decode("utf-8", errors="replace")
                    json.loads(raw)  # validate
                    found[info.filename] = raw
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        logger.warning(f"Failed to scan snapshot for JSON files: {e}")
    return found


def _get_json_row_key(row: dict[str, Any], json_id_field: str | None = None) -> str:
    """Get a unique key for a JSON row dict.

    Priority:
    1. Configured ``json_id_field`` if present in the row
    2. Heuristic scan of common id field names
    3. Fallback to full-row hash via ``_sql_row_hash``
    """
    if json_id_field and json_id_field in row and row[json_id_field] is not None:
        return f"pk_{row[json_id_field]}"

    for field in _JSON_ID_FIELD_HEURISTICS:
        if field in row and row[field] is not None:
            return f"pk_{row[field]}"

    return _sql_row_hash(row)


def _diff_json_table_data(
    initial_rows: list[dict[str, Any]],
    final_rows: list[dict[str, Any]],
    json_id_field: str | None = None,
) -> dict[str, Any]:
    """Diff table data between initial and final states for JSON arrays.

    Same output shape as ``_diff_sql_table_data``.
    """
    result: dict[str, Any] = {
        "rows_added": [],
        "rows_deleted": [],
        "rows_modified": [],
        "error": None,
    }

    initial_by_key: dict[str, dict[str, Any]] = {}
    for row in initial_rows:
        key = _get_json_row_key(row, json_id_field)
        initial_by_key[key] = row

    final_by_key: dict[str, dict[str, Any]] = {}
    for row in final_rows:
        key = _get_json_row_key(row, json_id_field)
        final_by_key[key] = row

    initial_keys = set(initial_by_key.keys())
    final_keys = set(final_by_key.keys())

    for key in final_keys - initial_keys:
        if len(result["rows_added"]) < MAX_ROWS_PER_TABLE:
            result["rows_added"].append(final_by_key[key])

    for key in initial_keys - final_keys:
        if len(result["rows_deleted"]) < MAX_ROWS_PER_TABLE:
            result["rows_deleted"].append(initial_by_key[key])

    for key in initial_keys & final_keys:
        initial_row = initial_by_key[key]
        final_row = final_by_key[key]
        if _sql_row_hash(initial_row) != _sql_row_hash(final_row):
            if len(result["rows_modified"]) < MAX_ROWS_PER_TABLE:
                result["rows_modified"].append(
                    {"before": initial_row, "after": final_row}
                )

    return result


def _parse_json_to_tables(
    content: str, filename: str
) -> dict[str, list[dict[str, Any]]]:
    """Parse JSON content into a dict of table-name → list-of-row-dicts.

    Handles three shapes:
    - Top-level list of dicts → single table named after the file stem
    - Top-level dict whose values are lists of dicts → each key is a table
    - Top-level dict (no list values) → single-row table named after file stem
    """
    data = json.loads(content)
    stem = PurePosixPath(filename).stem

    if isinstance(data, list):
        rows = [item for item in data if isinstance(item, dict)]
        return {stem: rows} if rows else {}

    if isinstance(data, dict):
        # Check if any values are list-of-dicts → treat those as tables
        tables: dict[str, list[dict[str, Any]]] = {}
        for key, value in data.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                tables[key] = [v for v in value if isinstance(v, dict)]
        if tables:
            return tables
        # Flat dict → single-row table
        return {stem: [data]}

    return {}


async def _diff_json_files(
    initial_snapshot_bytes: IO[bytes],
    final_snapshot_bytes: IO[bytes],
    json_id_field: str | None = None,
) -> dict[str, Any]:
    """Parse and diff JSON data files between initial and final snapshots.

    Produces output in the same format as the SQLite / SQL dump diffs so the
    same LLM eval can consume the result.
    """
    result: dict[str, Any] = {
        "databases": {},
        "summary": {
            "total_rows_added": 0,
            "total_rows_deleted": 0,
            "total_rows_modified": 0,
            "tables_changed": [],
            "databases_found": [],
        },
    }

    initial_files = _find_json_files_in_snapshot(initial_snapshot_bytes)
    final_files = _find_json_files_in_snapshot(final_snapshot_bytes)

    if not initial_files and not final_files:
        logger.warning("No JSON data files found in either snapshot")
        return result

    logger.info(
        f"Found JSON files: initial={list(initial_files.keys())}, "
        f"final={list(final_files.keys())}"
    )

    db_path = "json"
    result["summary"]["databases_found"] = [db_path]

    # Merge tables from all JSON files in both snapshots
    initial_tables: dict[str, list[dict[str, Any]]] = {}
    for path, content in initial_files.items():
        for table_name, rows in _parse_json_to_tables(content, path).items():
            initial_tables.setdefault(table_name, []).extend(rows)

    final_tables: dict[str, list[dict[str, Any]]] = {}
    for path, content in final_files.items():
        for table_name, rows in _parse_json_to_tables(content, path).items():
            final_tables.setdefault(table_name, []).extend(rows)

    all_tables = set(initial_tables.keys()) | set(final_tables.keys())

    db_result: dict[str, Any] = {"tables": {}}

    for table_name in sorted(all_tables):
        initial_rows = initial_tables.get(table_name, [])
        final_rows = final_tables.get(table_name, [])

        table_diff = _diff_json_table_data(initial_rows, final_rows, json_id_field)
        db_result["tables"][table_name] = table_diff

        added = len(table_diff.get("rows_added", []))
        deleted = len(table_diff.get("rows_deleted", []))
        modified = len(table_diff.get("rows_modified", []))

        result["summary"]["total_rows_added"] += added
        result["summary"]["total_rows_deleted"] += deleted
        result["summary"]["total_rows_modified"] += modified

        if added or deleted or modified:
            result["summary"]["tables_changed"].append(f"{db_path}:{table_name}")

    result["databases"][db_path] = db_result

    logger.info(
        f"JSON diff complete: "
        f"{result['summary']['total_rows_added']} added, "
        f"{result['summary']['total_rows_deleted']} deleted, "
        f"{result['summary']['total_rows_modified']} modified"
    )

    return result


async def db_diff_helper(
    initial_snapshot_bytes: IO[bytes],
    final_snapshot_bytes: IO[bytes],
    trajectory: AgentTrajectoryOutput,  # unused but required by helper interface
    json_id_field: str | None = None,
    diff_all_types: bool = False,
) -> dict[str, Any]:
    """
    Extract and diff databases between initial and final snapshots.

    Auto-detects database type:
    - If diff_all_types=False (default): Priority-based fallback
      1. First tries to find SQLite .db files
      2. If none found, tries to find MySQL/MariaDB/PostgreSQL SQL dump files
      3. If neither found, tries to find JSON data files
      4. If nothing found, returns empty result

    - If diff_all_types=True: Diffs ALL data sources in parallel
      1. Finds and diffs SQLite .db files
      2. Finds and diffs SQL dump files
      3. Finds and diffs JSON data files
      4. Merges all results into a single response

    Compares all databases found and produces a structured diff showing
    rows added, deleted, and modified per table.

    Args:
        initial_snapshot_bytes: Task snapshot (initial state after populate hook)
        final_snapshot_bytes: Trajectory snapshot (final state after agent)
        trajectory: Agent trajectory output (for metadata)
        json_id_field: Optional field name to use as row identity when diffing
            JSON arrays (e.g. 'course_name'). When None, auto-detects via
            heuristic scan of common id field names.
        diff_all_types: If True, diffs all data source types (SQLite, SQL dumps,
            JSON) in parallel. If False, uses priority-based fallback (default).

    Returns:
        Dict with structure:
        {
            "databases": {
                "<db_path>": {
                    "tables": {
                        "<table_name>": {
                            "rows_added": [...],
                            "rows_deleted": [...],
                            "rows_modified": [{"before": {...}, "after": {...}}],
                        }
                    }
                }
            },
            "summary": {
                "total_rows_added": N,
                "total_rows_deleted": N,
                "total_rows_modified": N,
                "tables_changed": ["table1", "table2"],
                "databases_found": ["db1.db", "db2.db"],
            }
        }
    """
    result: dict[str, Any] = {
        "databases": {},
        "summary": {
            "total_rows_added": 0,
            "total_rows_deleted": 0,
            "total_rows_modified": 0,
            "tables_changed": [],
            "databases_found": [],
        },
    }

    if diff_all_types:
        # NEW: Parallel mode - diff all data source types and merge results
        logger.info("Scanning all data source types in parallel...")

        # Scan for all types
        initial_dbs = set(_find_all_dbs_in_snapshot(initial_snapshot_bytes))
        final_dbs = set(_find_all_dbs_in_snapshot(final_snapshot_bytes))
        all_dbs = initial_dbs | final_dbs

        has_sql_dump = bool(
            _find_sql_dump_path_in_snapshot(initial_snapshot_bytes)
            or _find_sql_dump_path_in_snapshot(final_snapshot_bytes)
        )

        has_json = bool(
            _find_json_files_in_snapshot(initial_snapshot_bytes)
            or _find_json_files_in_snapshot(final_snapshot_bytes)
        )

        logger.info(
            f"Found data sources: SQLite DBs={len(all_dbs)}, "
            f"SQL dumps={has_sql_dump}, JSON files={has_json}"
        )

        # Diff SQLite databases if found
        if all_dbs:
            logger.info(f"Diffing {len(all_dbs)} SQLite database(s)...")
            for db_path in sorted(all_dbs):
                initial_conn = _extract_db_from_snapshot(
                    initial_snapshot_bytes, db_path
                )
                final_conn = _extract_db_from_snapshot(final_snapshot_bytes, db_path)

                try:
                    # Skip non-SQLite .db files (both sides None) so they aren't
                    # reported as empty, change-free databases.
                    if initial_conn or final_conn:
                        db_result, totals, changed = _diff_sqlite_db(
                            initial_conn, final_conn
                        )
                        result["databases"][db_path] = db_result
                        result["summary"]["total_rows_added"] += totals["added"]
                        result["summary"]["total_rows_deleted"] += totals["deleted"]
                        result["summary"]["total_rows_modified"] += totals["modified"]
                        result["summary"]["tables_changed"].extend(
                            f"{db_path}:{t}" for t in changed
                        )
                        result["summary"]["databases_found"].append(db_path)
                finally:
                    if initial_conn:
                        initial_conn.close()
                    if final_conn:
                        final_conn.close()

        # Diff SQL dumps if found
        if has_sql_dump:
            logger.info("Diffing SQL dump files...")
            sql_result = await _diff_sql_dumps(
                initial_snapshot_bytes, final_snapshot_bytes
            )
            # Merge SQL dump results
            for db_name, db_data in sql_result.get("databases", {}).items():
                result["databases"][db_name] = db_data
            result["summary"]["total_rows_added"] += sql_result["summary"][
                "total_rows_added"
            ]
            result["summary"]["total_rows_deleted"] += sql_result["summary"][
                "total_rows_deleted"
            ]
            result["summary"]["total_rows_modified"] += sql_result["summary"][
                "total_rows_modified"
            ]
            result["summary"]["tables_changed"].extend(
                sql_result["summary"]["tables_changed"]
            )
            result["summary"]["databases_found"].extend(
                sql_result["summary"]["databases_found"]
            )

        # Diff JSON files if found
        if has_json:
            logger.info("Diffing JSON data files...")
            json_result = await _diff_json_files(
                initial_snapshot_bytes, final_snapshot_bytes, json_id_field
            )
            # Merge JSON results
            for db_name, db_data in json_result.get("databases", {}).items():
                result["databases"][db_name] = db_data
            result["summary"]["total_rows_added"] += json_result["summary"][
                "total_rows_added"
            ]
            result["summary"]["total_rows_deleted"] += json_result["summary"][
                "total_rows_deleted"
            ]
            result["summary"]["total_rows_modified"] += json_result["summary"][
                "total_rows_modified"
            ]
            result["summary"]["tables_changed"].extend(
                json_result["summary"]["tables_changed"]
            )
            result["summary"]["databases_found"].extend(
                json_result["summary"]["databases_found"]
            )

        logger.info(
            f"Parallel diff complete: "
            f"{result['summary']['total_rows_added']} added, "
            f"{result['summary']['total_rows_deleted']} deleted, "
            f"{result['summary']['total_rows_modified']} modified across all sources"
        )

        return result

    # ORIGINAL: Priority-based fallback mode (default behavior)

    # Try SQLite .db files first
    initial_dbs = set(_find_all_dbs_in_snapshot(initial_snapshot_bytes))
    final_dbs = set(_find_all_dbs_in_snapshot(final_snapshot_bytes))
    all_dbs = initial_dbs | final_dbs

    # .db is only a filename hint — a candidate may be a non-SQLite file (H2,
    # XML) that _extract_db_from_snapshot skips. Track whether any candidate
    # actually opened as SQLite; if none did, fall through to the SQL dump /
    # JSON fallback below instead of returning an empty result.
    opened_any_sqlite = False
    if all_dbs:
        logger.info(f"Found {len(all_dbs)} candidate .db file(s) to diff: {all_dbs}")
        for db_path in sorted(all_dbs):
            logger.info(f"Diffing database: {db_path}")

            initial_conn = _extract_db_from_snapshot(initial_snapshot_bytes, db_path)
            final_conn = _extract_db_from_snapshot(final_snapshot_bytes, db_path)

            try:
                # Skip non-SQLite .db files (both sides None) so they aren't
                # reported as empty, change-free databases.
                if initial_conn or final_conn:
                    opened_any_sqlite = True
                    db_result, totals, changed = _diff_sqlite_db(
                        initial_conn, final_conn
                    )
                    result["databases"][db_path] = db_result
                    result["summary"]["total_rows_added"] += totals["added"]
                    result["summary"]["total_rows_deleted"] += totals["deleted"]
                    result["summary"]["total_rows_modified"] += totals["modified"]
                    result["summary"]["tables_changed"].extend(
                        f"{db_path}:{t}" for t in changed
                    )
                    result["summary"]["databases_found"].append(db_path)
            finally:
                # Clean up connections even if an exception occurs
                if initial_conn:
                    initial_conn.close()
                if final_conn:
                    final_conn.close()

    if opened_any_sqlite:
        logger.info(
            f"DB diff complete: "
            f"{result['summary']['total_rows_added']} added, "
            f"{result['summary']['total_rows_deleted']} deleted, "
            f"{result['summary']['total_rows_modified']} modified"
        )
        return result

    # No usable SQLite database (none found, or every .db candidate was a
    # non-SQLite file that was skipped) — fall back to SQL dump, then JSON.
    logger.info("No SQLite database opened; checking for SQL dump files...")
    sql_result = await _diff_sql_dumps(initial_snapshot_bytes, final_snapshot_bytes)
    if sql_result["databases"]:
        return sql_result
    logger.info("No SQL dumps found, checking for JSON files...")
    return await _diff_json_files(
        initial_snapshot_bytes, final_snapshot_bytes, json_id_field
    )
