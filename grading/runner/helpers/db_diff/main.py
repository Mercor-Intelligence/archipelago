"""DB Diff helper - extracts and diffs databases between snapshots.

Supports SQLite (.db) files, MySQL/MariaDB INSERT-format SQL dumps,
PostgreSQL COPY-format SQL dumps, and JSON data files.  Auto-detects the
database type and uses the appropriate parsing strategy.
"""

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from loguru import logger

from runner.helpers.artifact_state.parsers.sql import iter_sql_dump_from_stream
from runner.models import AgentTrajectoryOutput

# Maximum number of rows to include in diff output to avoid token limits
MAX_ROWS_PER_TABLE = 100

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


def _extract_db_from_snapshot(
    snapshot_bytes: io.BytesIO, db_path: str
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
            # Check for exact path first
            if db_path in zf.namelist():
                db_bytes = zf.read(db_path)
            else:
                # Try with leading slash stripped or added
                alt_path = db_path.lstrip("/")
                if alt_path in zf.namelist():
                    db_bytes = zf.read(alt_path)
                else:
                    alt_path = "/" + db_path.lstrip("/")
                    if alt_path in zf.namelist():
                        db_bytes = zf.read(alt_path)
                    else:
                        return None

            # Write to temp file (SQLite needs file path)
            temp_file = tempfile.NamedTemporaryFile(
                suffix=".db", delete=False, mode="wb"
            )
            temp_file.write(db_bytes)
            temp_file.flush()
            temp_path = temp_file.name
            temp_file.close()

            return DbConnection(
                conn=sqlite3.connect(temp_path),
                temp_path=temp_path,
            )
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        logger.warning(f"Failed to extract database {db_path}: {e}")
        return None


def _find_all_dbs_in_snapshot(snapshot_bytes: io.BytesIO) -> list[str]:
    """Find all .db files in a snapshot."""
    snapshot_bytes.seek(0)
    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            return [f for f in zf.namelist() if f.endswith(".db")]
    except zipfile.BadZipFile:
        return []


def _get_table_names(conn: sqlite3.Connection) -> list[str]:
    """Get all table names from a database."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return [row[0] for row in cursor.fetchall()]


def _get_table_schema(conn: sqlite3.Connection, table_name: str) -> list[str]:
    """Get column names for a table."""
    cursor = conn.execute(f'PRAGMA table_info("{table_name}")')
    return [row[1] for row in cursor.fetchall()]


def _get_primary_key(conn: sqlite3.Connection, table_name: str) -> list[str]:
    """Get primary key columns for a table."""
    cursor = conn.execute(f'PRAGMA table_info("{table_name}")')
    pk_cols = [row[1] for row in cursor.fetchall() if row[5] > 0]
    return pk_cols


def _row_hash(row: tuple[Any, ...]) -> str:
    """Create a hash for a row for comparison."""
    return hashlib.md5(str(row).encode()).hexdigest()


def _safe_value(v: Any) -> Any:
    """Convert a value to a JSON-serializable type."""
    if isinstance(v, (bytes, memoryview)):
        return bytes(v).decode("utf-8", errors="replace")
    return v


def _dict_from_row(columns: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
    """Convert a row tuple to a dict with column names."""
    return {col: _safe_value(val) for col, val in zip(columns, row, strict=False)}


def _diff_table(
    initial_conn: DbConnection | None,
    final_conn: DbConnection | None,
    table_name: str,
) -> dict[str, Any]:
    """
    Diff a single table between initial and final states.

    Returns dict with rows_added, rows_deleted, rows_modified.
    """
    result: dict[str, Any] = {
        "rows_added": [],
        "rows_deleted": [],
        "rows_modified": [],
        "error": None,
    }

    # Handle missing connections
    if initial_conn is None and final_conn is None:
        result["error"] = "Table not found in either snapshot"
        return result

    # Get schemas
    initial_cols: list[str] = []
    final_cols: list[str] = []

    if initial_conn:
        try:
            initial_cols = _get_table_schema(initial_conn.conn, table_name)
        except sqlite3.OperationalError:
            pass

    if final_conn:
        try:
            final_cols = _get_table_schema(final_conn.conn, table_name)
        except sqlite3.OperationalError:
            pass

    # Use final columns if available, otherwise initial
    columns = final_cols or initial_cols
    if not columns:
        result["error"] = f"Could not get schema for table {table_name}"
        return result

    # Get primary key for row matching
    pk_cols: list[str] = []
    if final_conn:
        pk_cols = _get_primary_key(final_conn.conn, table_name)
    elif initial_conn:
        pk_cols = _get_primary_key(initial_conn.conn, table_name)

    # Fetch rows
    initial_rows: dict[str, tuple[Any, ...]] = {}
    final_rows: dict[str, tuple[Any, ...]] = {}

    if initial_conn and initial_cols:
        try:
            cursor = initial_conn.conn.execute(
                f'SELECT * FROM "{table_name}" LIMIT {MAX_ROWS_PER_TABLE * 2}'
            )
            # Compute pk_indices once outside the loop
            pk_indices = (
                [initial_cols.index(c) for c in pk_cols if c in initial_cols]
                if pk_cols
                else []
            )
            for row in cursor.fetchall():
                if pk_indices:
                    # Use primary key as row identifier
                    key = str(tuple(row[i] for i in pk_indices))
                else:
                    # Use full row hash (note: duplicate rows will collapse to one entry)
                    key = _row_hash(row)
                initial_rows[key] = row
        except sqlite3.OperationalError as e:
            logger.warning(f"Error reading initial table {table_name}: {e}")

    if final_conn and final_cols:
        try:
            cursor = final_conn.conn.execute(
                f'SELECT * FROM "{table_name}" LIMIT {MAX_ROWS_PER_TABLE * 2}'
            )
            # Compute pk_indices once outside the loop
            pk_indices = (
                [final_cols.index(c) for c in pk_cols if c in final_cols]
                if pk_cols
                else []
            )
            for row in cursor.fetchall():
                if pk_indices:
                    key = str(tuple(row[i] for i in pk_indices))
                else:
                    # Use full row hash (note: duplicate rows will collapse to one entry)
                    key = _row_hash(row)
                final_rows[key] = row
        except sqlite3.OperationalError as e:
            logger.warning(f"Error reading final table {table_name}: {e}")

    # Compute diff
    initial_keys = set(initial_rows.keys())
    final_keys = set(final_rows.keys())

    # Added rows (in final but not initial)
    for key in final_keys - initial_keys:
        if len(result["rows_added"]) < MAX_ROWS_PER_TABLE:
            result["rows_added"].append(_dict_from_row(final_cols, final_rows[key]))

    # Deleted rows (in initial but not final)
    for key in initial_keys - final_keys:
        if len(result["rows_deleted"]) < MAX_ROWS_PER_TABLE:
            result["rows_deleted"].append(
                _dict_from_row(initial_cols, initial_rows[key])
            )

    # Modified rows (same key but different values)
    for key in initial_keys & final_keys:
        initial_row = initial_rows[key]
        final_row = final_rows[key]
        if _row_hash(initial_row) != _row_hash(final_row):
            if len(result["rows_modified"]) < MAX_ROWS_PER_TABLE:
                result["rows_modified"].append(
                    {
                        "before": _dict_from_row(initial_cols, initial_row),
                        "after": _dict_from_row(final_cols, final_row),
                    }
                )

    return result


# =============================================================================
# SQL Dump Parsing Functions (MySQL/MariaDB INSERT + PostgreSQL COPY)
# =============================================================================


def _find_sql_dump_path_in_snapshot(snapshot_bytes: io.BytesIO) -> str | None:
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
    snapshot_bytes: io.BytesIO,
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
    initial_snapshot_bytes: io.BytesIO,
    final_snapshot_bytes: io.BytesIO,
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


def _find_json_files_in_snapshot(snapshot_bytes: io.BytesIO) -> dict[str, str]:
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
    initial_snapshot_bytes: io.BytesIO,
    final_snapshot_bytes: io.BytesIO,
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
    initial_snapshot_bytes: io.BytesIO,
    final_snapshot_bytes: io.BytesIO,
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
                    db_result: dict[str, Any] = {"tables": {}}
                    initial_tables = (
                        set(_get_table_names(initial_conn.conn))
                        if initial_conn
                        else set()
                    )
                    final_tables = (
                        set(_get_table_names(final_conn.conn)) if final_conn else set()
                    )
                    all_tables = initial_tables | final_tables

                    for table_name in sorted(all_tables):
                        table_diff = _diff_table(initial_conn, final_conn, table_name)
                        db_result["tables"][table_name] = table_diff

                        added = len(table_diff.get("rows_added", []))
                        deleted = len(table_diff.get("rows_deleted", []))
                        modified = len(table_diff.get("rows_modified", []))

                        result["summary"]["total_rows_added"] += added
                        result["summary"]["total_rows_deleted"] += deleted
                        result["summary"]["total_rows_modified"] += modified

                        if added or deleted or modified:
                            result["summary"]["tables_changed"].append(
                                f"{db_path}:{table_name}"
                            )

                    result["databases"][db_path] = db_result
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

    if not all_dbs:
        # No SQLite databases found, try SQL dump files
        logger.info("No SQLite databases found, checking for SQL dump files...")
        sql_result = await _diff_sql_dumps(initial_snapshot_bytes, final_snapshot_bytes)
        if sql_result["databases"]:
            return sql_result
        # No SQL dumps found either, try JSON files
        logger.info("No SQL dumps found, checking for JSON files...")
        return await _diff_json_files(
            initial_snapshot_bytes, final_snapshot_bytes, json_id_field
        )

    # SQLite databases found - use SQLite parsing
    result["summary"]["databases_found"] = sorted(all_dbs)
    logger.info(f"Found {len(all_dbs)} SQLite database(s) to diff: {all_dbs}")

    for db_path in sorted(all_dbs):
        logger.info(f"Diffing database: {db_path}")

        initial_conn = _extract_db_from_snapshot(initial_snapshot_bytes, db_path)
        final_conn = _extract_db_from_snapshot(final_snapshot_bytes, db_path)

        try:
            db_result: dict[str, Any] = {"tables": {}}

            # Get all tables from both connections
            initial_tables = (
                set(_get_table_names(initial_conn.conn)) if initial_conn else set()
            )
            final_tables = (
                set(_get_table_names(final_conn.conn)) if final_conn else set()
            )
            all_tables = initial_tables | final_tables

            for table_name in sorted(all_tables):
                table_diff = _diff_table(initial_conn, final_conn, table_name)
                db_result["tables"][table_name] = table_diff

                # Update summary
                added = len(table_diff.get("rows_added", []))
                deleted = len(table_diff.get("rows_deleted", []))
                modified = len(table_diff.get("rows_modified", []))

                result["summary"]["total_rows_added"] += added
                result["summary"]["total_rows_deleted"] += deleted
                result["summary"]["total_rows_modified"] += modified

                if added or deleted or modified:
                    result["summary"]["tables_changed"].append(
                        f"{db_path}:{table_name}"
                    )

            result["databases"][db_path] = db_result
        finally:
            # Clean up connections even if an exception occurs
            if initial_conn:
                initial_conn.close()
            if final_conn:
                final_conn.close()

    logger.info(
        f"DB diff complete: "
        f"{result['summary']['total_rows_added']} added, "
        f"{result['summary']['total_rows_deleted']} deleted, "
        f"{result['summary']['total_rows_modified']} modified"
    )

    return result
