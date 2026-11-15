"""Unified CSV export engine driven by snapshot configuration."""

import io
import json
import re
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import polars as pl
from loguru import logger
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.exc import NoSuchTableError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .config import ColumnMapping, EntityConfig, ImportDirectives, NestedDirective, SnapshotConfig
from .directives import get_key_normalizer
from .schema import get_table_schema
from .types import format_bool, format_date, format_decimal


def _write_csv_text(
    headers: list[str],
    rows: Iterable[Iterable[Any]],
    *,
    include_header: bool = True,
) -> str:
    """Build CSV text from ``headers`` + ``rows`` via polars.

    Used by every export site that previously called ``csv.writer``. The
    output bytes follow polars's default conventions with one targeted
    tweak:

    * line terminator is ``\\n`` (LF), matching polars's default and
      diverging from stdlib csv's ``\\r\\n`` — intentional Phase E
      behaviour shift; consumers whose snapshot canonicals were anchored
      against the stdlib output need to re-establish the canonical
      against this format.
    * **Empty strings are collapsed to ``None`` at write time** so polars
      emits them as bare empty (no quotes), matching the legacy
      ``csv.writer`` output. Without this, polars would emit ``""``
      (quoted empty) for empty-string cells and break input parity on
      round-trips that legitimately produce empty cells (e.g. a Tag
      column with no tags).

    Materialises ``rows`` into a list so polars can build a DataFrame;
    if the input is already a list (the common case), no extra
    allocation. Cells are stringified by stdlib ``str()`` so callers are
    responsible for any DB-value-aware formatting upstream (see
    :func:`_format_value`).
    """
    rows_list = [list(r) for r in rows]
    if not rows_list:
        if include_header:
            # Bare-header CSV — polars refuses an empty DataFrame, so emit
            # the line manually to match the legacy writer.writerow path.
            return ",".join(headers) + "\n" if headers else ""
        return ""
    # Polars accepts dict[str, list] in column-major form. Build column
    # vectors by zipping rows; missing trailing cells become ``None`` so
    # under-wide rows don't shift columns. Collapse empty strings to
    # ``None`` so polars writes them as bare empty cells (without this
    # they would emit as ``""``, breaking input parity).
    data: dict[str, list[Any]] = {h: [] for h in headers}
    for row in rows_list:
        for i, h in enumerate(headers):
            v = row[i] if i < len(row) else None
            data[h].append(None if v == "" else v)
    df = pl.DataFrame(data, schema={h: pl.Utf8 for h in headers}, strict=False)
    return df.write_csv(include_header=include_header)


def _format_value(value, col: ColumnMapping) -> str:
    """Format a database value for CSV output using column config.

    Uses ``col.default`` only when value is ``None``.  Falsy values like
    ``0``, ``False``, or ``""`` are formatted normally — they are NOT
    replaced with the default.
    """
    if value is None:
        return col.default if col.default is not None else ""
    fmt = col.format
    if fmt == "date":
        return format_date(value)
    if fmt == "decimal":
        return format_decimal(value)
    if fmt == "bool":
        return format_bool(value)
    return str(value)


def _write_json_text(
    headers: list[str],
    rows: Iterable[Iterable[Any]],
    *,
    include_header: bool = True,  # noqa: ARG001 - accepted for signature parity, not used
) -> str:
    """Build JSON text from ``headers`` + ``rows`` — the JSON peer of ``_write_csv_text``.

    Emits a top-level JSON array of objects. Each object maps a wire-side
    field name (the entry from ``headers``, i.e. ``ColumnMapping.csv``) to
    the value in that position of the row. Values are whatever
    ``_format_value`` produced upstream — string form for v1, symmetric
    with the CSV path and losslessly round-trippable through
    :func:`~mcp_middleware.csv_engine.readers.read_json`. Native-type
    export (int → ``int``, bool → ``bool``, ``None`` → ``null``) is a
    natural follow-up but out of scope here; keeping the string shape
    means a JSON-configured entity round-trips through the same directive
    transforms as its CSV twin without a code path split.

    ``include_header`` is accepted for signature parity with
    :func:`_write_csv_text` but has no analogue in JSON — the array
    schema is self-describing via the keys of each object. The parameter
    is silently ignored.
    """
    records = [{h: v for h, v in zip(headers, row, strict=False)} for row in rows]
    return json.dumps(records, ensure_ascii=False, indent=2) + "\n"


def _write_wire_text(
    headers: list[str],
    rows: Iterable[Iterable[Any]],
    fmt: str,
    *,
    include_header: bool = True,
) -> str:
    """Dispatch text emission by wire format.

    Callers that need format-aware emission (the three "config-driven
    entity export" sites: :func:`_export_entity`,
    :func:`export_snapshot_zip`, and :func:`snapshot_directory` via
    :func:`export_with_directives`) route through here. Callers that
    are CSV-only by contract (e.g. :func:`export_all_tables_zip` — no
    entity config, no ``sources`` map to consult) keep calling
    :func:`_write_csv_text` directly.

    Args:
        headers: Wire-side field names.
        rows: Iterable of value-iterables, one per record.
        fmt: Format token — ``"csv"`` or ``"json"``. Unknown formats
            fall back to CSV (matches how ``resolve_format``'s default
            handles unmapped extensions).
        include_header: Passed through to the CSV writer; ignored for
            JSON (see :func:`_write_json_text`).
    """
    if fmt == "json":
        return _write_json_text(headers, rows, include_header=include_header)
    # Fall through to CSV for "csv" and any unknown format — matches the
    # DEFAULT_FORMAT policy in the reader registry.
    return _write_csv_text(headers, rows, include_header=include_header)


async def _ensure_export_resolved(
    engine: AsyncEngine,
    entity: EntityConfig,
    conn: AsyncConnection | None = None,
) -> None:
    """Resolve empty query/columns from the DB schema (convention-over-config).

    Mutates ``entity.export`` in-place so resolution happens only once.
    Skipped when the entity already has an explicit query (e.g. JOINs).

    ``conn`` is forwarded to ``get_table_schema`` so callers inside a
    single-connection block (in-memory SQLite) introspect the same database.
    """
    export_cfg = entity.export
    if not export_cfg:
        return

    needs_query = not export_cfg.query
    needs_columns = not export_cfg.columns

    if not needs_query and not needs_columns:
        return

    schema = await get_table_schema(engine, entity.table, conn=conn)

    if needs_columns:
        # Auto-generate identity column mappings with inferred formats
        export_cfg.columns = [
            ColumnMapping(
                db=name,
                csv=name,
                format=schema.infer_format(name),
            )
            for name in schema.columns
        ]

    if needs_query:
        col_list = ", ".join(f'"{col.db}"' for col in export_cfg.columns)
        # Order by primary key for a deterministic, portable export.
        # ``rowid`` is SQLite-specific and breaks on PostgreSQL/MySQL, so
        # fall back to no ORDER BY when the table has no primary key.
        pk_cols = [name for name, col in schema.columns.items() if col.is_primary_key]
        order_clause = ""
        if pk_cols:
            order_clause = " ORDER BY " + ", ".join(f'"{c}"' for c in pk_cols)
        export_cfg.query = f'SELECT {col_list} FROM "{entity.table}"{order_clause}'

    logger.debug(
        f"Auto-resolved export for {entity.name}: "
        f"query={'auto' if needs_query else 'explicit'}, "
        f"columns={'auto' if needs_columns else 'explicit'}"
    )


def _is_missing_table_error(exc: BaseException) -> bool:
    """True when ``exc`` means an entity's export table doesn't exist in the DB.

    A snapshot may configure entities whose table was never scaffolded — e.g.
    metadata skip-markers (``data_quality_report``, ``summary``) that exist in
    ``snapshot_config.yaml`` only so full-world CSV import doesn't raise
    "unrecognized schema", plus real entities whose migrations haven't run in
    the DB being dumped. Since entities default to ``export: true``, the
    exporter auto-resolves an export for them and hits the absent table. Such
    an entity should be skipped (0 rows, no file), not abort the whole dump.

    Covers the two shapes the absence surfaces as:

    * ``NoSuchTableError`` — raised by ``get_table_schema``'s reflection when
      the export is auto-resolved (no explicit query/columns).
    * A DBAPI error from executing an explicit query against a missing table —
      SQLite ``OperationalError: no such table``; PostgreSQL/MySQL
      ``ProgrammingError: relation/table ... does not exist``. Matched on the
      driver message so it stays dialect-portable.
    """
    if isinstance(exc, NoSuchTableError):
        return True
    if isinstance(exc, (OperationalError, ProgrammingError)):
        msg = str(getattr(exc, "orig", exc)).lower()
        # Match *table*-scoped phrasing only. A bare "does not exist" substring
        # also catches column/function errors (Postgres: `column "x" does not
        # exist`), which would silently swallow a genuine query failure as a
        # missing table. Anchor on "table"/"relation" (or SQLite's dedicated
        # "no such table") so only real missing-table errors are skipped.
        return (
            "no such table" in msg
            or "undefined table" in msg  # Postgres SQLSTATE 42P01 name
            or ("relation" in msg and ("does not exist" in msg or "doesn't exist" in msg))
            or ("table" in msg and ("does not exist" in msg or "doesn't exist" in msg))
        )
    return False


async def _export_entity(
    engine: AsyncEngine,
    entity: EntityConfig,
    output_dir: Path,
    config: SnapshotConfig,
) -> int:
    """Export a single entity using its config, dispatching wire format by extension.

    Output filename is ``entity.files[0]`` (via :func:`_resolve_output_filename`),
    not ``entity.name + ".csv"``. This ensures that re-importing from the snapshot
    directory discovers the file under the same name the importer expects.

    **Wire format is inferred from the resolved filename's extension via
    :func:`~mcp_middleware.csv_engine.readers.resolve_format` against
    ``config.sources``.** An entity with ``files: [users.json]`` and
    ``sources: [{glob: "*.json", format: json}]`` writes ``users.json``
    with JSON content — same source of truth as the import side. Entities
    without a matching ``sources`` rule (or apps that don't declare
    ``sources`` at all) default to CSV via the reader registry's
    ``DEFAULT_FORMAT`` fallback, preserving backwards compatibility.

    Subdirectory handling:

    * If ``entity.files[0]`` already includes a path component (e.g.
      ``"gmail/emails.csv"``), the file is written to
      ``output_dir/gmail/emails.csv`` and the subdirectory is created if needed.
    * If the name is flat (e.g. ``"emails.csv"``) **and** the entity name follows
      the ``{service}-{type}`` convention (e.g. ``"gmail-messages"``) **and**
      ``output_dir/{service}/`` already exists, the file is written to that
      subdirectory instead of the root.  This handles combined multi-service
      trees (Gmail + Drive) where two entities share the same base filename
      (e.g. ``users.csv``).
    """
    from .readers import resolve_format

    if not entity.export:
        return 0

    out_name = _resolve_output_filename(entity)
    if out_name is None:
        logger.warning(
            f"export_snapshot: skipping {entity.name!r} — no output filename "
            "(entity has no files configured)"
        )
        return 0

    await _ensure_export_resolved(engine, entity)
    export_cfg = entity.export

    async with engine.connect() as conn:
        result = await conn.execute(text(export_cfg.query))
        rows = result.fetchall()

    if not rows:
        logger.info(f"No {entity.name} to export")
        return 0

    # Resolve the output path, honouring any path component in out_name and
    # the {service}-{type} subdirectory convention for flat filenames.
    out_path = Path(out_name)
    if out_path.parent != Path("."):
        # out_name already encodes a subdirectory (e.g. "gmail/emails.csv").
        output_path = output_dir / out_name
    else:
        # Flat filename — check for {service}-{type} subdirectory convention.
        parts = entity.name.split("-", 1)
        service_dir = output_dir / parts[0] if len(parts) == 2 else None
        if service_dir is not None and service_dir.is_dir():
            output_path = service_dir / out_name
            # Remove any stale root-level copy so the importer doesn't find
            # two files with the same name and import both.
            root_copy = output_dir / out_name
            if root_copy.exists():
                root_copy.unlink()
                logger.info(
                    f"export_snapshot: removed stale root {out_name!r} "
                    f"(now routed to {service_dir.name}/)"
                )
        else:
            output_path = output_dir / out_name

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wire_headers = [col.csv for col in export_cfg.columns]

    def _rows() -> Iterable[list[str]]:
        for row in rows:
            yield [_format_value(getattr(row, col.db, None), col) for col in export_cfg.columns]

    # Match import's format routing: basename globs match the bare filename,
    # path-aware globs (e.g. "gmail/*.json") match the relative path. Passing
    # the full path as ``filename`` would break basename globs like "*.json"
    # (``*`` never crosses "/"), silently falling back to CSV.
    fmt = resolve_format(Path(out_name).name, config.sources, rel_path=out_name)
    output_path.write_text(_write_wire_text(wire_headers, _rows(), fmt), encoding="utf-8")

    logger.success(f"Exported {len(rows)} {entity.name} rows to {output_path} ({fmt})")
    return len(rows)


async def export_snapshot(
    engine: AsyncEngine,
    config: SnapshotConfig,
    output_dir: Path,
    entities: list[str] | None = None,
) -> dict[str, int]:
    """Export all configured entities to CSV files.

    Args:
        engine: SQLAlchemy async engine
        config: Snapshot configuration
        output_dir: Directory to write CSV files
        entities: Optional list of entity names to export (default: all)

    Returns:
        Dict mapping entity name to row count exported
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, int] = {}

    for name, entity in config.entities.items():
        if entities and name not in entities:
            continue
        if not entity.export:
            continue
        # Per-entity isolation: one entity's failure must not abort the whole
        # dump. A missing table (unscaffolded metadata skip-marker, or an
        # entity whose migration hasn't run) is the common, benign case and
        # logs at warning; any other failure logs at error. Either way the
        # entity is skipped (omitted from results, no file written) and the
        # remaining entities still export.
        try:
            count = await _export_entity(engine, entity, output_dir, config)
        except Exception as e:
            if _is_missing_table_error(e):
                logger.warning(
                    f"export_snapshot: skipping {name!r} — table {entity.table!r} "
                    "does not exist (not scaffolded)"
                )
            else:
                logger.error(f"export_snapshot: skipping {name!r} — export failed: {e}")
            continue
        results[name] = count

    total = sum(results.values())
    logger.success(f"Export complete: {total} total rows across {len(results)} entities")
    return results


def _entity_to_csv_string(entity: EntityConfig, rows: list) -> str:
    """Write entity rows to CSV string using config column mappings.

    Preserved for backwards compatibility with existing CSV-only callers.
    Format-aware callers should use :func:`_entity_to_wire_string` and
    pass an explicit ``fmt`` resolved via
    :func:`~mcp_middleware.csv_engine.readers.resolve_format`.
    """
    return _entity_to_wire_string(entity, rows, "csv")


def _entity_to_csv_bytes(entity: EntityConfig, rows: list) -> bytes:
    """Write entity rows to CSV bytes — thin utf-8 wrapper over the string form."""
    return _entity_to_csv_string(entity, rows).encode("utf-8")


def _entity_to_wire_string(entity: EntityConfig, rows: list, fmt: str) -> str:
    """Write entity rows using config column mappings, dispatching by wire format.

    Args:
        entity: Entity whose ``export.columns`` provide the wire-side
            field names + value-formatting rules.
        rows: SQLAlchemy result rows (row-object with attribute access).
        fmt: Wire format token — ``"csv"`` or ``"json"``. See
            :func:`_write_wire_text` for the dispatch policy.
    """
    export_cfg = entity.export
    wire_headers = [col.csv for col in export_cfg.columns]
    return _write_wire_text(
        wire_headers,
        (
            [_format_value(getattr(row, col.db, None), col) for col in export_cfg.columns]
            for row in rows
        ),
        fmt,
    )


def _entity_to_wire_bytes(entity: EntityConfig, rows: list, fmt: str) -> bytes:
    """Write entity rows to bytes — thin utf-8 wrapper over :func:`_entity_to_wire_string`."""
    return _entity_to_wire_string(entity, rows, fmt).encode("utf-8")


async def export_snapshot_zip(
    engine: AsyncEngine,
    config: SnapshotConfig,
    entities: list[str] | None = None,
) -> bytes:
    """Export all configured entities to a ZIP, dispatching wire format per entity.

    Each entity's zip entry name and content format come from
    ``entity.files[0]`` via :func:`_resolve_output_filename` +
    :func:`~mcp_middleware.csv_engine.readers.resolve_format`. An entity
    with ``files: [users.json]`` and ``sources: [{glob: "*.json", format: json}]``
    lands as a ``users.json`` entry with JSON content. Entities without
    a matching source rule fall back to CSV.

    Uses a single connection for all operations to support in-memory SQLite
    databases where each connection sees a separate database.

    Args:
        engine: SQLAlchemy async engine
        config: Snapshot configuration
        entities: Optional list of entity names to export (default: all)

    Returns:
        Bytes of a ZIP file containing one file per entity, format-per-entity
        based on ``files[0]`` extension resolved against ``config.sources``.
    """
    from .readers import resolve_format

    zip_buffer = io.BytesIO()
    seen_zip_entries: dict[str, str] = {}  # zip entry name -> entity name

    async with engine.connect() as conn:
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, entity in config.entities.items():
                if entities and name not in entities:
                    continue
                if not entity.export:
                    continue

                try:
                    await _ensure_export_resolved(engine, entity, conn=conn)
                    result = await conn.execute(text(entity.export.query))
                    rows = result.fetchall()
                except Exception as e:
                    if _is_missing_table_error(e):
                        logger.warning(
                            f"export_snapshot_zip: skipping {name!r} — table "
                            f"{entity.table!r} does not exist (not scaffolded)"
                        )
                        continue
                    raise

                if not rows:
                    logger.info(f"No {name} to export")
                    continue

                out_name = _resolve_output_filename(entity)
                if out_name is None:
                    logger.warning(
                        f"export_snapshot_zip: skipping {name!r} — no output filename "
                        "(entity has no files configured)"
                    )
                    continue

                if out_name in seen_zip_entries:
                    logger.warning(
                        f"export_snapshot_zip: {name!r} and {seen_zip_entries[out_name]!r} "
                        f"both resolve to zip entry {out_name!r} — "
                        f"{seen_zip_entries[out_name]!r} will be overwritten. "
                        "Assign unique files[0] values to avoid silent data loss."
                    )
                seen_zip_entries[out_name] = name

                fmt = resolve_format(Path(out_name).name, config.sources, rel_path=out_name)
                wire_data = _entity_to_wire_bytes(entity, rows, fmt)
                zf.writestr(out_name, wire_data)
                logger.info(f"Exported {len(rows)} {name} rows ({fmt})")

    total_bytes = zip_buffer.tell()
    logger.success(f"Export ZIP complete: {total_bytes} bytes")
    return zip_buffer.getvalue()


async def export_all_tables_zip(
    engine: AsyncEngine,
    tables: list[str] | None = None,
) -> bytes:
    """Export all database tables to a ZIP of CSV files (no config needed).

    Generic fallback that does SELECT * from every table.
    Uses \\N for NULL values for consistency with pandas-based exports.

    Uses a single connection for all operations to support in-memory SQLite
    databases where each connection sees a separate database.

    Args:
        engine: SQLAlchemy async engine
        tables: Optional list of table names to export (default: all)

    Returns:
        Bytes of a ZIP file containing one CSV per table
    """
    zip_buffer = io.BytesIO()
    failed_tables: list[tuple[str, str]] = []

    async with engine.connect() as conn:
        table_names = await conn.run_sync(lambda sync_conn: sa_inspect(sync_conn).get_table_names())
        if tables:
            table_names = [t for t in table_names if t in tables]

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for table_name in table_names:
                try:
                    result = await conn.execute(text(f'SELECT * FROM "{table_name}"'))
                    columns = list(result.keys())
                    rows = result.fetchall()

                    csv_text = _write_csv_text(
                        columns,
                        (["\\N" if v is None else v for v in row] for row in rows),
                    )
                    zf.writestr(f"{table_name}.csv", csv_text)
                    logger.info(f"Exported table {table_name} with {len(rows)} rows")
                except Exception as e:
                    logger.error(f"Failed to export table {table_name}: {e}")
                    failed_tables.append((table_name, str(e)))

    if failed_tables:
        failed_info = ", ".join(f"{name}: {err}" for name, err in failed_tables)
        raise RuntimeError(f"Export incomplete - failed tables: {failed_info}")

    return zip_buffer.getvalue()


async def export_entity_csv(
    table_name: str,
    engine: AsyncEngine,
    config: SnapshotConfig | None = None,
    output_format: str = "csv",
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
    include_headers: bool = True,
) -> tuple[str | None, list[dict[str, Any]] | None, int]:
    """Export a single table to CSV or JSON.

    Dual-mode:
    - With config: uses entity's export query and column mappings/formatting
    - Without config: raw SELECT * with optional filters

    Args:
        table_name: Database table to export
        engine: SQLAlchemy async engine
        config: Optional snapshot configuration
        output_format: "csv" or "json"
        filters: Optional column=value filters (generic mode only)
        limit: Optional row limit
        include_headers: Include CSV headers (csv format only)

    Returns:
        Tuple of (csv_content, json_rows, row_count).
        csv_content is set for "csv" format, json_rows for "json".

    Raises:
        ValueError: If table doesn't exist or params are invalid
    """
    if output_format not in ("csv", "json"):
        raise ValueError(f"Invalid format '{output_format}': use 'csv' or 'json'")
    if limit is not None and (limit < 1 or limit > 10000):
        raise ValueError("Limit must be between 1 and 10000")

    # Check if we have config-driven export for this table
    entity = config.find_entity_by_table(table_name) if config else None

    if entity and entity.export:
        # Config-driven export with column mappings
        await _ensure_export_resolved(engine, entity)
        query = entity.export.query
        if limit:
            query += f" LIMIT {limit}"

        async with engine.connect() as conn:
            result = await conn.execute(text(query))
            rows = result.fetchall()

        if output_format == "json":
            json_rows = []
            for row in rows:
                record = {}
                for col in entity.export.columns:
                    value = getattr(row, col.db, None)
                    record[col.csv] = _format_value(value, col)
                json_rows.append(record)
            return None, json_rows, len(rows)
        else:
            csv_headers = [col.csv for col in entity.export.columns]
            csv_text = _write_csv_text(
                csv_headers,
                (
                    [
                        _format_value(getattr(row, col.db, None), col)
                        for col in entity.export.columns
                    ]
                    for row in rows
                ),
                include_header=include_headers,
            )
            return csv_text, None, len(rows)
    else:
        # Generic export — raw SELECT *
        # Validate table exists and filter columns
        async with engine.connect() as conn:
            existing_tables = await conn.run_sync(
                lambda sync_conn: set(sa_inspect(sync_conn).get_table_names())
            )
            if table_name not in existing_tables:
                raise ValueError(f"Table '{table_name}' does not exist")

            if filters:
                columns_info = await conn.run_sync(
                    lambda sync_conn: sa_inspect(sync_conn).get_columns(table_name)
                )
                column_names = {col["name"] for col in columns_info}
                for filter_col in filters:
                    if filter_col not in column_names:
                        raise ValueError(
                            f"Invalid filter column '{filter_col}' for table '{table_name}'. "
                            f"Available columns: {sorted(column_names)}"
                        )

        # Build query
        query = f'SELECT * FROM "{table_name}"'
        params: dict[str, Any] = {}
        if filters:
            where_clauses = []
            for col, val in filters.items():
                param_name = f"filter_{col}"
                where_clauses.append(f'"{col}" = :{param_name}')
                params[param_name] = val
            query += " WHERE " + " AND ".join(where_clauses)
        if limit:
            query += f" LIMIT {limit}"

        async with engine.connect() as conn:
            result = await conn.execute(text(query), params)
            columns = list(result.keys())
            rows = result.fetchall()

        if output_format == "json":
            json_rows = [dict(zip(columns, row)) for row in rows]
            return None, json_rows, len(rows)
        else:
            csv_text = _write_csv_text(
                list(columns), (list(row) for row in rows), include_header=include_headers
            )
            return csv_text, None, len(rows)


async def export_multi_csv(
    engine: AsyncEngine,
    config: SnapshotConfig | None = None,
    entities: list[str] | None = None,
    delimiter: str = "#",
) -> str:
    """Export multiple tables as section-delimited CSV text.

    Each section is introduced by a delimiter line (e.g. ``# accounts``)
    followed by the CSV content for that entity — analogous to files in
    a ZIP archive.

    Args:
        engine: SQLAlchemy async engine
        config: Optional snapshot configuration.  If provided, uses entity
            export queries and column mappings.  If ``None``, exports all
            tables with ``SELECT *``.
        entities: Optional list of entity/table names to include.
        delimiter: Section delimiter character (default ``#``).

    Returns:
        Section-delimited CSV text.  Empty tables are excluded.
    """
    parts: list[str] = []

    # Use a single connection for all queries to support in-memory SQLite
    # databases where each connection sees a separate database.
    async with engine.connect() as conn:
        if config:
            for name, entity in config.entities.items():
                if entities and name not in entities:
                    continue
                if not entity.export:
                    continue

                await _ensure_export_resolved(engine, entity, conn=conn)
                result = await conn.execute(text(entity.export.query))
                rows = result.fetchall()

                if not rows:
                    continue

                csv_text = _entity_to_csv_string(entity, rows)
                parts.append(f"{delimiter} {name}\n{csv_text}")
        else:
            table_names = await conn.run_sync(
                lambda sync_conn: sa_inspect(sync_conn).get_table_names()
            )

            for table_name in sorted(table_names):
                if entities and table_name not in entities:
                    continue

                result = await conn.execute(text(f'SELECT * FROM "{table_name}"'))
                columns = list(result.keys())
                rows = result.fetchall()

                if not rows:
                    continue

                # Use \N for NULL (consistent with export_all_tables_zip and
                # recognized by the generic import path) so NULLs are not
                # conflated with empty strings on round-trip.
                csv_text = _write_csv_text(
                    list(columns),
                    (["\\N" if v is None else v for v in row] for row in rows),
                )
                parts.append(f"{delimiter} {table_name}\n{csv_text}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Directive round-trip: reconstruct the wide CSV from a fan-out import
# ---------------------------------------------------------------------------


def _stringify(value: Any) -> str:
    """Render a DB value as a CSV cell ("" for NULL)."""
    if value is None:
        return ""
    return str(value)


def _classify_eav_keys(
    directives: ImportDirectives, parsed_json: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """Split EAV keys into pure-lookup (always ``{id, name}``) vs scalar.

    Mirrors the bespoke export: a key seen as both a lookup dict and a scalar is
    treated as scalar to preserve plain-string fidelity. Declared-``nested`` keys
    are excluded (handled by their own columns).
    """
    nested_keys = {ne.key for ne in directives.nested}
    lookup: set[str] = set()
    scalar: set[str] = set()
    for jdata in parsed_json:
        for key, val in jdata.items():
            if key in nested_keys:
                continue
            if isinstance(val, dict) and "id" in val and "name" in val:
                lookup.add(key)
            else:
                scalar.add(key)
    pure_lookup = lookup - scalar
    return sorted(pure_lookup), sorted(scalar | (lookup - pure_lookup))


def _derive_wide_columns(
    directives: ImportDirectives, parsed_json: list[dict[str, Any]]
) -> list[str]:
    """Deterministic wide-column order when none is declared.

    Order: id, extract sources, declared nested base/id pairs, multi-value
    columns, then EAV keys — pure-lookup keys as ``<key><id_suffix>`` + ``<key>``
    pairs (so ``nest_id_pairs`` re-import rebuilds them), then scalar keys. EAV
    headers are the normalized keys (original header text is lossy when derived).
    """
    cols: list[str] = []
    seen: set[str] = set()

    def add(col: str) -> None:
        if col not in seen:
            seen.add(col)
            cols.append(col)

    if directives.id_from:
        add(directives.id_from)
    for ex in directives.extract:
        for source in ex.fields.values():
            add(source)
    for ne in directives.nested:
        add(ne.base)
        add(ne.base + ne.id_suffix)
    for mv in directives.multi_value:
        add(mv.column)

    suffix = directives.json_collapse.id_suffix if directives.json_collapse else ".id"
    pure_lookup, scalar_keys = _classify_eav_keys(directives, parsed_json)
    for key in pure_lookup:
        add(f"{key}{suffix}")
        add(key)
    for key in scalar_keys:
        add(key)
    return cols


async def export_with_directives(
    engine: AsyncEngine,
    table: str,
    directives: ImportDirectives,
    *,
    wide_columns: list[str] | None = None,
    include_headers: bool = True,
    where_by_constants: bool = True,
    fmt: str = "csv",
) -> str:
    """Reconstruct the wide file from a directive fan-out (inverse of import).

    ``fmt`` selects the wire format for the reconstructed output — ``"csv"``
    (default, matches historic behaviour) or ``"json"`` (a JSON array of
    ``{header: value}`` records). All the reconstruction logic (extract,
    multi_value, nested, aliases, json_collapse, constants) is
    format-agnostic; only the terminal writer differs. Callers routing
    through :func:`snapshot_directory` derive the format from
    ``resolve_format(Path(out_name).name, config.sources, rel_path=out_name)``
    so the round-trip is symmetric with the import side.

    Inverts each directive:
      - ``id_from``       <- the record id column
      - ``extract``       <- JOIN the target table by FK; emit each source column
                             from the mapped target fields (e.g. Owner / Owner.id)
      - ``multi_value``   <- gather junction rows and re-join with the delimiter
      - ``nested`` / ``nest_id_pairs`` <- ``field_data[key]`` ``{id, name}`` ->
                             ``<base>`` / ``<base><id_suffix>``
      - ``aliases``       <- inverted (the EAV key is looked up via the same
                             normalize+alias mapping used on import)
      - ``json_collapse`` <- ``field_data[key]`` for the scalar remainder
      - ``constants``     <- ``WHERE col=value`` (when ``where_by_constants``):
                             the inverse of "set X=Y on every imported row" is
                             "emit only rows where X=Y", so two entities sharing
                             one table (e.g. Zoho's 5 modules -> ``records``
                             discriminated by ``module_api_name``) cleanly slice
                             into their own files.

    Constant/computed columns are import-only and are NOT emitted. Column order
    is ``wide_columns`` (arg) > ``directives.wide_columns`` > a deterministic
    derived order. Declare ``wide_columns`` for a byte-stable round-trip of the
    upstream header text (the normalizer is lossy, so derived EAV headers use the
    normalized keys).

    Rows are emitted in primary-key order (``directives.id_column``) for
    portable, deterministic output. SQLite returns insertion order by default,
    but PostgreSQL/MySQL do not — ordering here keeps the round-trip stable
    across backends.
    """
    normalizer = get_key_normalizer(directives.key_normalizer)
    aliases = directives.aliases
    jc = directives.json_collapse
    nest_pairs = bool(jc and jc.nest_id_pairs)
    suffix = jc.id_suffix if jc else ".id"

    def _key(col: str) -> str:
        norm = normalizer(col)
        return aliases.get(norm, norm)

    where_params: dict[str, Any] = {}
    where_sql = ""
    if where_by_constants and directives.constants:
        clauses: list[str] = []
        for i, (col, val) in enumerate(directives.constants.items()):
            pname = f"_const_{i}"
            clauses.append(f'"{col}" = :{pname}')
            where_params[pname] = val
        where_sql = " WHERE " + " AND ".join(clauses)

    order_sql = f' ORDER BY "{directives.id_column}"' if directives.id_column else ""

    async with engine.connect() as conn:
        result = await conn.execute(
            text(f'SELECT * FROM "{table}"{where_sql}{order_sql}'), where_params
        )
        record_rows = [dict(r) for r in result.mappings().all()]

        # Extract targets keyed by id (for FK -> source-column reconstruction).
        extract_lookups: list[dict[Any, dict[str, Any]]] = []
        for ex in directives.extract:
            res = await conn.execute(text(f'SELECT * FROM "{ex.into}"'))
            extract_lookups.append({row[ex.id_column]: dict(row) for row in res.mappings().all()})

        # Multi-value values grouped by FK, preserving row order.
        multi_lookups: list[dict[Any, list[Any]]] = []
        for mv in directives.multi_value:
            res = await conn.execute(text(f'SELECT * FROM "{mv.into}"'))
            grouped: dict[Any, list[Any]] = {}
            for row in res.mappings().all():
                grouped.setdefault(row[mv.fk], []).append(row[mv.value])
            multi_lookups.append(grouped)

    # Merge each record's JSON columns (nested object + EAV remainder live here).
    json_columns: set[str] = {ne.into for ne in directives.nested}
    if directives.json_collapse is not None:
        json_columns.add(directives.json_collapse.into)
    parsed_json: list[dict[str, Any]] = []
    for rec in record_rows:
        merged: dict[str, Any] = {}
        for col in json_columns:
            raw = rec.get(col)
            # Only skip true absence (NULL) and empty string. Falsy native
            # values like ``{}`` / ``0`` / ``False`` are well-formed JSON
            # data — an empty dict is a no-op ``merged.update({})`` rather
            # than a parse error, and skipping it via ``if not raw:`` would
            # incorrectly drop a deliberately-stored empty EAV blob.
            if raw is None or raw == "":
                continue
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict):
                merged.update(data)
        parsed_json.append(merged)

    columns = (
        wide_columns or directives.wide_columns or _derive_wide_columns(directives, parsed_json)
    )
    column_set = set(columns)

    # Classification maps for fast per-column reconstruction.
    extract_field_to_target: dict[str, tuple[int, str]] = {}
    for idx, ex in enumerate(directives.extract):
        for target_col, source_col in ex.fields.items():
            extract_field_to_target[source_col] = (idx, target_col)
    nested_map: dict[str, tuple[NestedDirective, bool]] = {}
    for ne in directives.nested:
        nested_map[ne.base] = (ne, False)
        nested_map[ne.base + ne.id_suffix] = (ne, True)
    multi_col_map: dict[str, int] = {
        mv.column: idx for idx, mv in enumerate(directives.multi_value)
    }

    def _lookup_obj(base: str) -> dict[str, Any] | None:
        obj = jdata.get(_key(base))
        return obj if isinstance(obj, dict) else None

    materialised_rows: list[list[str]] = []

    for rec, jdata in zip(record_rows, parsed_json):
        out: list[str] = []
        for col in columns:
            if directives.id_from is not None and col == directives.id_from:
                out.append(_stringify(rec.get(directives.id_column)))
            elif col in extract_field_to_target:
                idx, target_col = extract_field_to_target[col]
                fk_val = rec.get(directives.extract[idx].fk)
                target = extract_lookups[idx].get(fk_val) if fk_val is not None else None
                out.append(_stringify(target.get(target_col)) if target else "")
            elif col in nested_map:
                ne, is_id = nested_map[col]
                obj = jdata.get(ne.key)
                out.append(
                    _stringify(obj.get(ne.id_field if is_id else ne.name_field))
                    if isinstance(obj, dict)
                    else ""
                )
            elif col in multi_col_map:
                idx = multi_col_map[col]
                values = multi_lookups[idx].get(rec.get(directives.id_column), [])
                out.append(
                    directives.multi_value[idx].delimiter.join(_stringify(v) for v in values)
                )
            elif (
                nest_pairs
                and suffix
                and col.endswith(suffix)
                and (col[: -len(suffix)] in column_set)
            ):
                # Auto-nest pair, ".id" side -> {id}.
                obj = _lookup_obj(col[: -len(suffix)])
                out.append(_stringify(obj.get("id")) if obj else "")
            elif nest_pairs and suffix and (col + suffix) in column_set:
                # Auto-nest pair, name side -> {name}.
                obj = _lookup_obj(col)
                out.append(_stringify(obj.get("name")) if obj else "")
            else:
                value = jdata.get(_key(col))
                if isinstance(value, dict):
                    # Mixed-type lookup leaked into a scalar column -> emit name.
                    out.append(_stringify(value.get("name")))
                else:
                    out.append(_stringify(value))
        materialised_rows.append(out)

    return _write_wire_text(list(columns), materialised_rows, fmt, include_header=include_headers)


# ---------------------------------------------------------------------------
# Top-level snapshot facade: symmetric inverse of import_directory
# ---------------------------------------------------------------------------


_GLOB_CHARS = frozenset("*?[")


def _count_csv_data_rows(csv_text: str) -> int:
    """Count CSV data rows (header excluded), respecting quoted multi-line cells.

    A naive ``csv_text.count("\\n") - 1`` over-counts when any exported value
    contains literal newlines — common in EAV ``field_data`` JSON blobs or
    multi-line text columns. Parse with polars so the count matches the row
    count that ``import_directory`` will read from the same file.
    """
    if not csv_text:
        return 0
    try:
        df = pl.read_csv(io.StringIO(csv_text), n_rows=None, infer_schema_length=0)
    except pl.exceptions.NoDataError:
        return 0
    return df.height


def _count_wire_data_rows(wire_text: str, fmt: str) -> int:
    """Count records in wire-format output text.

    JSON: the top-level array's ``len``. CSV: :func:`_count_csv_data_rows`.
    Both counts match what ``import_directory`` will read back from the
    same text, so the ``row_count`` reported in :func:`snapshot_directory`
    is the round-trip-consistent number.
    """
    if not wire_text:
        return 0
    if fmt == "json":
        try:
            data = json.loads(wire_text)
        except json.JSONDecodeError:
            return 0
        return len(data) if isinstance(data, list) else 0
    return _count_csv_data_rows(wire_text)


def _strip_glob_wildcards(pattern: str) -> str:
    """Derive the simplest concrete filename from a glob pattern.

    * Bracket expressions ``[...]`` are removed entirely.
    * ``*`` and ``?`` are deleted in place.
    * Consecutive ``/`` characters (produced by ``**`` segments) are collapsed.
    * Leading/trailing ``/`` is stripped.

    Examples::

        "gdrive/**/files.csv"  →  "gdrive/files.csv"
        "leads_*.csv"          →  "leads_.csv"
        "leads_*.csv*"         →  "leads_.csv"
    """
    result = re.sub(r"\[[^\]]*\]", "", pattern)  # remove [abc] / [0-9] etc.
    result = result.replace("*", "").replace("?", "")
    result = re.sub(r"/+", "/", result)  # collapse // left by ** removal
    return result.strip("/")


def _resolve_output_filename(entity: EntityConfig) -> str | None:
    """Return a concrete output filename for ``entity``, or ``None`` to skip.

    Uses ``entity.files[0]``. If that pattern contains glob wildcards
    (``*`` / ``?`` / ``[...]``), they are stripped to produce the simplest
    filename the glob *would have* matched:

    * ``"gdrive/**/files.csv"``  →  ``"gdrive/files.csv"``
    * ``"leads_*.csv"``          →  ``"leads_.csv"``

    Returns ``None`` only when ``entity.files`` is empty or the stripped
    result is empty.
    """
    if not entity.files:
        return None
    candidate = entity.files[0]
    if any(ch in candidate for ch in _GLOB_CHARS):
        resolved = _strip_glob_wildcards(candidate)
        if not resolved:
            logger.warning(
                f"snapshot: entity {entity.name!r} has glob-only files[0]={candidate!r} "
                "that reduces to an empty filename — skipping."
            )
            return None
        logger.debug(
            f"snapshot: entity {entity.name!r} — stripped glob {candidate!r} → {resolved!r}"
        )
        return resolved
    return candidate


async def snapshot_directory(
    engine: AsyncEngine,
    config: SnapshotConfig,
    output_dir: Path,
    *,
    entities: list[str] | None = None,
) -> dict[str, int]:
    """Snapshot the DB back to a directory of files — inverse of ``import_directory``.

    For each entity in ``config`` that has both ``import_config.directives``
    and a usable ``files[0]`` (literal — see :func:`_resolve_output_filename`),
    calls :func:`export_with_directives` and writes the reconstructed wide CSV
    to ``output_dir / <filename>``. Entities without directives or without
    ``files`` (e.g. snapshot-metadata placeholders like ``data_quality_report``
    in the Zoho config) are skipped silently.

    The output is suitable as input for :func:`import_directory` (round-trip):
    the same ``snapshot_config.yaml`` populates and snapshots without changes.

    When multiple entities target the same table with different ``constants``
    (e.g. Zoho's 5 modules -> ``records`` keyed on ``module_api_name``),
    ``export_with_directives``' default ``where_by_constants=True`` filters
    each entity's slice — every record is emitted under exactly one filename.

    Args:
        engine: SQLAlchemy async engine.
        config: Snapshot configuration (same one used by ``import_directory``).
        output_dir: Directory to write output files. Created if missing.
        entities: Optional subset of entity names to emit. ``None`` = all.

    Returns:
        Mapping of entity name -> row count written. Skipped entities do not
        appear in the result.
    """
    from .readers import is_binary_reader, resolve_format

    output_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, int] = {}

    for name, entity in config.entities.items():
        if entities is not None and name not in entities:
            continue
        imp = entity.import_config
        directives = imp.directives if imp else None
        if directives is None:
            logger.debug(f"snapshot: skipping {name!r} (no import directives)")
            continue
        out_name = _resolve_output_filename(entity)
        if out_name is None:
            logger.debug(f"snapshot: skipping {name!r} (no usable filename)")
            continue

        # Binary-input entities (PDF / DOCX / images via the file_content
        # reader) cannot be reconstructed from DB rows: the original bytes
        # were not stored. Their round-trip is "keep the original source
        # files alongside the snapshot output and re-import them" — apps
        # handle that file copy themselves; snapshot stays a no-op here.
        fmt = resolve_format(Path(out_name).name, config.sources, rel_path=out_name)
        if is_binary_reader(fmt):
            logger.info(
                f"snapshot: skipping {name!r} (binary format {fmt!r}; "
                f"original source files are the source of truth)"
            )
            continue

        try:
            wire_text = await export_with_directives(engine, entity.table, directives, fmt=fmt)
        except Exception as e:
            if _is_missing_table_error(e):
                logger.warning(
                    f"snapshot: skipping {name!r} — table {entity.table!r} "
                    "does not exist (not scaffolded)"
                )
                continue
            raise

        # Count data rows in the emitted text. CSV: re-parse (handles
        # quoted multi-line cells — EAV ``field_data`` JSON blobs and
        # multi-line text fields routinely carry embedded newlines that
        # a naive ``count("\\n")`` would overcount). JSON: the top-level
        # array's length equals the record count.
        row_count = _count_wire_data_rows(wire_text, fmt)
        out_path = output_dir / out_name

        # If files[0] is a glob, remove all current matches before writing so
        # only the canonical output file remains.  This handles DBs that were
        # populated from multiple source files that all matched the same glob
        # (e.g. gdrive/**/files.csv matched across several sub-directories).
        # Run the cleanup unconditionally, even when row_count==0, so an empty
        # table always leaves a clean directory.
        original_pattern = entity.files[0]
        if any(ch in original_pattern for ch in _GLOB_CHARS):
            for stale in output_dir.glob(original_pattern):
                stale.unlink()
                logger.info(f"snapshot: removed stale {stale.relative_to(output_dir)!s}")

        if row_count == 0:
            logger.info(f"snapshot: skipping {name!r} (no rows to export)")
            continue

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(wire_text, encoding="utf-8")
        results[name] = row_count
        logger.info(f"snapshot: wrote {row_count} {name} row(s) to {out_path.name} ({fmt})")

    total = sum(results.values())
    logger.success(f"snapshot complete: {total} total row(s) across {len(results)} entit(ies)")
    return results
