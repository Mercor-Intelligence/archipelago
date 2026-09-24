"""Per-file database diff export: two loose files in, one downloadable zip out.

Grading materializes a diff for the judge and throws it away. This produces the
same diff for a human reader, as `summary.json` plus per-table CSVs, from the
two versions of ONE file rather than a whole snapshot pair.

Row caps come from the `DB_DIFF_MAX_ROW*` env vars read in `main`; a caller that
wants a complete export raises them in the container env.
"""

import csv
import io
import json
import sqlite3
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from loguru import logger

from runner.helpers.artifact_state.parsers.sql import iter_sql_dump_from_stream

from .main import (
    _SQLITE_MAGIC as SQLITE_MAGIC,
)
from .main import (
    MAX_JSON_FILE_BYTES,
    SQL_DUMP_SYSTEM_TABLES,
    DbConnection,
    _diff_json_table_data,
    _diff_sqlite_db,
    _DiskBackedDiffStore,
    _get_sql_row_key_from_tuple,
    _parse_json_to_tables,
    _sql_row_hash_from_tuple,
)

SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
DUMP_SUFFIXES = (".sql",)
JSON_SUFFIXES = (".json",)

CHANGE_KINDS = ("added", "deleted", "modified")


class UnsupportedDiffFile(ValueError):
    """The file's extension has no row-level differ."""


def is_diffable(file_path: str) -> bool:
    """True when `file_path` has a row-level differ (SQLite, SQL dump, JSON)."""
    suffix = PurePosixPath(file_path).suffix.lower()
    return suffix in SQLITE_SUFFIXES + DUMP_SUFFIXES + JSON_SUFFIXES


def _stream_local_dump_to_store(
    dump_path: Path, phase: str, store: _DiskBackedDiffStore
) -> None:
    """Stream a loose SQL dump into *store* under *phase* ("i" or "f")."""
    table_columns: dict[str, tuple[str, ...]] = {}
    with dump_path.open("rb") as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
        for table_name, row in iter_sql_dump_from_stream(stream):
            if table_name in SQL_DUMP_SYSTEM_TABLES:
                continue
            if table_name not in table_columns:
                table_columns[table_name] = tuple(row.keys())
            columns = table_columns[table_name]
            values = tuple(row.get(column) for column in columns)
            store.add_row(
                phase,
                table_name,
                _get_sql_row_key_from_tuple(columns, values),
                _sql_row_hash_from_tuple(columns, values),
                columns,
                values,
            )


def _diff_dump_pair(before: Path | None, after: Path | None) -> dict[str, Any]:
    store = _DiskBackedDiffStore()
    try:
        if before is not None:
            _stream_local_dump_to_store(before, "i", store)
        if after is not None:
            _stream_local_dump_to_store(after, "f", store)
        tables = sorted(set(store.get_tables("i")) | set(store.get_tables("f")))
        return {name: store.diff_table(name) for name in tables}
    finally:
        store.close()


def _read_json_tables(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if path is None:
        return {}
    # Same ceiling the snapshot JSON scan applies. `json.loads` holds the text
    # and the object graph at once, so an unbounded read is how a multi-GB
    # agent-authored file exhausts the worker instead of returning an answer.
    size = path.stat().st_size
    if size > MAX_JSON_FILE_BYTES:
        raise UnsupportedDiffFile(
            f"JSON file is {size:,} bytes, over the {MAX_JSON_FILE_BYTES:,} "
            "byte limit for a row-level diff"
        )
    content = path.read_text(encoding="utf-8", errors="replace")
    try:
        return _parse_json_to_tables(content, path.name)
    except json.JSONDecodeError as error:
        raise UnsupportedDiffFile(f"not parseable as JSON: {error}") from error


def _diff_json_pair(
    before: Path | None, after: Path | None, json_id_field: str | None
) -> dict[str, Any]:
    before_tables = _read_json_tables(before)
    after_tables = _read_json_tables(after)
    return {
        name: _diff_json_table_data(
            before_tables.get(name, []), after_tables.get(name, []), json_id_field
        )
        for name in sorted(set(before_tables) | set(after_tables))
    }


def _sqlite_connection(path: Path | None) -> DbConnection | None:
    """Open a downloaded .db in place, or None when absent / not SQLite.

    Opened where it was downloaded rather than copied to a temp file, so a
    `-wal` sidecar sitting beside it is replayed and a multi-GB database isn't
    duplicated on disk. That is also why this can't be `diff_sqlite_artifact`,
    which takes streams and copies each one.
    """
    if path is None:
        return None
    with path.open("rb") as handle:
        if handle.read(len(SQLITE_MAGIC)) != SQLITE_MAGIC:
            raise UnsupportedDiffFile("not a SQLite database")
    return DbConnection(conn=sqlite3.connect(path), temp_path=str(path))


def _diff_sqlite_pair(before: Path | None, after: Path | None) -> dict[str, Any]:
    before_conn = after_conn = None
    try:
        before_conn = _sqlite_connection(before)
        after_conn = _sqlite_connection(after)
        tables, _totals, _changed = _diff_sqlite_db(before_conn, after_conn)
        return tables["tables"]
    finally:
        # keep_file: the caller owns the downloads and cleans up the whole dir.
        for connection in (before_conn, after_conn):
            if connection is not None:
                connection.close(keep_file=True)


def diff_file_pair(
    before: Path | None,
    after: Path | None,
    file_path: str,
    json_id_field: str | None = None,
) -> dict[str, Any]:
    """Row-level diff of one file's two versions, keyed by table name.

    `before`/`after` are local paths; `None` means the file is absent on that
    side (created or deleted). `file_path` is the snapshot-relative path, used
    only to pick the differ. Raises `UnsupportedDiffFile` for an extension with
    no differ, or a payload that doesn't parse as its extension claims.
    """
    if before is None and after is None:
        raise UnsupportedDiffFile("file is absent on both sides")

    suffix = PurePosixPath(file_path).suffix.lower()
    if suffix in SQLITE_SUFFIXES:
        return _diff_sqlite_pair(before, after)
    if suffix in DUMP_SUFFIXES:
        return _diff_dump_pair(before, after)
    if suffix in JSON_SUFFIXES:
        return _diff_json_pair(before, after, json_id_field)
    raise UnsupportedDiffFile(f"no row-level differ for '{suffix}'")


def _row_fieldnames(rows: list[Any], kind: str) -> list[str]:
    """Column order for a CSV, unioned over rows (modified rows are pairs)."""
    seen: dict[str, None] = {}
    for row in rows:
        pair = (
            (row.get("before", {}), row.get("after", {}))
            if kind == "modified"
            else (row,)
        )
        for side in pair:
            if isinstance(side, dict):
                for column in side:
                    seen.setdefault(column, None)
    return list(seen)


def _write_rows_csv(
    zip_file: zipfile.ZipFile, name: str, rows: list[Any], kind: str
) -> None:
    """Write one change kind's rows as CSV. Modified rows get a change column."""
    fieldnames = _row_fieldnames(rows, kind)
    buffer = io.StringIO()
    header = (["_change"] if kind == "modified" else []) + fieldnames
    writer = csv.DictWriter(buffer, fieldnames=header, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        if kind == "modified":
            for side in ("before", "after"):
                values = row.get(side) or {}
                writer.writerow({"_change": side, **_stringify(values)})
        else:
            writer.writerow(_stringify(row))
    zip_file.writestr(name, buffer.getvalue())


def _stringify(row: dict[str, Any]) -> dict[str, Any]:
    """JSON-encode nested values so a cell never spills into extra columns."""
    return {
        key: json.dumps(value) if isinstance(value, (dict, list)) else value
        for key, value in row.items()
    }


def _safe_table_name(table: str) -> str:
    """Table name reduced to a safe path segment for the zip."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in table) or "table"


def write_diff_zip(
    tables: dict[str, Any], file_path: str, out_path: Path
) -> dict[str, int]:
    """Write `summary.json` + per-table CSVs to *out_path*. Returns the totals.

    Tables with no changes are omitted; a table that errored keeps its error in
    the summary so a partial export still says what it couldn't read.
    """
    totals: dict[str, int] = {kind: 0 for kind in CHANGE_KINDS}
    table_summaries: dict[str, Any] = {}
    used_names: dict[str, str] = {}

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for table, diff in sorted(tables.items()):
            counts = {
                kind: int(diff.get("counts", {}).get(kind, 0)) for kind in CHANGE_KINDS
            }
            error = diff.get("error")
            if (
                not any(counts.values())
                and not error
                and not diff.get("schema_changed")
            ):
                continue
            for kind in CHANGE_KINDS:
                totals[kind] += counts[kind]

            folder = _safe_table_name(table)
            # Two distinct table names can sanitize to the same segment; suffix
            # the later ones so neither CSV overwrites the other in the zip.
            if folder in used_names and used_names[folder] != table:
                folder = f"{folder}_{len(used_names)}"
            used_names[folder] = table

            written: dict[str, int] = {}
            for kind in CHANGE_KINDS:
                rows = diff.get(f"rows_{kind}") or []
                if rows:
                    _write_rows_csv(zip_file, f"tables/{folder}/{kind}.csv", rows, kind)
                    written[kind] = len(rows)

            table_summaries[table] = {
                "counts": counts,
                "rows_exported": written,
                "folder": folder,
                "truncated": bool(diff.get("truncated")),
                "no_stable_key": bool(diff.get("no_stable_key")),
                "schema_changed": diff.get("schema_changed"),
                "error": error,
            }

        summary = {
            "file_path": file_path,
            "totals": totals,
            "tables_changed": sorted(table_summaries),
            "tables": table_summaries,
        }
        zip_file.writestr("summary.json", json.dumps(summary, indent=2, default=str))

    logger.info(
        f"[DB_DIFF_EXPORT] {file_path}: {totals['added']} added, "
        f"{totals['deleted']} deleted, {totals['modified']} modified across "
        f"{len(table_summaries)} table(s)"
    )
    return totals
