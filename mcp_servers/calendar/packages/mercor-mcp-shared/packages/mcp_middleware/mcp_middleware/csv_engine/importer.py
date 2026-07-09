"""Unified CSV import engine driven by snapshot configuration."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import tempfile
import zipfile
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import polars as pl
from loguru import logger
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .config import EntityConfig, ImportDirectives, SnapshotConfig
from .directives import directive_table_order, transform_with_directives
from .emit import Emit, EmitSet, resolve_emits
from .readers import (
    SourceInfo,
    _glob_match,
    get_header_reader,
    get_reader,
    is_binary_reader,
    is_multi_entity_reader,
    read_csv,
    read_csv_headers_path,
    read_csv_headers_text,
    resolve_format,
)
from .schema import get_table_schema
from .transforms.dispatch import apply_row_hooks_in_order as _apply_row_hooks_in_order
from .transforms.dispatch import apply_row_hooks_lazy as _apply_row_hooks_lazy
from .transforms.dispatch import apply_to_dataframe as _apply_transforms_to_dataframe
from .types import normalize_header, parse_decimal
from .validator import is_valid_csv_file

# Reserved key in :func:`discover_snapshot` output for files whose configured
# format is a multi-entity reader. The reader's own output keys are the entity
# routing for these files, so they bypass filename/header detection.
_MULTI_ENTITY_BUCKET = "__multi_entity__"

# Section delimiter: line starting with a non-alphanumeric, non-whitespace char
_SECTION_RE = re.compile(r"^[^a-zA-Z0-9\s]\s*(.+)$")


# Maximum rows to buffer per batch during streamed imports.  Keeps peak
# memory bounded: 10 000 rows × ~10–100 KB avg row ≈ 100 MB–1 GB.
_IMPORT_BATCH_SIZE = 10_000


class CSVSnapshotError(Exception):
    """Raised when CSV snapshot import fails."""


def _is_section_name(raw: str) -> bool:
    """Check if text looks like a table/entity name (not CSV data)."""
    stripped = raw.strip()
    # Must contain at least one letter and no commas (commas → CSV data)
    return bool(stripped) and any(c.isalpha() for c in stripped) and "," not in stripped


def parse_multi_csv(csv_text: str) -> list[tuple[str, str]]:
    """Parse section-delimited multi-table CSV text.

    A section delimiter is a line starting with a non-alphanumeric character
    (e.g. ``#``, ``>``, ``@``), followed by an entity/table name — analogous
    to a filename inside a ZIP archive.

    Example::

        # accounts
        id,name,account_type
        acct1,Cash,Asset

        # journal_entries
        docnumber,txndate,line_amount
        JE-001,2025-10-15,5000.00

    Args:
        csv_text: Multi-table CSV with section delimiters.

    Returns:
        List of ``(section_name, csv_text)`` tuples.  Empty sections
        (delimiter with no data rows) are skipped.

    Raises:
        CSVSnapshotError: If CSV data appears before the first delimiter.
    """
    if not csv_text or not csv_text.strip():
        return []

    sections: list[tuple[str, str]] = []
    current_name: str | None = None
    current_lines: list[str] = []

    def _finalize():
        if current_name is not None:
            body = "\n".join(current_lines).strip()
            if body:
                sections.append((current_name, body))

    for line in csv_text.splitlines():
        match = _SECTION_RE.match(line)
        if match and _is_section_name(match.group(1)):
            _finalize()
            current_name = normalize_header(match.group(1).strip())
            current_lines = []
        else:
            if current_name is None and line.strip():
                raise CSVSnapshotError(
                    "Section-delimited CSV must start with a section header like '# table_name'"
                )
            current_lines.append(line)

    _finalize()
    return sections


def detect_entity_type(headers: set[str], config: SnapshotConfig) -> str | None:
    """Detect entity type from CSV headers using config signatures.

    Tries to match normalized headers against each entity's required/optional
    headers, using aliases for flexible matching. Returns the best match by
    score.

    When ``optional`` is empty (convention-over-config mode), all extra
    headers beyond ``required`` count as matches — any column that maps to
    a real DB column is acceptable.

    If several entities match equally well and the tie cannot be broken by
    signature specificity (size of the ``required`` set), detection is
    treated as ambiguous and ``None`` is returned rather than picking an
    arbitrary entity. This is important when multiple entities use
    ``auto_required`` (empty ``required`` set): an empty set is a subset of
    every header set, so without this guard every such entity would match
    every CSV and the first one in config order would win arbitrarily.
    """
    scored: list[tuple[str, int, int]] = []  # (name, score, required_size)

    for name, entity in config.entities.items():
        imp = entity.import_config
        if not imp:
            continue
        sig = imp.signatures
        # Normalize headers using this entity's aliases
        normalized = {sig.aliases.get(h, h) for h in headers}
        if not sig.required.issubset(normalized):
            continue
        if sig.optional:
            # Explicit optional set — score by how many match
            optional_found = len(sig.optional.intersection(normalized))
        else:
            # Convention-over-config — all extra headers score as matches
            optional_found = len(normalized - sig.required)
        score = len(sig.required) + optional_found
        scored.append((name, score, len(sig.required)))

    if not scored:
        return None

    best_score = max(score for _, score, _ in scored)
    top = [(name, req) for name, score, req in scored if score == best_score]

    # Unique top score — clear winner.
    if len(top) == 1:
        return top[0][0]

    # Tie on score: prefer the most specific entity (largest required set).
    best_req = max(req for _, req in top)
    most_specific = [name for name, req in top if req == best_req]
    if len(most_specific) == 1:
        return most_specific[0]

    # Genuinely ambiguous (e.g. several auto_required entities) — refuse to
    # guess so the caller surfaces an "unrecognized schema" error instead of
    # silently importing into the wrong table.
    logger.warning(
        f"Ambiguous entity detection for headers {sorted(headers)}: "
        f"{sorted(most_specific)} match equally; treating as unrecognized"
    )
    return None


def read_csv_headers(csv_path: Path) -> set[str]:
    """Read and normalize headers from a CSV file.

    Delegates to the polars-backed :func:`read_csv_headers_path` so BOM /
    NUL hardening stays in one place, reading only a bounded prefix of the
    file (a whole-file read here OOM-killed populate on multi-GiB CSVs).
    """
    headers = read_csv_headers_path(csv_path)
    if not headers:
        raise CSVSnapshotError(f"CSV file {csv_path.name} has no headers")
    return {normalize_header(h) for h in headers}


def csv_has_data_rows(csv_path: Path) -> bool:
    """Check if a CSV file has at least one non-empty data row.

    Polars-backed: reads a single batch of rows and tests for any
    non-blank string. Returns ``False`` on any parse error so callers can
    treat unreadable files as "no data" without surfacing the exception
    (matches the legacy ``csv.DictReader`` behaviour).
    """
    try:
        # Read just enough to find a non-empty row; ``infer_schema_length=0``
        # forces every column to Utf8 (matches the legacy all-strings
        # contract).
        df = pl.read_csv(csv_path, n_rows=_IMPORT_BATCH_SIZE, infer_schema_length=0)
    except Exception:
        return False
    for row in df.iter_rows(named=True):
        if any(v and isinstance(v, str) and v.strip() for v in row.values()):
            return True
    return False


def _iter_normalized_csv_rows(
    rows: Iterable[dict[str, Any]],
    aliases: dict[str, str],
) -> Iterator[dict[str, str]]:
    """Yield normalized CSV rows one at a time without buffering.

    Accepts any iterable of row dicts (the polars-backed reader yields
    ``dict[str, str | None]`` per row; the legacy ``csv.DictReader``
    yielded ``dict[str, str]``). Normalises every header to snake_case
    and applies alias renames; fully-empty rows are skipped. Callers
    that need a list can wrap with ``list()``.
    """
    for row in rows:
        if not any(v and isinstance(v, str) and v.strip() for v in row.values()):
            continue
        normalized = {}
        for k, v in row.items():
            if k is None:
                continue
            norm_key = normalize_header(k)
            canonical = aliases.get(norm_key, norm_key)
            normalized[canonical] = v or ""
        yield normalized


def _normalize_csv_rows(
    rows: Iterable[dict[str, Any]],
    aliases: dict[str, str],
    source_label: str,
) -> list[dict[str, str]]:
    """Normalize CSV rows from an iterable of dicts, applying aliases.

    Shared logic for both file-based and string-based CSV reading.
    """
    out = list(_iter_normalized_csv_rows(rows, aliases))
    logger.info(f"Read {len(out)} rows from {source_label}")
    return out


def _normalize_record_keys(
    rows: list[dict[str, Any]],
    aliases: dict[str, str],
) -> list[dict[str, Any]]:
    """Snake-case + alias record *keys* while preserving values verbatim.

    The key-only counterpart to :func:`_normalize_csv_rows`, used by the
    non-CSV flat/denormalized path (:func:`_apply_non_csv_text`). CSV rows
    are always strings, so ``_normalize_csv_rows`` can coerce values (``v or
    ""``) and drop blank rows; JSON records carry typed values (ints, bools,
    nested objects) that must survive untouched, so this variant only remaps
    keys.

    Matches the CSV side's header handling — ``normalize_header`` then the
    entity's ``signatures.aliases`` — so a flat entity round-trips
    identically whether its source is CSV or JSON. ``normalize_header`` is
    idempotent on already-canonical keys, so records whose keys are already
    DB column names pass through unchanged. On a key collision after
    normalisation the last value wins (same as the CSV path).
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        normalized: dict[str, Any] = {}
        for k, v in row.items():
            if k is None:
                continue
            norm_key = normalize_header(k)
            canonical = aliases.get(norm_key, norm_key)
            normalized[canonical] = v
        out.append(normalized)
    return out


def read_csv_rows(csv_path: Path, entity_name: str, config: SnapshotConfig) -> list[dict[str, str]]:
    """Read CSV rows with normalized and alias-mapped headers.

    Returns list of row dicts with canonical column names. Materialises the
    whole file — used by callers that need ``len()`` or multi-pass iteration
    (e.g. pre-transform hooks that take ``list[dict]``). For streaming use,
    call :func:`iter_csv_rows` instead.
    """
    entity = config.entities.get(entity_name)
    imp = entity.import_config if entity else None
    aliases = imp.signatures.aliases if imp else {}

    info = SourceInfo(filename=csv_path.name)
    try:
        raw = read_csv(csv_path.read_bytes(), info)
    except ValueError as exc:
        raise CSVSnapshotError(f"CSV file {csv_path.name} has no headers") from exc
    return _normalize_csv_rows(raw, aliases, csv_path.name)


def iter_csv_rows(
    csv_path: Path,
    entity_name: str,
    config: SnapshotConfig,
) -> Iterator[dict[str, str]]:
    """Yield normalized + alias-mapped CSV rows one at a time without materialising.

    Built on ``pl.scan_csv(path).collect_batches(chunk_size=_IMPORT_BATCH_SIZE)``
    so peak memory stays bounded to a single batch (~10 000 rows). The
    streaming entry point for the legacy ``import_snapshot`` path; the
    EmitSet path (``import_snapshot_emits``) already streams via
    :func:`_apply_csv_file_batched`.

    Raises :class:`CSVSnapshotError` if the file has no header row. Empty
    rows (all cells blank) are skipped; alias renames in
    ``entity.import_config.signatures.aliases`` are applied.
    """
    entity = config.entities.get(entity_name)
    imp = entity.import_config if entity else None
    aliases = imp.signatures.aliases if imp else {}
    column_transforms = imp.column_transforms if imp else None

    if not read_csv_headers_path(csv_path):
        raise CSVSnapshotError(f"CSV file {csv_path.name} has no headers")

    import warnings

    # ``infer_schema_length=0`` forces every column to Utf8 — kills the
    # bool-inference bug class and matches the legacy csv.DictReader
    # all-strings contract.
    lf = pl.scan_csv(csv_path, infer_schema_length=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for df in lf.collect_batches(chunk_size=_IMPORT_BATCH_SIZE):
            # Always-vectorised column_transforms. ``apply_to_dataframe``
            # handles non-vectorisable transforms internally by falling
            # back to ``pl.col(name).map_elements`` per-column, so the
            # single-path dispatch is correct for every pipeline shape.
            # Row hooks (per-row + polars_expr) are dispatched downstream
            # in :meth:`EntityImporter.transform_flat` in registration
            # order — keeping all hook dispatch at one stage means
            # registration order is the only ordering rule.
            if column_transforms is not None:
                df = _apply_transforms_to_dataframe(df, column_transforms)

            raw_rows: Iterable[dict[str, Any]] = (
                {k: (v if v is not None else "") for k, v in row.items()}
                for row in df.iter_rows(named=True)
            )

            yield from _iter_normalized_csv_rows(raw_rows, aliases)


def read_csv_rows_from_string(
    csv_text: str,
    entity_name: str | None = None,
    config: SnapshotConfig | None = None,
) -> tuple[list[dict[str, str]], set[str]]:
    """Read CSV rows from a string with normalized headers.

    Dual-mode:
    - With config + entity_name: applies alias mapping from entity's import_config
    - Without: just normalizes headers to snake_case

    Returns:
        Tuple of (rows, normalized_headers)
    """
    aliases: dict[str, str] = {}
    if config and entity_name:
        entity = config.entities.get(entity_name)
        imp = entity.import_config if entity else None
        aliases = imp.signatures.aliases if imp else {}

    info = SourceInfo(filename="<string>")
    fieldnames = read_csv_headers_text(csv_text, info)
    if not fieldnames:
        raise CSVSnapshotError("CSV content has no headers")
    try:
        raw = read_csv(csv_text, info)
    except ValueError as exc:
        raise CSVSnapshotError("CSV content has no headers") from exc

    normalized_headers = {aliases.get(normalize_header(h), normalize_header(h)) for h in fieldnames}
    rows = _normalize_csv_rows(raw, aliases, "<string>")
    return rows, normalized_headers


def _read_raw_csv_rows(csv_text: str) -> list[dict[str, str]]:
    """Read CSV rows preserving the *original* headers (no normalization).

    Used by the directive fan-out path, where directives reference the original
    column names and the key normalizer defines the EAV keys. Fully-empty rows
    are skipped.
    """
    try:
        rows = read_csv(csv_text, SourceInfo(filename="<string>"))
    except ValueError as exc:
        raise CSVSnapshotError("CSV content has no headers") from exc
    # ``read_csv`` already strips fully-empty rows + coerces null cells to "".
    return list(rows)


def discover_csv_files(csv_dir: Path, config: SnapshotConfig) -> dict[str, list[Path]]:
    """Discover and categorize CSV files by entity type.

    Returns dict mapping entity name -> list of CSV file paths.
    Unrecognized files are under the "_unrecognized" key.
    """
    categorized: dict[str, list[Path]] = defaultdict(list)
    csv_files = sorted(csv_dir.glob("*.csv"))

    if not csv_files:
        logger.warning(f"No CSV files found in {csv_dir}")
        return categorized

    logger.info(f"Found {len(csv_files)} CSV file(s) in {csv_dir}")

    for csv_path in csv_files:
        try:
            headers = read_csv_headers(csv_path)
            entity_type = detect_entity_type(headers, config)
            if entity_type:
                categorized[entity_type].append(csv_path)
                logger.info(f"  {csv_path.name} -> detected as '{entity_type}'")
            else:
                categorized["_unrecognized"].append(csv_path)
                logger.error(f"  {csv_path.name} -> UNRECOGNIZED SCHEMA")
        except CSVSnapshotError as e:
            logger.error(f"  {csv_path.name}: {e}")

    return categorized


class EntityImporter:
    """Imports rows for a single entity type using its config.

    Handles:
    - Auto-ID generation with collision-free prefixed IDs
    - Deduplication by configured key
    - Flat imports (direct column mapping)
    - Denormalized imports (group-by, parent/child split)
    """

    def __init__(self, entity: EntityConfig, config: SnapshotConfig):
        self.entity = entity
        self.config = config
        self.imp = entity.import_config
        self._id_counter = 1
        self._child_id_counter = 1
        self._used_ids: set[str] = set()
        self._used_child_ids: set[str] = set()
        self._dedup_seen: dict[tuple, str] = {}

    def _next_id(self, prefix: str, used: set[str]) -> str:
        """Generate next collision-free ID."""
        counter_attr = "_id_counter" if prefix == self.imp.id_prefix else "_child_id_counter"
        counter = getattr(self, counter_attr)
        while True:
            new_id = f"{prefix}{counter:06d}"
            counter += 1
            if new_id not in used:
                setattr(self, counter_attr, counter)
                used.add(new_id)
                return new_id

    async def seed_id_counters(self, engine: AsyncEngine) -> None:
        """Query the DB for the highest existing auto-generated ID so that
        new IDs don't collide with previously inserted rows.

        For each prefix (parent ``id_prefix`` and ``child_id_prefix``), we
        look for existing IDs that match the ``{prefix}NNNNNN`` pattern and
        set the counter to ``max_N + 1``.
        """
        if not self.imp:
            return

        async def _max_counter(table: str, prefix: str, counter_attr: str) -> None:
            if not prefix or not table:
                return
            try:
                async with engine.connect() as conn:
                    result = await conn.execute(
                        text(f'SELECT "id" FROM "{table}" WHERE "id" LIKE :pat'),
                        {"pat": f"{prefix}%"},
                    )
                    ids = [r[0] for r in result.fetchall()]
                for existing_id in ids:
                    suffix = existing_id[len(prefix) :]
                    if suffix.isdigit():
                        val = int(suffix)
                        if val >= getattr(self, counter_attr):
                            setattr(self, counter_attr, val + 1)
                        if counter_attr == "_id_counter":
                            self._used_ids.add(existing_id)
                        else:
                            self._used_child_ids.add(existing_id)
            except Exception:
                # Table may not exist yet (first import) — safe to ignore
                pass

        await _max_counter(self.entity.table, self.imp.id_prefix, "_id_counter")
        if self.entity.child_table and self.imp.child_id_prefix:
            await _max_counter(
                self.entity.child_table, self.imp.child_id_prefix, "_child_id_counter"
            )

    def _collect_explicit_ids(self, rows: list[dict[str, str]]) -> None:
        """Pre-collect explicit IDs to prevent auto-ID collisions.

        Coerce to ``str`` before stripping: CSV rows are all strings, but a
        flat JSON source keeps native scalar types (e.g. a numeric ``id``),
        and a bare ``.strip()`` on an ``int`` would raise ``AttributeError``.
        This mirrors :meth:`_get_dedup_key`'s ``str(val)`` handling.
        """
        for row in rows:
            raw = row.get("id")
            if raw is None:
                continue
            row_id = str(raw).strip()
            if row_id:
                self._used_ids.add(row_id)

    def _get_dedup_key(self, row: dict[str, Any]) -> tuple | None:
        """Build dedup key tuple from row values."""
        if not self.imp or not self.imp.dedup_key:
            return None
        parts = []
        for field in self.imp.dedup_key:
            val = row.get(field, "")
            parts.append(str(val).strip().lower() if val else "")
        return tuple(parts)

    def _is_denormalized(self) -> bool:
        """Check if this entity uses denormalized (parent+child) CSV format."""
        return bool(self.imp and self.imp.group_by)

    def _is_normalized_format(self, rows: list[dict[str, str]]) -> bool:
        """Detect if rows are in normalized format (one row per parent, no child columns)."""
        if not self.imp or not self.imp.group_by:
            return True
        # If total_field is present and no child total_from column, it's normalized
        if self.imp.total_field and self.imp.total_from:
            has_total = any(row.get(self.imp.total_field) for row in rows[:5])
            has_child = any(row.get(self.imp.total_from) for row in rows[:5])
            if has_total and not has_child:
                return True
        return False

    def transform_flat(self, rows: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
        """Transform flat CSV rows into DB records.

        Two-pass structure:

        **Pass 1 — record synthesis.** For every input row: compute the
        dedup key, skip if already seen, build a record dict (coercing
        empty cells to ``None``), auto-assign ``id`` when ``id_prefix``
        is set and no explicit id was supplied, and set ``created_at`` /
        ``updated_at`` to a single ``datetime.now(UTC)`` per batch when
        the record doesn't already carry them.

        **Pass 2 — hooks in registration order.** Delegate to
        :func:`apply_row_hooks_in_order`, which dispatches every
        registered row hook in the order it was registered — per-row
        callables inline, ``polars_expr``-bearing hooks via a single
        ``pl.DataFrame(records).lazy().pipe(…).collect()`` round trip
        (consecutive ``polars_expr`` hooks chain on the same LazyFrame
        so polars's planner can fuse them).

        Both kinds of hooks see records with synthesized ``id`` /
        ``created_at`` / ``updated_at`` — the brief's
        "``polars_expr`` is a perf hint with the same observational
        semantics as the dict→dict callable" contract is satisfied
        exactly.

        Two behavioural notes:

        * **Streaming vs. materialising.** When ``id_prefix`` is empty,
          ``rows`` flows through the pass-1 loop as a generator and
          peak memory stays bounded to a single batch (matches the
          Phase B.2 contract). When ``id_prefix`` is set, the auto-ID
          collision pre-scan needs two passes over the input, so we
          materialise.
        * **Eager dedup commit.** ``_dedup_seen`` is updated when the
          initial record is built (pass 1), not after hooks succeed.
          A hook returning ``None`` for a record drops it from the
          output but does **not** release its dedup slot for a later
          row with the same key — that later row stays skipped. This is
          a small behavioural shift from the pre-Phase-C dispatch
          (which committed dedup post-hook); it keeps hook dispatch
          bulk-friendly so ``polars_expr`` hooks don't have to be
          dispatched record-by-record. Consumers that relied on
          "hook-None releases dedup" should structure their hooks to
          mark the row in a way the dedup key reflects, rather than
          dropping it.
        """
        if not self.imp:
            return []

        # Auto-ID collision protection requires walking the full input first.
        # Skip the materialisation when id_prefix is empty (no auto-IDs to
        # generate, nothing to protect).
        if self.imp.id_prefix:
            rows = list(rows)
            self._collect_explicit_ids(rows)
        now = datetime.now(UTC)

        # Pass 1 — synthesise initial records (ID + timestamps; dedup-filtered
        # with eager commit).
        initial_records: list[dict[str, Any]] = []
        for row in rows:
            dedup_key = self._get_dedup_key(row)
            if dedup_key and dedup_key in self._dedup_seen:
                continue

            record: dict[str, Any] = {}
            for k, v in row.items():
                record[k] = None if (v is None or v == "") else v

            if self.imp.id_prefix and not record.get("id"):
                record["id"] = self._next_id(self.imp.id_prefix, self._used_ids)
            if "created_at" not in record:
                record["created_at"] = now
            if "updated_at" not in record:
                record["updated_at"] = now

            if dedup_key:
                self._dedup_seen[dedup_key] = record.get("id", "")

            initial_records.append(record)

        # Pass 2 — apply hooks in registration order, with polars_expr support.
        return _apply_row_hooks_in_order(
            initial_records,
            self.config.get_row_hooks(self.entity.name),
        )

    def transform_flat_lazy(self, lf: pl.LazyFrame) -> pl.LazyFrame:
        """Lazy-first counterpart to :meth:`transform_flat`.

        Stays in polars Rust columnar form through every step that the
        records-first :meth:`transform_flat` performs in Python: null
        coercion of blank cells, ID assignment (with collision avoidance
        against explicit IDs from the CSV and previously-assigned auto
        IDs), timestamp synthesis, cross-batch dedup, and hook
        dispatch. No ``iter_rows`` materialisation — every value lives
        as a polars Series until the caller asks for records (or the
        per-row callable I/O escape hatch inside
        :func:`apply_row_hooks_lazy` forces a collect for that one
        hook).

        Per-batch cost on a 10k row × 47 column gmail-shaped batch
        drops from the ~58ms ``pl.DataFrame(records)`` round-trip that
        the records-first path pays to a couple of cheap columnar
        ``.collect()`` calls (one for the ``id`` column when
        ``id_prefix`` is set, one for the ``_dedup_key`` column when
        ``dedup_key`` is set). Neither materialises rows to Python.

        The cross-batch state (``self._used_ids``, ``self._dedup_seen``)
        is updated in place exactly as :meth:`transform_flat` did, so
        callers that interleave lazy and records-first batches in the
        same populate see consistent behaviour.

        Returns a :class:`polars.LazyFrame` carrying the surviving
        post-hook rows. The schema is derived statically — no inference
        anywhere — and downstream consumers can either chain further
        ``polars_expr`` operations or ``.collect()`` for the insert
        boundary.

        Behavioural parity notes (same contract as the records-first
        :meth:`transform_flat`):

        * **Streaming vs. materialising.** ID collision avoidance still
          needs to see the ``id`` column up front, but the materialise
          is column-narrow (a single ``Series``) instead of all 47
          columns × 10k rows.
        * **Eager dedup commit.** ``_dedup_seen`` is updated when the
          dedup filter is applied, *before* the hooks run. A hook
          returning ``None`` for a record drops it from the output but
          does **not** release its dedup slot for a later row.
        """
        if not self.imp:
            schema = lf.collect_schema()
            return pl.LazyFrame(schema=dict(schema)).limit(0)

        schema = lf.collect_schema()
        columns = list(schema.names())

        # 1. Empty-string → null normalisation on every Utf8 column.
        #    ``pl.scan_csv(infer_schema_length=0)`` produces Utf8 columns
        #    where blank cells may arrive as ``""`` rather than null
        #    depending on the polars version / reader options. Coerce
        #    explicitly so downstream code sees a uniform ``None`` shape.
        utf8_cols = [c for c in columns if schema[c] == pl.Utf8]
        if utf8_cols:
            lf = lf.with_columns(
                *[
                    pl.when(pl.col(c) == "").then(None).otherwise(pl.col(c)).alias(c)
                    for c in utf8_cols
                ]
            )

        # 2. Ensure ``id`` column exists so step 3 can address it
        #    uniformly. When the CSV omits ``id`` entirely, add it as
        #    an all-null Utf8 column — step 3 will populate it.
        if "id" not in columns:
            lf = lf.with_columns(pl.lit(None, dtype=pl.Utf8).alias("id"))

        # 3. ID assignment (collision-avoiding against ``self._used_ids``).
        #    We materialise the ``id`` column only — not the whole
        #    LazyFrame — to do the Python-side collision check. The
        #    LazyFrame's other columns stay lazy throughout.
        if self.imp.id_prefix:
            id_series = lf.select(pl.col("id")).collect()["id"]
            id_list: list[str | None] = list(id_series)
            # Register any explicit IDs from this batch first so the
            # auto-generation step skips them.
            for v in id_list:
                if v:
                    self._used_ids.add(v)
            # Generate IDs for the rest.
            prefix = self.imp.id_prefix
            for i, v in enumerate(id_list):
                if not v:
                    id_list[i] = self._next_id(prefix, self._used_ids)
            lf = lf.with_columns(
                pl.Series("id", id_list, dtype=pl.Utf8),
            )

        # 4. Timestamps — only add the columns the CSV doesn't already
        #    carry. Per-row variance within a column doesn't happen in
        #    practice (the CSV either ships the column or doesn't), so
        #    a column-level check matches the records-first per-row
        #    ``if "created_at" not in record`` check at this granularity.
        now = datetime.now(UTC)
        timestamp_exprs: list[pl.Expr] = []
        post_schema = lf.collect_schema().names()
        if "created_at" not in post_schema:
            timestamp_exprs.append(pl.lit(now).alias("created_at"))
        if "updated_at" not in post_schema:
            timestamp_exprs.append(pl.lit(now).alias("updated_at"))
        if timestamp_exprs:
            lf = lf.with_columns(*timestamp_exprs)

        # 5. Cross-batch dedup with eager commit.
        #    Compute the dedup key as a ``pl.struct`` (matches the
        #    records-first ``_get_dedup_key`` tuple shape), materialise
        #    just that one column, filter rows by Python set membership
        #    against ``self._dedup_seen``, then update the cross-batch
        #    state from the kept rows. Both cross-batch and within-batch
        #    dedup happen in one pass.
        #
        #    Two parity behaviours we have to honour vs the records-first
        #    :meth:`_get_dedup_key`:
        #
        #    * **All ``dedup_key`` fields contribute, even when absent
        #      from the batch schema.** The records-first variant does
        #      ``row.get(field, "")`` per field — missing columns become
        #      empty strings in the tuple. The lazy path used to only
        #      include columns present in the schema, so two rows that
        #      ought to collide on ``("a", "")`` and ``("a",)`` ended up
        #      with different tuple lengths and missed the dedup
        #      (Bugbot #2).
        #    * **All-blank tuples are still dedup keys.** The lazy path
        #      used to short-circuit ``if not any(key_tuple): keep`` so
        #      rows whose every dedup field was empty bypassed dedup.
        #      The records-first variant treats every non-None tuple as
        #      a dedup key — including the all-blank one — and dedupes
        #      it against itself, so subsequent rows with the same
        #      all-blank key are dropped (Bugbot #1).
        if self.imp.dedup_key:
            dedup_cols = list(self.imp.dedup_key)
            schema_names_now = set(lf.collect_schema().names())
            # Build dedup-key Exprs for every declared dedup column. For
            # columns absent from the batch schema we emit ``pl.lit("")``
            # — matches the records-first ``row.get(field, "")`` default
            # so the tuple shape stays identical between paths.
            dedup_key_exprs = [
                (
                    pl.col(c)
                    .cast(pl.Utf8, strict=False)
                    .str.strip_chars()
                    .str.to_lowercase()
                    .fill_null("")
                    if c in schema_names_now
                    else pl.lit("", dtype=pl.Utf8)
                ).alias(c)
                for c in dedup_cols
            ]
            lf = lf.with_columns(pl.struct(*dedup_key_exprs).alias("_dedup_key"))
            df = lf.collect()
            key_series = df["_dedup_key"].to_list()
            keep_mask: list[bool] = []
            committed_in_batch: set[tuple[str, ...]] = set()
            for k in key_series:
                if k is None:
                    # Null struct (shouldn't happen given the fill_null
                    # branches above, but guard against future polars
                    # behaviour) → treat as records-first does when the
                    # whole dedup_key check returns None: keep the row.
                    keep_mask.append(True)
                    continue
                key_tuple = tuple(k[c] for c in dedup_cols)
                if key_tuple in self._dedup_seen or key_tuple in committed_in_batch:
                    keep_mask.append(False)
                else:
                    keep_mask.append(True)
                    committed_in_batch.add(key_tuple)
            df = df.filter(pl.Series(keep_mask)).drop("_dedup_key")
            # Eager commit to cross-batch state.
            for key_tuple in committed_in_batch:
                self._dedup_seen[key_tuple] = ""
            lf = df.lazy()

        # 6. Apply hooks via the lazy dispatcher. polars_expr hooks
        #    chain on the LazyFrame; per-row callables (I/O escape
        #    hatches) collect → walk → rebuild lazy with explicit
        #    schema enforced by their contract.
        hooks = self.config.get_row_hooks(self.entity.name)
        return _apply_row_hooks_lazy(lf, hooks)

    def transform_denormalized(
        self, rows: list[dict[str, str]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Transform denormalized CSV rows into parent + child DB records.

        Groups rows by group_by column, extracts parent fields from first row,
        creates child records from each row's child columns.

        Returns:
            Tuple of (parent_records, child_records)
        """
        if not self.imp or not self.imp.group_by:
            return [], []

        # Check if data is actually in normalized format
        if self._is_normalized_format(rows):
            return self._transform_normalized(rows)

        self._collect_explicit_ids(rows)
        now = datetime.now(UTC)

        # Group rows by group_by column
        groups: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in rows:
            key = row.get(self.imp.group_by, "")
            if key:
                groups[key].append(row)

        # Apply group validation hooks
        for hook in self.config.get_group_hooks(self.entity.name):
            for group_key, group_rows in groups.items():
                hook(group_rows)  # Raises on validation failure

        parents = []
        children = []
        child_line_number = 0

        for group_key, group_rows in groups.items():
            first_row = group_rows[0]

            # Generate parent ID
            parent_id = self._next_id(self.imp.id_prefix, self._used_ids)

            # Build parent record from parent columns
            parent: dict[str, Any] = {"id": parent_id}
            for csv_col in self.imp.parent_columns:
                db_col = self.imp.parent_field_map.get(csv_col, csv_col)
                value = first_row.get(csv_col, "")
                parent[db_col] = value if value else None

            # Calculate total from child amounts if configured
            if self.imp.total_field and self.imp.total_from:
                total = sum(parse_decimal(r.get(self.imp.total_from, "0")) for r in group_rows)
                parent[self.imp.total_field] = total

            parent["created_at"] = now
            parent["updated_at"] = now

            # Apply hooks in registration order. The denormalized path
            # dispatches per-parent (one parent per ``group_by`` group),
            # so the bulk helper sees a single-record list. polars_expr
            # hooks pay a 1-row DataFrame round-trip — fine for typical
            # denormalized fixtures, suboptimal at scale (consumers with
            # heavy polars_expr needs should use a flat entity shape).
            survivors = _apply_row_hooks_in_order(
                [parent], self.config.get_row_hooks(self.entity.name)
            )
            if not survivors:
                continue
            parent = survivors[0]

            parents.append(parent)

            # Build child records
            fk_column = f"{self.entity.table.rstrip('s')}_id"
            if self.entity.child_table:
                # Infer FK column name from child table pattern
                # e.g., invoice_lines -> invoice_id, bill_line -> bill_id
                child_table = self.entity.child_table
                if child_table.endswith("_lines"):
                    fk_column = child_table.replace("_lines", "_id")
                elif child_table.endswith("_line"):
                    fk_column = child_table.replace("_line", "_id")

            for line_num, row in enumerate(group_rows, 1):
                child_id = self._next_id(
                    self.imp.child_id_prefix or f"{self.imp.id_prefix}line_",
                    self._used_child_ids,
                )
                child: dict[str, Any] = {
                    "id": child_id,
                    fk_column: parent_id,
                    "line_number": line_num,
                }

                # Extract child columns, stripping prefix
                strip = self.imp.child_column_strip_prefix
                for csv_col in self.imp.child_columns:
                    stripped = (
                        csv_col[len(strip) :] if strip and csv_col.startswith(strip) else csv_col
                    )
                    db_col = self.imp.child_field_map.get(stripped, stripped)
                    value = row.get(csv_col, "")
                    child[db_col] = value if value else None

                children.append(child)
                child_line_number += 1

        logger.info(
            f"Transformed {len(parents)} {self.entity.name} with {len(children)} child records"
        )
        return parents, children

    def _transform_normalized(
        self, rows: list[dict[str, str]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Handle normalized format (one row per parent, total_amount provided)."""
        if not self.imp:
            return [], []

        now = datetime.now(UTC)
        parents = []

        for row in rows:
            parent_id = self._next_id(self.imp.id_prefix, self._used_ids)
            parent: dict[str, Any] = {"id": parent_id}

            for csv_col in self.imp.parent_columns:
                db_col = self.imp.parent_field_map.get(csv_col, csv_col)
                value = row.get(csv_col, "")
                parent[db_col] = value if value else None

            # Use the total field directly from CSV
            if self.imp.total_field:
                parent[self.imp.total_field] = parse_decimal(row.get(self.imp.total_field, "0"))

            parent["created_at"] = now
            parent["updated_at"] = now

            # Apply hooks in registration order (single-record bulk
            # dispatch — see :meth:`transform_denormalized` for the
            # rationale on per-parent dispatch).
            survivors = _apply_row_hooks_in_order(
                [parent], self.config.get_row_hooks(self.entity.name)
            )
            if survivors:
                parents.append(survivors[0])

        logger.info(f"Transformed {len(parents)} {self.entity.name} (normalized format)")
        return parents, []

    def _fk_column_name(self) -> str:
        """Infer the child table's FK column name from the parent table /
        child table conventions (matches :meth:`transform_denormalized`).

        ``invoice_lines`` → ``invoice_id``; ``bill_line`` → ``bill_id``;
        fallback ``f"{entity.table.rstrip('s')}_id"`` for child tables
        whose name doesn't follow either suffix pattern.
        """
        if self.entity.child_table:
            child_table = self.entity.child_table
            if child_table.endswith("_lines"):
                return child_table.replace("_lines", "_id")
            if child_table.endswith("_line"):
                return child_table.replace("_line", "_id")
        return f"{self.entity.table.rstrip('s')}_id"

    def _is_normalized_format_lazy(self, lf: pl.LazyFrame) -> bool:
        """Lazy-form counterpart to :meth:`_is_normalized_format`.

        Materialises only the first 5 rows of the ``total_field`` and
        ``total_from`` columns to check the heuristic — cheaper than
        the records-first variant which walks ``rows[:5]`` in Python.
        """
        if not self.imp or not self.imp.group_by:
            return True
        if not (self.imp.total_field and self.imp.total_from):
            return False
        schema_names = lf.collect_schema().names()
        if self.imp.total_field not in schema_names:
            return False
        sample_cols: list[str] = [self.imp.total_field]
        if self.imp.total_from in schema_names:
            sample_cols.append(self.imp.total_from)
        head_df = lf.select(*[pl.col(c) for c in sample_cols]).head(5).collect()
        # ``Series.filter`` consumes a boolean ``Series`` — not a polars
        # ``Expr``. Compute the mask on the Series directly and check
        # for any truthy entry via ``.any()``.
        total_series = head_df[self.imp.total_field].drop_nulls()
        has_total = (total_series != "").any() if total_series.len() else False
        has_child = False
        if self.imp.total_from in schema_names:
            from_series = head_df[self.imp.total_from].drop_nulls()
            has_child = (from_series != "").any() if from_series.len() else False
        return bool(has_total) and not bool(has_child)

    def transform_denormalized_lazy(self, lf: pl.LazyFrame) -> tuple[pl.LazyFrame, pl.LazyFrame]:
        """Lazy-first counterpart to :meth:`transform_denormalized`.

        Produces a pair of LazyFrames — ``(parents, children)`` —
        without materialising rows to Python dicts. The aggregation
        runs via :meth:`polars.LazyFrame.group_by` + ``.agg(...)``,
        which is the polars-native way to fold a wide denormalized CSV
        into one parent row per group plus one child row per source
        row.

        Behavioural parity with :meth:`transform_denormalized`:

        * Rows whose ``group_by`` cell is null or empty are filtered
          out (matches the records-first ``if key:`` short-circuit).
        * Normalized-format detection runs first; matched batches go
          through :meth:`_transform_normalized_lazy`, returning
          ``(parents_lf, empty_children_lf)``.
        * Parent ID generation pre-registers any explicit IDs the CSV
          ships on the first row of each group; auto-generated IDs
          skip those collisions.
        * Group validation hooks (``config.get_group_hooks(...)``) run
          per group — these expect ``list[dict]`` and so do force a
          per-group materialisation, but only of that group's rows.
        * Per-parent row hooks dispatch via :func:`apply_row_hooks_lazy`
          on the *whole* parents LazyFrame in one call. The records-
          first variant dispatched per-parent (one-record batches);
          the lazy variant batches every parent in this transform call
          together so polars_expr hooks vectorise across them. The
          observational semantics on the columns the hook touches are
          unchanged.
        * Child ID generation uses the same shared
          ``self._id_counter`` as parent IDs (parents allocated
          first), matching the call order :meth:`transform_denormalized`
          establishes between :meth:`_next_id` invocations.
        """
        if not self.imp or not self.imp.group_by:
            empty = pl.LazyFrame()
            return empty, empty

        group_col = self.imp.group_by

        # 1. Filter rows whose group_by key is null or empty (records-first
        #    ``if key: groups[key].append(row)`` semantics).
        lf = lf.filter(pl.col(group_col).is_not_null() & (pl.col(group_col) != ""))

        # 2. Normalized-format detection — bail to the normalized path
        #    when the heuristic matches.
        if self._is_normalized_format_lazy(lf):
            return self._transform_normalized_lazy(lf)

        schema_names = lf.collect_schema().names()
        now = datetime.now(UTC)

        # 3. Pre-register any explicit parent IDs from the CSV's "id"
        #    column (first row of each group), then generate parent IDs
        #    for every unique group. The group_col is materialised as
        #    one Series — no records-form round trip.
        if "id" in schema_names:
            explicit = (
                lf.group_by(group_col, maintain_order=True)
                .agg(pl.col("id").first().alias("first_id"))
                .collect()
            )
            for first_id in explicit["first_id"].to_list():
                if first_id:
                    self._used_ids.add(first_id)

        group_keys = (
            lf.select(pl.col(group_col).unique(maintain_order=True)).collect()[group_col].to_list()
        )
        parent_id_map: dict[str, str] = {
            k: self._next_id(self.imp.id_prefix, self._used_ids) for k in group_keys
        }

        # 4. Group validation hooks. These expect ``list[dict]`` per
        #    group, so we materialise per-group via a struct aggregation
        #    — but only for the validation step, not the parent/child
        #    construction.
        group_hooks = self.config.get_group_hooks(self.entity.name)
        if group_hooks:
            grouped_df = (
                lf.group_by(group_col, maintain_order=True)
                .agg(pl.struct(pl.all()).alias("_rows"))
                .collect()
            )
            for row in grouped_df.iter_rows(named=True):
                group_rows = [dict(r) for r in row["_rows"]]
                for hook in group_hooks:
                    hook(group_rows)

        # 5. Build the parents LazyFrame via group_by + first/agg on
        #    parent columns, with renames applied at the .alias()
        #    step.
        parent_agg_exprs: list[pl.Expr] = []
        for csv_col in self.imp.parent_columns:
            db_col = self.imp.parent_field_map.get(csv_col, csv_col)
            if csv_col not in schema_names:
                continue
            # Records-first coerces ``value if value else None`` — empty
            # string → None — using ``first()`` then a when/then.
            parent_agg_exprs.append(
                pl.when(pl.col(csv_col).first() == "")
                .then(None)
                .otherwise(pl.col(csv_col).first())
                .alias(db_col)
            )

        parents = lf.group_by(group_col, maintain_order=True).agg(*parent_agg_exprs)

        # Total field aggregation (sum-of-children) when configured.
        if self.imp.total_field and self.imp.total_from and self.imp.total_from in schema_names:
            totals = lf.group_by(group_col, maintain_order=True).agg(
                pl.col(self.imp.total_from)
                .cast(pl.Utf8, strict=False)
                .map_elements(parse_decimal, return_dtype=pl.Float64)
                .sum()
                .alias(self.imp.total_field)
            )
            parents = parents.join(totals, on=group_col)

        # Stamp parent ``id``, ``created_at``, ``updated_at`` columns.
        parents = parents.with_columns(
            pl.col(group_col).replace_strict(parent_id_map, default=None).alias("id"),
            pl.lit(now).alias("created_at"),
            pl.lit(now).alias("updated_at"),
        )

        # Drop the group_by column from parents unless it's one of the
        # parent_columns (records-first keeps it only if it's in
        # parent_field_map's target set).
        parent_db_cols = {self.imp.parent_field_map.get(c, c) for c in self.imp.parent_columns}
        if group_col not in parent_db_cols:
            parents = parents.drop(group_col)

        # 6. Per-parent row hooks via lazy dispatch.
        #    A hook returning ``None`` for a parent drops that parent
        #    from the parents LazyFrame. The records-first
        #    :meth:`transform_denormalized` mirrors the drop on the
        #    child side via the outer-loop ``continue`` — a dropped
        #    parent also drops its children. The lazy path has to
        #    replicate that: collect the surviving parent IDs and
        #    filter the children frame to the groups they belong to.
        #    Otherwise children for a dropped parent emit anyway,
        #    stamped with the parent_id_map's id — the consumer sees
        #    orphaned children with dangling FKs (or, when the FK is
        #    enforced, an insert failure).
        hooks = self.config.get_row_hooks(self.entity.name)
        parents = _apply_row_hooks_lazy(parents, hooks)
        surviving_parent_ids: set[Any]
        if hooks:
            surviving_parent_ids = set(parents.select(pl.col("id")).collect()["id"].to_list())
        else:
            surviving_parent_ids = set(parent_id_map.values())
        surviving_group_keys: set[Any] = {
            k for k, pid in parent_id_map.items() if pid in surviving_parent_ids
        }

        # 7. Build the children LazyFrame. Each input row becomes one
        #    child record stamped with the parent FK, a line number
        #    within the group, and a generated child ID.
        fk_column = self._fk_column_name()
        child_strip = self.imp.child_column_strip_prefix
        child_rename: dict[str, str] = {}
        child_db_cols: list[str] = []
        for csv_col in self.imp.child_columns:
            if csv_col not in schema_names:
                continue
            stripped = (
                csv_col[len(child_strip) :]
                if child_strip and csv_col.startswith(child_strip)
                else csv_col
            )
            db_col = self.imp.child_field_map.get(stripped, stripped)
            child_db_cols.append(db_col)
            if csv_col != db_col:
                child_rename[csv_col] = db_col

        children = lf
        if child_rename:
            children = children.rename(child_rename)

        # Drop children whose parent was filtered out by a row hook.
        # Applied *before* line_number / ID assignment so the
        # 1-indexed sequence within each surviving group doesn't
        # leak gaps from the dropped groups.
        if hooks and len(surviving_group_keys) < len(parent_id_map):
            children = children.filter(pl.col(group_col).is_in(list(surviving_group_keys)))

        # Empty-string → null on child cols (matches records-first
        # ``value if value else None``).
        children_schema = children.collect_schema().names()
        empty_null_exprs = [
            pl.when(pl.col(c) == "").then(None).otherwise(pl.col(c)).alias(c)
            for c in child_db_cols
            if c in children_schema
        ]
        if empty_null_exprs:
            children = children.with_columns(*empty_null_exprs)

        # Stamp parent FK + line number (1-indexed within the group).
        children = children.with_columns(
            pl.col(group_col).replace_strict(parent_id_map, default=None).alias(fk_column),
            (pl.cum_count(group_col).over(group_col)).alias("line_number"),
        )

        # Generate child IDs (Python counter dance + Series injection).
        n_children = children.select(pl.len()).collect().item()
        child_prefix = self.imp.child_id_prefix or f"{self.imp.id_prefix}line_"
        child_ids: list[str] = [
            self._next_id(child_prefix, self._used_child_ids) for _ in range(n_children)
        ]
        children = children.with_columns(pl.Series("id", child_ids, dtype=pl.Utf8))

        # Project to just the columns the records-first child record
        # contains: id, FK, line_number, mapped child cols.
        keep_cols = ["id", fk_column, "line_number"] + [
            c for c in child_db_cols if c in children.collect_schema().names()
        ]
        children = children.select(*keep_cols)

        return parents, children

    def _transform_normalized_lazy(self, lf: pl.LazyFrame) -> tuple[pl.LazyFrame, pl.LazyFrame]:
        """Lazy counterpart to :meth:`_transform_normalized`.

        Handles the one-row-per-parent normalized format. No child
        rows; the second LazyFrame in the returned tuple is empty.
        """
        if not self.imp:
            empty = pl.LazyFrame()
            return empty, empty

        schema_names = lf.collect_schema().names()
        now = datetime.now(UTC)

        # Generate parent IDs for every row in the LazyFrame.
        n_rows = lf.select(pl.len()).collect().item()
        parent_ids: list[str] = [
            self._next_id(self.imp.id_prefix, self._used_ids) for _ in range(n_rows)
        ]

        # Build parent columns via rename + empty-null coercion.
        parent_rename: dict[str, str] = {}
        parent_db_cols: list[str] = []
        for csv_col in self.imp.parent_columns:
            if csv_col not in schema_names:
                continue
            db_col = self.imp.parent_field_map.get(csv_col, csv_col)
            parent_db_cols.append(db_col)
            if csv_col != db_col:
                parent_rename[csv_col] = db_col

        parents = lf
        if parent_rename:
            parents = parents.rename(parent_rename)

        parents_schema_now = parents.collect_schema().names()
        empty_null_exprs = [
            pl.when(pl.col(c) == "").then(None).otherwise(pl.col(c)).alias(c)
            for c in parent_db_cols
            if c in parents_schema_now
        ]
        if empty_null_exprs:
            parents = parents.with_columns(*empty_null_exprs)

        # Total field directly from CSV — coerce via parse_decimal.
        if self.imp.total_field and self.imp.total_field in parents_schema_now:
            parents = parents.with_columns(
                pl.col(self.imp.total_field)
                .cast(pl.Utf8, strict=False)
                .map_elements(parse_decimal, return_dtype=pl.Float64)
                .alias(self.imp.total_field)
            )

        # Inject the parent IDs as a Series; do this in its own
        # ``with_columns`` call so polars doesn't try to normalise
        # the Series against neighbouring ``pl.lit`` Exprs (which
        # raises ``Series constructor called with unsupported type
        # 'Expr'`` when mixed in the same call).
        parents = parents.with_columns(pl.Series("id", parent_ids, dtype=pl.Utf8))
        parents = parents.with_columns(
            pl.lit(now).alias("created_at"),
            pl.lit(now).alias("updated_at"),
        )

        # Apply hooks via lazy dispatch.
        hooks = self.config.get_row_hooks(self.entity.name)
        parents = _apply_row_hooks_lazy(parents, hooks)

        empty = pl.LazyFrame()
        return parents, empty

    def to_emits(self, rows: list[dict[str, str]]) -> EmitSet:
        """Express this entity's transform as an :class:`EmitSet`.

        Selects the transform by config:

        - ``directives`` set -> declarative one-row -> many-tables fan-out
          (records + hoisted sub-entities + junction rows + EAV/JSON column).
          NOTE: the directive path expects ``rows`` keyed by their *original*
          CSV headers (not snake_cased), since the key normalizer defines the
          EAV keys.
        - ``group_by`` set   -> denormalized parent (``entity.table``) + child
          (``entity.child_table``).
        - otherwise          -> flat: one Emit per record into ``entity.table``.

        The flat/denormalized records are identical to ``transform_flat`` /
        ``transform_denormalized`` — only the container changes.
        """
        if self.imp and self.imp.directives is not None:
            return transform_with_directives(rows, self.imp.directives, table=self.entity.table)

        emit_set = EmitSet()
        if self._is_denormalized():
            parents, children = self.transform_denormalized(rows)
            for parent in parents:
                emit_set.add(Emit(table=self.entity.table, record=parent))
            if self.entity.child_table:
                for child in children:
                    emit_set.add(Emit(table=self.entity.child_table, record=child))
        else:
            for record in self.transform_flat(rows):
                emit_set.add(Emit(table=self.entity.table, record=record))
        return emit_set


def import_snapshot(
    csv_dir: Path,
    config: SnapshotConfig,
    entities: list[str] | None = None,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Discover, read, and transform CSV files into DB-ready records.

    This function handles everything except actual DB insertion, which is
    left to the caller (since insertion depends on server-specific logic
    like import_csv_to_db, journal entry auto-generation, etc.).

    Args:
        csv_dir: Directory containing CSV files
        config: Snapshot configuration
        entities: Optional list of entity names to import (default: all)

    Returns:
        Dict mapping entity name -> {
            "parent": list of parent records,
            "child": list of child records (empty list if flat entity),
        }

    Raises:
        CSVSnapshotError: If CSV files have unrecognized schemas
    """
    categorized = discover_csv_files(csv_dir, config)

    if not categorized:
        logger.warning("No valid CSV files found to import")
        return {}

    if "_unrecognized" in categorized:
        unrecognized = categorized["_unrecognized"]
        file_list = ", ".join(f.name for f in unrecognized)
        raise CSVSnapshotError(
            f"Cannot import: {len(unrecognized)} CSV file(s) have unrecognized schemas: {file_list}"
        )

    results: dict[str, dict[str, list[dict[str, Any]]]] = {}

    for entity_name, csv_paths in categorized.items():
        if entity_name.startswith("_"):
            continue
        if entities and entity_name not in entities:
            continue

        entity = config.entities.get(entity_name)
        if not entity or not entity.import_config:
            continue

        entity_importer = EntityImporter(entity, config)

        all_parents: list[dict[str, Any]] = []
        all_children: list[dict[str, Any]] = []

        # Decide between the streaming path (constant memory) and the legacy
        # materialise-then-transform path.  Streaming is safe only when:
        #   * the entity is flat (group_by would need cross-row visibility),
        #   * there are no pre-transform hooks (they take ``list[dict]`` and
        #     run before transform_flat — passing them a generator would
        #     materialise inside the hook anyway),
        #   * ``id_prefix`` is empty (otherwise the auto-ID pre-scan inside
        #     transform_flat would have to materialise the iterator anyway).
        # Otherwise we keep the legacy full-file read so consumers don't
        # see behaviour drift.
        entity_imp = entity.import_config
        has_pre_hooks = bool(config.get_pre_transform_hooks(entity_name))
        can_stream = (
            not entity_importer._is_denormalized()
            and not has_pre_hooks
            and entity_imp is not None
            and not entity_imp.id_prefix
        )

        for csv_path in csv_paths:
            if not csv_has_data_rows(csv_path):
                logger.info(f"Skipping {csv_path.name} (no data rows)")
                continue

            if can_stream:
                # Generator → transform_flat → records. Constant-memory:
                # only one polars batch (~10k rows) is resident at a time.
                row_iter = iter_csv_rows(csv_path, entity_name, config)
                records = entity_importer.transform_flat(row_iter)
                all_parents.extend(records)
                continue

            rows = read_csv_rows(csv_path, entity_name, config)

            # Apply pre-transform hooks (e.g. format detection)
            # Note: only sync hooks are supported in this path;
            # async hooks (e.g. account resolution) run in import_csv_entity
            for hook in config.get_pre_transform_hooks(entity_name):
                result = hook(rows)
                if asyncio.iscoroutine(result):
                    # Can't await in sync function — skip async hooks here
                    result.close()  # prevent "coroutine was never awaited" warning
                elif result is not None:
                    rows = result

            if entity_importer._is_denormalized():
                parents, children = entity_importer.transform_denormalized(rows)
                all_parents.extend(parents)
                all_children.extend(children)
            else:
                records = entity_importer.transform_flat(rows)
                all_parents.extend(records)

        results[entity_name] = {
            "parent": all_parents,
            "child": all_children,
        }

    return results


def _file_matches_sources(filename: str, config: SnapshotConfig) -> bool:
    """True when ``filename`` matches at least one configured source glob.

    When ``config.sources`` is empty, returns ``True`` only for ``*.csv``
    (backward-compat with the CSV-only file walk).

    Uses :func:`_glob_match` (Python 3.12-compatible) for all patterns so
    that ``*`` never crosses a path separator and ``**`` matches zero-or-more
    path components.  Called from the flat ``os.scandir`` pass where
    ``filename`` is always a bare name (no slashes), so non-recursive
    patterns like ``*.csv`` match correctly and recursive ones like
    ``**/*.csv`` also match (zero directory components allowed by ``**``).
    """
    if not config.sources:
        return is_valid_csv_file(filename)
    name_str = str(filename)
    if "__MACOSX" in name_str or Path(filename).name.startswith("._"):
        return False
    return any(_glob_match(filename, src.glob) for src in config.sources)


def _scandir_source_paths(source_dir: Path, config: SnapshotConfig) -> list[Path]:
    """List source files with a single ``os.scandir`` (one readdir, stat-minimal).

    Walks the top of ``source_dir`` once and keeps every file matching
    :func:`_file_matches_sources` (the configured source globs, or ``*.csv``
    when none are configured). On FUSE/S3-backed mounts each directory op is a
    network round-trip, so a single ``os.scandir`` (with ``DirEntry.is_file()``
    using the cached ``d_type`` on most platforms) beats ``glob`` + per-entry
    ``stat``.
    """
    paths: list[Path] = []
    try:
        with os.scandir(source_dir) as entries:
            for entry in entries:
                if not _file_matches_sources(entry.name, config):
                    continue
                try:
                    if entry.is_file():
                        paths.append(Path(entry.path))
                except OSError:
                    continue
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(paths)


def _declared_source_suffixes(config: SnapshotConfig) -> set[str]:
    """Lowercased file suffixes declared across ``config.sources`` globs.

    Gates the extension-agnostic pass in :func:`_match_entity_by_filename`.
    Suffixes are taken from ``PurePosixPath(glob).suffix`` so ``**/*.json``,
    ``snapshot_*.json`` and a bare ``*.json`` all contribute ``.json``.
    Globs with no extension (e.g. a directory pattern) contribute nothing.
    """
    suffixes: set[str] = set()
    for src in config.sources:
        suffix = PurePosixPath(src.glob).suffix.lower()
        if suffix:
            suffixes.add(suffix)
    return suffixes


def _match_entity_by_filename(
    filename: str,
    config: SnapshotConfig,
    *,
    rel_path: str | None = None,
) -> str | None:
    """Return the entity whose ``import_config.files`` glob matches ``filename``.

    Filename routing takes precedence over header-signature detection — needed
    when several modules share the same envelope headers (so signatures can't
    disambiguate). Multiple entities may map to the same table.

    Matching runs in two passes:

    * **Pass 1 — literal.** The file must match an entity's ``files`` glob
      exactly. A JSON file that declares its own ``*.json`` glob is routed
      here, and literal matches always win over the cross-format pass.
    * **Pass 2 — cross-format (extension-agnostic).** When no literal glob
      matches, the file's suffix is swapped to each pattern's suffix and
      retried — but only when *both* suffixes are declared in
      ``config.sources``. This lets a snapshot enable a new source format
      for **every** entity by adding one ``sources`` line (e.g.
      ``- {glob: "*.json", format: json}``): an entity whose only glob is
      ``organization.csv`` then also routes ``organization.json`` without a
      per-entity ``files`` edit. The gate keeps the crossing scoped to
      declared formats — a ``.json`` file reuses a ``.csv`` glob only
      because both are declared, never matching an unrelated extension.

    Args:
        filename: Bare filename (``path.name``).
        config: Snapshot configuration.
        rel_path: POSIX path relative to ``csv_dir``
                  (e.g. ``"subdir/foo.csv"`` or ``"foo.csv"`` for root-level
                  files). Patterns containing ``/`` or ``**`` are matched
                  against this; bare-name patterns (no ``/`` or ``**``) fall
                  back to ``filename`` so that ``files: ["data.csv"]`` still
                  matches a file anywhere in the tree (backward compatible).
    """

    def _subject_for(pattern: str) -> str:
        # Path-aware patterns use rel_path for directory context; simple
        # name-only patterns (no / or **) match against the bare filename
        # so that pre-existing configs like files: ["data.csv"] continue
        # to route files in any sub-folder correctly.
        if "/" in pattern or "**" in pattern:
            return rel_path if rel_path is not None else filename
        return filename

    # Pass 1 — literal glob match (takes precedence over cross-format).
    for name, entity in config.entities.items():
        for pattern in entity.files:
            if _glob_match(_subject_for(pattern), pattern):
                return name

    # Pass 2 — cross-format fallback, gated on declared source suffixes.
    # Needs at least two distinct declared suffixes to cross between.
    declared = _declared_source_suffixes(config)
    if len(declared) < 2:
        return None
    for name, entity in config.entities.items():
        for pattern in entity.files:
            pat_suffix = PurePosixPath(pattern).suffix.lower()
            if not pat_suffix or pat_suffix not in declared:
                continue
            subject = _subject_for(pattern)
            subj_suffix = PurePosixPath(subject).suffix.lower()
            if subj_suffix == pat_suffix or subj_suffix not in declared:
                continue
            swapped = subject[: -len(subj_suffix)] + pat_suffix
            if _glob_match(swapped, pattern):
                return name
    return None


def _resolve_csv_paths(csv_dir: Path, config: SnapshotConfig) -> list[Path]:
    """Top-level scandir + (only if needed) recursive glob for nested layouts.

    Format-aware: when ``config.sources`` is non-empty, the top-level scan and
    nested walks honor the configured source globs (CSV + JSON + custom formats).
    When ``config.sources`` is empty, falls back to the CSV-only walk (``*.csv``)
    for backward compatibility.

    **Recursive glob support**: source globs containing ``**`` (e.g.
    ``**/*.csv``) trigger a ``Path.glob()`` walk that covers the whole
    tree — root level and every sub-folder. Patterns without ``**`` stay
    flat (top-level only via ``os.scandir``). Use ``*/**/*.csv`` to
    restrict to sub-folders only. Duplicates between the flat and
    recursive passes are removed by a set.

    If the config declares filename-routing (``EntityConfig.files``) and any
    literal name (no glob chars) isn't found at the top level, walk the tree
    ONCE filtered to those missing names. Matches the bespoke
    ``_resolve_all_csvs`` perf shape: a flat layout pays one readdir, a nested
    layout pays one extra rglob. Ambiguous matches (same name in multiple
    sub-folders) are logged and skipped.
    """
    # Phase 1: flat scan for non-recursive source globs (or legacy CSV-only).
    paths = _scandir_source_paths(csv_dir, config)
    found_paths: set[Path] = set(paths)

    # Phase 2: recursive glob discovery for ** source patterns.
    # ``**/*.csv`` covers the whole tree — root level AND every sub-folder.
    # Root-level files that also appeared in the Phase 1 flat scan are
    # deduplicated by the ``found_paths`` set.
    # Use ``*/**/*.csv`` to restrict to sub-folders only.
    recursive_globs = [src.glob for src in config.sources if "**" in src.glob]
    if recursive_globs:
        for pattern in recursive_globs:
            for p in csv_dir.glob(pattern):
                if not p.is_file():
                    continue
                # Apply the same macOS archive-metadata filter as Phase 1.
                rel = p.relative_to(csv_dir).as_posix()
                if "__MACOSX" in rel or p.name.startswith("._"):
                    continue
                found_paths.add(p)

    # Phase 3 (unchanged): rglob for literal entity filenames missing from the
    # top level (nested-layout compat for non-glob entity.files entries).
    found_names = {p.name for p in found_paths}
    expected: set[str] = set()
    for entity in config.entities.values():
        for pattern in entity.files:
            if not any(ch in pattern for ch in "*?["):
                expected.add(pattern)
    missing = expected - found_names
    if missing:
        try:
            has_subdirs = any(child.is_dir() for child in csv_dir.iterdir())
        except OSError:
            return sorted(found_paths)
        if has_subdirs:
            # Use the source globs to drive rglob when configured; otherwise
            # stick to ``*.csv`` to preserve the legacy perf shape.
            rglob_patterns: list[str] = (
                [src.glob for src in config.sources] if config.sources else ["*.csv"]
            )
            index: dict[str, list[Path]] = {}
            seen_paths: set[Path] = set()
            for pattern in rglob_patterns:
                for nested in csv_dir.rglob(pattern):
                    if nested in seen_paths:
                        continue
                    seen_paths.add(nested)
                    if nested.name in missing and nested.parent != csv_dir and nested.is_file():
                        index.setdefault(nested.name, []).append(nested)
            for name, found in index.items():
                if len(found) == 1:
                    found_paths.add(found[0])
                else:
                    rels = sorted(str(p.relative_to(csv_dir)) for p in found)
                    logger.error(f"{name} found in {len(found)} locations under {csv_dir}: {rels}")

    return sorted(found_paths)


def discover_snapshot(
    csv_dir: Path, config: SnapshotConfig
) -> dict[str, list[tuple[str, bytes | str]]]:
    """Single-pass discovery: one readdir + exactly one read per file.

    Reads each source file's full text or raw bytes once (per the format's
    binary flag), detects its entity from the in-memory headers when needed
    (CSV / JSON; binary formats route by filename only), and returns
    ``{entity_name: [(filename, content), ...]}`` with unrecognized files
    under the ``"_unrecognized"`` key. ``content`` is ``str`` for text-mode
    formats (CSV / JSON / custom) and ``bytes`` for binary-mode formats
    (file_content / any reader registered with ``binary=True``). Designed
    to meet a container-start budget on FUSE/S3 mounts where every open
    is an API call.

    Format dispatch: per-file format is resolved via :class:`SourceMapping`
    in ``config.sources`` (first glob match wins). When ``config.sources`` is
    empty, every file is read as CSV (backward compat). Header detection uses
    the format's registered ``header_reader``, so JSON sources detect entity
    via the keys of their first record. Binary formats skip header detection
    entirely — their "headers" are file-shape bytes (e.g. ``%PDF``) that
    don't carry column information.

    Nested layouts (top-level ``STATE_LOCATION`` with files in a sub-folder)
    are handled by :func:`_resolve_csv_paths` — one extra rglob filtered to
    the literal filenames declared in entities' ``files:`` mapping.

    For files discovered via ``**`` source globs, format resolution and entity
    routing use the path relative to ``csv_dir`` (e.g. ``"subdir/foo.csv"``)
    so that patterns like ``**/*.csv`` match correctly via
    :meth:`pathlib.PurePosixPath.full_match`.
    """
    categorized: dict[str, list[tuple[str, str, bytes | str | Path]]] = defaultdict(list)
    paths = _resolve_csv_paths(csv_dir, config)

    if not paths:
        logger.warning(f"No source files found in {csv_dir}")
        return categorized

    logger.info(f"Found {len(paths)} source file(s) in {csv_dir}")

    for path in paths:
        # Compute relative path for ** glob matching (e.g. "subdir/foo.csv").
        # For top-level files this equals path.name; for nested files it
        # includes the subdirectory component(s).
        try:
            rel_str = path.relative_to(csv_dir).as_posix()
        except ValueError:
            rel_str = path.name

        # Resolve format first so we know whether the reader expects bytes
        # (binary documents — PDF / DOCX / images) or UTF-8 text (CSV / JSON).
        fmt = resolve_format(path.name, config.sources, rel_path=rel_str)
        binary = is_binary_reader(fmt)

        info = SourceInfo(filename=path.name)

        # Multi-entity readers and binary formats need the full content
        # in memory. For single-entity text CSVs we defer the read and
        # store the *path* instead — the consumer streams rows on demand,
        # avoiding multi-GiB strings for large files.
        needs_full_read = binary or is_multi_entity_reader(fmt)

        if needs_full_read:
            try:
                content: bytes | str | Path = (
                    path.read_bytes() if binary else path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError) as exc:
                logger.error(f"  {path.name}: {exc}")
                continue
        else:
            # Deferred: store the path; only read headers for detection.
            content = path

        if is_multi_entity_reader(fmt):
            categorized[_MULTI_ENTITY_BUCKET].append((path.name, rel_str, content))
            logger.info(f"  {path.name} -> multi-entity reader ({fmt!r})")
            continue

        # Filename routing wins over header-signature detection. Binary formats
        # rely on filename routing exclusively (their "headers" are file-shape
        # specific and not useful for entity detection — e.g. a PDF's first
        # bytes are %PDF, not column names).
        entity_type = _match_entity_by_filename(path.name, config, rel_path=rel_str)
        if entity_type is None and not binary:
            # For deferred files, read just the header line for detection.
            # Use utf-8-sig to strip BOM and remove NUL bytes — matching
            # the tolerant reader in _as_csv_text (readers.py:171-195).
            try:
                if isinstance(content, Path) and fmt == "csv":
                    # Bounded prefix read — only the header row is pulled
                    # into memory. The whole-file ``read_bytes()`` this
                    # replaces OOM-killed populate when detection hit a
                    # multi-GiB warehouse CSV.
                    raw_headers = read_csv_headers_path(content) or None
                elif isinstance(content, Path):
                    # Deferred non-CSV file (e.g. a JSON source without a
                    # ``files:`` route). No bounded prefix reader exists for
                    # arbitrary formats, so read the full text and hand it to
                    # the format's registered header reader (JSON →
                    # ``read_json_headers``). SME-authored non-CSV fixtures
                    # are small, so the full read is cheap. Using the CSV
                    # header reader here instead would parse JSON as CSV and
                    # misroute the file.
                    header_reader = get_header_reader(fmt)
                    raw_headers = header_reader(content.read_text(encoding="utf-8"), info)
                else:
                    header_reader = get_header_reader(fmt)
                    raw_headers = header_reader(content, info)
            except (KeyError, ValueError, OSError) as exc:
                logger.error(f"  {path.name}: header read failed ({fmt}): {exc}")
                continue
            if not raw_headers:
                logger.error(f"  {path.name}: no headers")
                continue
            headers = {normalize_header(h) for h in raw_headers}
            entity_type = detect_entity_type(headers, config)

        if entity_type:
            categorized[entity_type].append((path.name, rel_str, content))
            logger.info(f"  {path.name} -> detected as '{entity_type}'")
        else:
            categorized["_unrecognized"].append((path.name, rel_str, content))
            logger.error(f"  {path.name} -> UNRECOGNIZED SCHEMA")

    return categorized


def _entity_table_order(entity: EntityConfig) -> list[str]:
    """FK-topological target tables for one entity's import (parents first)."""
    imp = entity.import_config
    if imp and imp.directives is not None:
        return directive_table_order(imp.directives, entity.table)
    order = [entity.table]
    if entity.child_table:
        order.append(entity.child_table)
    return order


def _apply_csv_file_batched(
    csv_path: Path,
    entity_name: str,
    entity_importer: EntityImporter,
    config: SnapshotConfig,
    emit_set: EmitSet,
) -> None:
    """Stream a deferred CSV file through the classic transform in batches.

    Built on ``pl.scan_csv(path).collect_batches(chunk_size=_IMPORT_BATCH_SIZE)``
    so the CSV is parsed by polars in Rust and never fully materialised in
    Python: at most ``_IMPORT_BATCH_SIZE`` rows live in memory at a time.
    Each batch passes through pre-transform hooks and
    ``EntityImporter.to_emits``; the importer's dedup / ID state accumulates
    across batches because the same instance is reused.

    Replaces the legacy ``csv.DictReader`` + Python-level batch accumulator
    that delivered the same row-level semantics but at ~10× the parsing
    cost — the bool-inference bug class (a string column whose cells happen
    to be ``"True"``/``"False"`` getting auto-inferred as ``pl.Boolean``)
    is sidestepped by forcing every column to ``pl.Utf8`` via
    ``infer_schema_length=0``.
    """
    entity = config.entities.get(entity_name)
    imp = entity.import_config if entity else None
    aliases = imp.signatures.aliases if imp else {}
    column_transforms = imp.column_transforms if imp else None

    # Verify the file has at least a header row before streaming the body —
    # matches the legacy ``if not reader.fieldnames:`` early-error.
    if not read_csv_headers_path(csv_path):
        raise CSVSnapshotError(f"CSV file {csv_path.name} has no headers")

    total = 0
    # ``infer_schema_length=0`` forces every column to ``Utf8``: matches the
    # legacy ``csv.DictReader`` all-strings contract and kills the bool-
    # inference bug class for string columns whose cells happen to be only
    # ``"True"`` / ``"False"`` values.
    lf = pl.scan_csv(csv_path, infer_schema_length=0)
    import warnings

    with warnings.catch_warnings():
        # ``collect_batches`` is marked unstable in polars 1.41 but is the
        # documented forward path (``read_csv_batched`` was deprecated in
        # 1.37 in favour of this exact call).
        warnings.simplefilter("ignore")
        batch_iter = lf.collect_batches(chunk_size=_IMPORT_BATCH_SIZE)
        for df in batch_iter:
            # Always-vectorised column_transforms (``apply_to_dataframe``
            # handles non-vectorisable transforms internally via
            # per-column ``map_elements`` fallback). All row hook
            # dispatch lives in ``transform_flat`` so registration order
            # is the only ordering rule across per-row and ``polars_expr``
            # hooks.
            if column_transforms is not None:
                df = _apply_transforms_to_dataframe(df, column_transforms)

            # Coerce nulls (empty cells) to "" so ``_iter_normalized_csv_rows``
            # sees the legacy ``csv.DictReader`` shape (str → str, never str →
            # None). The check inside ``_iter_normalized_csv_rows`` rejects
            # the all-blank rows.
            rows: Iterable[dict[str, Any]] = (
                {k: (v if v is not None else "") for k, v in row.items()}
                for row in df.iter_rows(named=True)
            )

            batch = list(_iter_normalized_csv_rows(rows, aliases))
            if not batch:
                continue
            _flush_classic_batch(batch, entity_name, entity_importer, config, emit_set)
            total += len(batch)
    logger.info(f"Read {total} rows from {csv_path.name} (streamed)")


def _flush_classic_batch(
    batch: list[dict[str, str]],
    entity_name: str,
    entity_importer: EntityImporter,
    config: SnapshotConfig,
    emit_set: EmitSet,
) -> None:
    """Apply pre-transform hooks and emit one batch of classic-entity rows."""
    for hook in config.get_pre_transform_hooks(entity_name):
        result = hook(batch)
        if asyncio.iscoroutine(result):
            result.close()
        elif result is not None:
            batch = result
    emit_set.extend(entity_importer.to_emits(batch).emits)


async def _stream_csv_file_to_db(
    csv_path: Path,
    entity_name: str,
    entity_importer: EntityImporter,
    config: SnapshotConfig,
    engine: AsyncEngine,
    conn: AsyncConnection,
) -> dict[str, int]:
    """Stream a flat-entity CSV file straight into the database, batch by batch.

    Solves the OOM on large flat entities (e.g. Foundry-Google-Workspace's
    118k-row Gmail messages corpus): the read side has streamed via
    :func:`_apply_csv_file_batched` since Phase B, but the emits there
    accumulated into a per-entity :class:`EmitSet` that wasn't inserted
    until the end of the import. For multi-GiB CSVs that accumulation is
    the entire memory profile.

    This function does the same per-batch polars read but **resolves and
    inserts each batch immediately** on the provided connection, then
    drops the references so the GC can reclaim the batch's DataFrame and
    Emits. Peak memory tracks one batch (``_IMPORT_BATCH_SIZE`` rows ≈
    10 000) plus the schema cache, regardless of how many rows the file
    contains overall.

    Constraints:

    * Caller-owned transaction. ``conn`` is open and (typically) inside
      a transaction with ``PRAGMA foreign_keys = OFF`` on SQLite, exactly
      matching :func:`_insert_snapshot_emits`'s contract. We do not
      ``commit()`` here — the orchestrator commits once everything has
      been streamed.
    * Flat entities only. Directive entities produce :class:`EmitLink`
      cross-row references during fan-out, and denormalized entities
      need cross-row group-by visibility; both would require carrying
      partial state across batches that defeats the streaming property.
      Those entities stay on :func:`_apply_csv_file_batched` +
      :func:`_insert_snapshot_emits`.
    * **CSV source only.** Uses ``pl.scan_csv`` under the hood — the
      polars Rust engine's row-group batching is CSV-specific and does
      not apply to JSON. JSON files under a flat entity route to the
      accumulate path (:func:`import_snapshot_emits`) via the format
      check in :func:`import_directory`. This path being CSV-only is
      the reason ``pl.scan_ndjson`` is deliberately not used — forcing
      newline-delimited JSON at the wire would defeat the "JSON is
      easy to hand-author" motivation for adding the format.

    Returns:
        ``{table_name: rows_inserted}`` for the rows from this file.
    """
    entity = config.entities[entity_name]
    imp = entity.import_config
    assert imp is not None, "_stream_csv_file_to_db requires an entity with import_config"
    aliases = imp.signatures.aliases
    column_transforms = imp.column_transforms

    # Header-presence guard. Same bounded reader as every other guard site so
    # NUL/BOM hardening and error types don't depend on which import path an
    # entity happens to route through (this site previously fed raw bytes to
    # a lazy ``pl.scan_csv``, which skips ``_as_csv_text``'s NUL-strip and
    # raises ComputeError instead of UnicodeDecodeError on bad UTF-8).
    if not read_csv_headers_path(csv_path):
        raise CSVSnapshotError(f"CSV file {csv_path.name} has no headers")

    file_results: dict[str, int] = {}
    total = 0
    lf = pl.scan_csv(csv_path, infer_schema_length=0)
    import warnings

    pre_hooks = config.get_pre_transform_hooks(entity_name)
    observers = config.get_batch_observers(entity_name)
    table = entity.table

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for df in lf.collect_batches(chunk_size=_IMPORT_BATCH_SIZE):
            if column_transforms is not None:
                df = _apply_transforms_to_dataframe(df, column_transforms)

            # Header normalisation + alias remapping in columnar form —
            # the records-first variant did this row-by-row inside
            # ``_iter_normalized_csv_rows``; here it's one rename on the
            # batch DataFrame.
            rename_map: dict[str, str] = {}
            for raw in df.columns:
                norm = normalize_header(raw)
                canonical = aliases.get(norm, norm)
                if raw != canonical:
                    rename_map[raw] = canonical
            if rename_map:
                df = df.rename(rename_map)

            # Filter fully-empty rows (records-first: ``any(v and v.strip()
            # for v in row.values())``). In polars: any Utf8 column has a
            # non-blank value.
            utf8_cols = [c for c, dt in df.schema.items() if dt == pl.Utf8]
            if utf8_cols:
                df = df.filter(
                    pl.any_horizontal(
                        *[(pl.col(c).fill_null("").str.strip_chars() != "") for c in utf8_cols]
                    )
                )
            if df.height == 0:
                continue

            # Pre-transform hooks need records form — they predate this
            # refactor and the engine has to honour their dict-callable
            # contract. Materialise only when a hook is registered; the
            # common case (no pre-transform hooks) stays purely columnar.
            if pre_hooks:
                batch_records: list[dict[str, Any]] = [
                    {k: (v if v is not None else "") for k, v in row.items()}
                    for row in df.iter_rows(named=True)
                ]
                for hook in pre_hooks:
                    result = hook(batch_records)
                    if asyncio.iscoroutine(result):
                        result.close()
                    elif result is not None:
                        batch_records = result
                if not batch_records:
                    continue
                df = pl.DataFrame(batch_records, infer_schema_length=None)

            # Lazy transform — ID assignment, dedup, timestamps, and
            # hook dispatch all stay in polars Rust. No records-form
            # materialisation here except for the column-narrow
            # collects ``transform_flat_lazy`` does internally for the
            # ID counter and dedup-key set membership checks.
            out_lf = entity_importer.transform_flat_lazy(df.lazy())
            out_df = out_lf.collect()
            if out_df.height == 0:
                continue

            # Batch observers fire here — they see the same final
            # post-hook DataFrame the insert step will use, no copy.
            for observer in observers:
                observer(out_df)

            # Insert directly from the polars DataFrame: tuple iteration
            # in Rust → DBAPI ``executemany`` with positional placeholders.
            # No records-form materialisation, no EmitSet, no per-row
            # dict construction. The ``insert_emit_set`` path stays for
            # entities that need FK link resolution (directives,
            # denormalized children) — flat entities don't, so the
            # ``Emit`` / ``EmitSet`` wrapper is pure overhead on this
            # hot path.
            inserted = await _insert_flat_dataframe(out_df, table, engine, conn)
            if inserted:
                file_results[table] = file_results.get(table, 0) + inserted
            total += out_df.height

    logger.info(f"Streamed {total} rows from {csv_path.name} → {file_results}")
    return file_results


def import_snapshot_emits(
    csv_dir: Path,
    config: SnapshotConfig,
    entities: list[str] | None = None,
    *,
    ignore_unrecognized: bool = False,
) -> dict[str, EmitSet]:
    """Discover, read, and transform a directory of CSVs into per-entity EmitSets.

    The EmitSet form of :func:`import_snapshot` — it supports the full
    multi-emit model, including declarative fan-out directives. Uses the
    single-pass :func:`discover_snapshot` (one readdir + one read per file), so
    each CSV is opened exactly once; rows are then parsed from the in-memory
    text. Directive entities use their *original* headers; classic
    flat/denormalized entities use normalized headers + sync pre-transform hooks.

    Args:
        csv_dir: Directory containing CSV files.
        config: Snapshot configuration.
        entities: Optional list of entity names to include (default: all).
        ignore_unrecognized: When True, files whose schema matches no entity
            are dropped with a warning instead of raising. Used by the
            ``import_always`` override path, where the pre-built-DB regime
            intentionally tolerates (and later prunes) stray sources — a
            scoped, flagged-entities-only apply must not abort on files that
            belong to the pruned bulk. Defaults to False so the normal import
            still fails loudly on unrecognized schemas.

    Returns:
        Dict mapping entity name -> EmitSet (empty dict if nothing to import).

    Raises:
        CSVSnapshotError: If CSV files have unrecognized schemas (unless
            ``ignore_unrecognized`` is set).
    """
    categorized = discover_snapshot(csv_dir, config)

    if not categorized:
        return {}

    if "_unrecognized" in categorized:
        unrecognized = categorized["_unrecognized"]
        file_list = ", ".join(name for name, *_ in unrecognized)
        if not ignore_unrecognized:
            raise CSVSnapshotError(
                f"Cannot import: {len(unrecognized)} CSV file(s) have unrecognized "
                f"schemas: {file_list}"
            )
        # Override path: drop the stray files (they belong to the pruned bulk)
        # and continue with only the flagged entities.
        categorized.pop("_unrecognized")
        logger.warning(
            "import_snapshot_emits: ignoring %d unrecognized source file(s): %s",
            len(unrecognized),
            file_list,
        )

    results: dict[str, EmitSet] = {}

    # One EntityImporter per entity, reused across ALL calls — single-entity
    # files AND multi-entity reader output. The importer carries dedup state
    # (``_used_ids``, ``_dedup_seen``) and surrogate-id counters; constructing
    # a fresh one per call would reset those, producing colliding ids and
    # silent dedup misses when the same entity receives rows from multiple
    # sources (e.g. an entity referenced by a CSV file plus a JSON multi-
    # entity payload, or by two different multi-entity files).
    importers: dict[str, EntityImporter] = {}

    def _get_importer(entity_name: str) -> EntityImporter | None:
        entity = config.entities.get(entity_name)
        if not entity or not entity.import_config:
            return None
        cached = importers.get(entity_name)
        if cached is None:
            cached = EntityImporter(entity, config)
            importers[entity_name] = cached
        return cached

    def _apply_rows_to_entity(entity_name: str, rows: list[dict[str, Any]]) -> None:
        """Run ``rows`` through ``entity_name``'s directive (or flat) transform.

        Merges the resulting emits into ``results[entity_name]``. Skips
        entities not in the active filter or without an import config.
        """
        if entities and entity_name not in entities:
            return
        entity = config.entities.get(entity_name)
        if not entity or not entity.import_config:
            return
        directives = entity.import_config.directives
        emit_set = results.setdefault(entity_name, EmitSet())
        if directives is not None:
            emit_set.extend(transform_with_directives(rows, directives, table=entity.table).emits)
        else:
            entity_importer = _get_importer(entity_name)
            assert entity_importer is not None  # narrowed by the early-return above
            for hook in config.get_pre_transform_hooks(entity_name):
                result = hook(rows)
                if asyncio.iscoroutine(result):
                    result.close()
                elif result is not None:
                    rows = result
            emit_set.extend(entity_importer.to_emits(rows).emits)

    def _apply_non_csv_text(
        text: str,
        fmt: str,
        filename: str,
        entity_name: str,
        entity_importer: EntityImporter,
        emit_set: EmitSet,
    ) -> None:
        """Read a non-CSV single-entity source and emit its rows.

        Shared by the ``Path`` and in-memory ``str`` flat/denormalized
        branches below (directive entities never reach here — they are
        routed through :func:`transform_with_directives`, which keeps the
        original keys on purpose). Dispatches through the reader registry
        (JSON → :func:`read_json`, custom formats via
        :func:`register_reader`), snake-cases + aliases the record keys so
        the flat transform lands them on the right DB columns (mirroring the
        CSV path's header normalisation — without it a flat JSON source
        would silently write to un-normalised / un-aliased columns), runs
        sync pre-transform hooks, then feeds the rows through the same
        :meth:`EntityImporter.to_emits` pipeline as CSV. No streaming —
        non-CSV formats are read fully (SME-authored sizes); see
        :func:`_stream_csv_file_to_db` for why JSON streaming is out of
        scope.
        """
        reader = get_reader(fmt)
        raw_rows = reader(text, SourceInfo(filename=filename))
        # Single-entity readers return ``list[dict]``; a multi-entity
        # reader routed here (via a misconfigured ``sources`` glob) would
        # return a dict — surface that as a config error rather than
        # letting it explode deeper in ``to_emits``.
        if not isinstance(raw_rows, list):
            raise CSVSnapshotError(
                f"{filename}: single-entity reader {fmt!r} returned "
                f"{type(raw_rows).__name__}, expected list[dict]"
            )
        # Normalise keys the same way the CSV path does (snake_case +
        # signature aliases), preserving typed values. Done before
        # pre-transform hooks so hooks see canonical keys, matching CSV.
        aliases = entity_importer.imp.signatures.aliases if entity_importer.imp else {}
        raw_rows = _normalize_record_keys(raw_rows, aliases)
        for hook in config.get_pre_transform_hooks(entity_name):
            result = hook(raw_rows)
            if asyncio.iscoroutine(result):
                result.close()
            elif result is not None:
                raw_rows = result
        emit_set.extend(entity_importer.to_emits(raw_rows).emits)

    # --- Single-entity files (routed via filename glob or header signature) ---
    for entity_name, files in categorized.items():
        if entity_name.startswith("_"):
            continue
        if entities and entity_name not in entities:
            continue

        entity = config.entities.get(entity_name)
        if not entity or not entity.import_config:
            continue

        directives = entity.import_config.directives
        # Cache hit if the entity already saw rows via the multi-entity loop;
        # cache miss creates the importer once per entity.
        entity_importer = _get_importer(entity_name)
        assert entity_importer is not None
        emit_set = results.setdefault(entity_name, EmitSet())

        for _name, _rel, content in files:
            if directives is not None:
                # Directive fan-out works on the original (un-normalized) keys.
                # Format dispatch picks CSV / JSON / custom-registered reader.
                # Pass rel_path so that **-glob patterns (e.g. "files/**/*.xlsx")
                # match against the relative path, not just the bare filename.
                if isinstance(content, Path):
                    content = content.read_text(encoding="utf-8")
                fmt = resolve_format(_name, config.sources, rel_path=_rel)
                reader = get_reader(fmt)
                raw_rows = reader(content, SourceInfo(filename=_name))
                # Single-entity readers return Records; mypy doesn't know the
                # bucketing in discover_snapshot ensures that here.
                emit_set.extend(
                    transform_with_directives(
                        raw_rows,  # type: ignore[arg-type]
                        directives,
                        table=entity.table,
                    ).emits
                )
            elif isinstance(content, Path):
                # Path-based flat/denormalized files. Dispatch by
                # ``resolve_format`` so JSON (and any other registered
                # non-CSV format) reads via its registered reader and
                # flows through the same ``EntityImporter.to_emits``
                # pipeline as CSV. Historically this branch was
                # CSV-only (``_apply_csv_file_batched`` + ``pl.scan_csv``),
                # which quietly returned 0 rows on a JSON file — the
                # bug ParityStudio hit.
                path_fmt = resolve_format(_name, config.sources, rel_path=_rel)
                if path_fmt != "csv":
                    # Non-CSV formats: dispatch through the reader
                    # registry (JSON → ``read_json``, custom formats
                    # via ``register_reader``). No streaming — see
                    # ``_stream_csv_file_to_db`` docstring for why
                    # JSON streaming is deliberately out of scope.
                    _apply_non_csv_text(
                        content.read_text(encoding="utf-8"),
                        path_fmt,
                        _name,
                        entity_name,
                        entity_importer,
                        emit_set,
                    )
                elif entity_importer._is_denormalized():
                    # Denormalized entities need all rows in one pass for
                    # correct group_by grouping — read fully.  These files
                    # are typically small (parent+child structure).
                    file_text = content.read_text(encoding="utf-8-sig").replace("\x00", "")
                    rows, _headers = read_csv_rows_from_string(file_text, entity_name, config)
                    for hook in config.get_pre_transform_hooks(entity_name):
                        result = hook(rows)
                        if asyncio.iscoroutine(result):
                            result.close()
                        elif result is not None:
                            rows = result
                    emit_set.extend(entity_importer.to_emits(rows).emits)
                else:
                    # Flat entity CSV — stream rows in batches to avoid
                    # materialising multi-GiB files in memory.
                    _apply_csv_file_batched(content, entity_name, entity_importer, config, emit_set)
            else:
                # Classic flat/denormalized path: in-memory string content
                # (small files or API-supplied content). Dispatch by
                # ``resolve_format`` so an in-memory JSON string reads via
                # its registered reader rather than being parsed as CSV
                # (which silently mis-parsed it before).
                assert isinstance(content, str)
                str_fmt = resolve_format(_name, config.sources, rel_path=_rel)
                if str_fmt != "csv":
                    _apply_non_csv_text(
                        content, str_fmt, _name, entity_name, entity_importer, emit_set
                    )
                    continue
                rows, _headers = read_csv_rows_from_string(content, entity_name, config)
                # Sync pre-transform hooks only (async ones run in import_csv_entity).
                for hook in config.get_pre_transform_hooks(entity_name):
                    result = hook(rows)
                    if asyncio.iscoroutine(result):
                        result.close()
                    elif result is not None:
                        rows = result
                emit_set.extend(entity_importer.to_emits(rows).emits)

    # --- Multi-entity files (reader's own keys ARE the entity routing) ---
    for filename, _rel, content in categorized.get(_MULTI_ENTITY_BUCKET, []):
        fmt = resolve_format(filename, config.sources, rel_path=_rel)
        reader = get_reader(fmt)
        try:
            multi_output = reader(content, SourceInfo(filename=filename))
        except (ValueError, KeyError) as exc:
            raise CSVSnapshotError(f"{filename}: multi-entity read failed ({fmt}): {exc}") from exc
        if not isinstance(multi_output, dict):
            raise CSVSnapshotError(
                f"{filename}: multi-entity reader {fmt!r} returned "
                f"{type(multi_output).__name__}, expected dict[str, list[dict]]"
            )
        unknown = [k for k in multi_output if k not in config.entities]
        if unknown:
            raise CSVSnapshotError(
                f"{filename}: multi-entity reader {fmt!r} emitted unknown entities: "
                f"{sorted(unknown)}. Declared: {sorted(config.entities)}"
            )
        for entity_name, rows in multi_output.items():
            if not isinstance(rows, list):
                raise CSVSnapshotError(
                    f"{filename}: multi-entity reader emitted non-list rows for "
                    f"{entity_name!r} ({type(rows).__name__})"
                )
            _apply_rows_to_entity(entity_name, rows)

    return results


@dataclass
class SnapshotFileCheck:
    """Per-file result of :func:`validate_snapshot`."""

    file: str
    ok: bool
    entity: str | None = None
    error: str | None = None


@dataclass
class SnapshotValidation:
    """Result of a dry-run snapshot validation."""

    ok: bool
    files: list[SnapshotFileCheck]
    errors: list[str]


def validate_snapshot(csv_dir: Path, config: SnapshotConfig) -> SnapshotValidation:
    """Dry-run validation of a source directory — no database access.

    Suitable for a seed-schema sandbox check: deterministic, side-effect-free,
    and fast (single readdir + one read per file). Verifies that:

    1. at least one source file is present,
    2. each file's format is registered (via ``config.sources`` dispatch),
    3. each file parses cleanly and exposes a non-empty set of "headers"
       (CSV: header row; JSON: keys of the first record), and
    4. each file's headers map to a configured entity (filename or signature).

    Returns a :class:`SnapshotValidation` whose ``ok`` flag a CLI can map to
    an exit code (0 when ``ok`` is True).
    """
    files: list[SnapshotFileCheck] = []
    errors: list[str] = []

    paths = _resolve_csv_paths(csv_dir, config)
    if not paths:
        return SnapshotValidation(
            ok=False, files=[], errors=[f"No source files found in {csv_dir}"]
        )

    for path in paths:
        try:
            rel_str = path.relative_to(csv_dir).as_posix()
        except ValueError:
            rel_str = path.name

        fmt = resolve_format(path.name, config.sources, rel_path=rel_str)
        binary = is_binary_reader(fmt)
        try:
            content: bytes | str = path.read_bytes() if binary else path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            files.append(SnapshotFileCheck(file=path.name, ok=False, error=str(exc)))
            errors.append(f"{path.name}: {exc}")
            continue

        info = SourceInfo(filename=path.name)

        # Multi-entity reader: call the reader, validate that every emitted
        # entity key is a declared entity in the config. The entity tagging
        # on the SnapshotFileCheck becomes a comma-joined summary so the CLI
        # can still report "what does this file contain?".
        if is_multi_entity_reader(fmt):
            try:
                reader = get_reader(fmt)
                output = reader(content, info)
            except (KeyError, ValueError) as exc:
                message = f"multi-entity read failed ({fmt}): {exc}"
                files.append(SnapshotFileCheck(file=path.name, ok=False, error=message))
                errors.append(f"{path.name}: {message}")
                continue
            if not isinstance(output, dict):
                message = (
                    f"multi-entity reader {fmt!r} returned {type(output).__name__}, expected dict"
                )
                files.append(SnapshotFileCheck(file=path.name, ok=False, error=message))
                errors.append(f"{path.name}: {message}")
                continue
            unknown = sorted(k for k in output if k not in config.entities)
            if unknown:
                message = f"reader emitted unknown entities: {unknown}"
                files.append(SnapshotFileCheck(file=path.name, ok=False, error=message))
                errors.append(f"{path.name}: {message}")
                continue
            entity_tag = ",".join(sorted(output.keys())) or None
            files.append(SnapshotFileCheck(file=path.name, ok=True, entity=entity_tag))
            continue

        # Binary single-entity formats validate by filename match only — their
        # bytes are file-shape (PDF/DOCX/...) and have no "headers" to detect
        # an entity from. The reader still runs at import time; here we just
        # ensure the file is routed to a declared entity.
        if binary:
            entity = _match_entity_by_filename(path.name, config, rel_path=rel_str)
            if entity is None:
                files.append(
                    SnapshotFileCheck(file=path.name, ok=False, error="unrecognized schema")
                )
                errors.append(f"{path.name}: unrecognized schema")
                continue
            files.append(SnapshotFileCheck(file=path.name, ok=True, entity=entity))
            continue

        # Single-entity reader: header detection -> filename / signature route.
        try:
            header_reader = get_header_reader(fmt)
            raw_headers = header_reader(content, info)
        except KeyError as exc:
            message = f"no reader registered for format {fmt!r}: {exc}"
            files.append(SnapshotFileCheck(file=path.name, ok=False, error=message))
            errors.append(f"{path.name}: {message}")
            continue
        except ValueError as exc:
            message = f"parse error ({fmt}): {exc}"
            files.append(SnapshotFileCheck(file=path.name, ok=False, error=message))
            errors.append(f"{path.name}: {message}")
            continue

        if not raw_headers:
            files.append(SnapshotFileCheck(file=path.name, ok=False, error="no headers"))
            errors.append(f"{path.name}: no headers")
            continue

        entity = _match_entity_by_filename(path.name, config, rel_path=rel_str)
        if entity is None:
            headers = {normalize_header(h) for h in raw_headers}
            entity = detect_entity_type(headers, config)
        if entity is None:
            files.append(SnapshotFileCheck(file=path.name, ok=False, error="unrecognized schema"))
            errors.append(f"{path.name}: unrecognized schema")
            continue

        files.append(SnapshotFileCheck(file=path.name, ok=True, entity=entity))

    return SnapshotValidation(ok=all(check.ok for check in files), files=files, errors=errors)


def _extract_table_name(csv_filename: str) -> str:
    """Extract table name from a CSV filename (basename minus .csv)."""
    name = os.path.basename(csv_filename)
    if name.lower().endswith(".csv"):
        name = name[:-4]
    if not name:
        raise ValueError(f"Invalid CSV filename: '{csv_filename}' results in empty table name")
    return name


async def _generic_import_zip(file_data: bytes, engine: AsyncEngine) -> dict[str, int]:
    """Import ZIP of CSVs using filename-to-table mapping (no config).

    Validates table names against the database schema.
    Uses DELETE + INSERT in a single transaction.
    """
    import pandas as pd

    zip_buffer = io.BytesIO(file_data)
    db_url = str(engine.url)
    is_sqlite = "sqlite" in db_url

    # Get existing tables for validation
    async with engine.connect() as conn:
        existing_tables = await conn.run_sync(
            lambda sync_conn: set(sa_inspect(sync_conn).get_table_names())
        )

    with zipfile.ZipFile(zip_buffer, "r") as zf:
        csv_files = [f for f in zf.namelist() if is_valid_csv_file(f)]

        # Map filenames to table names
        table_map: dict[str, list[str]] = {}
        for csv_filename in csv_files:
            table_name = _extract_table_name(csv_filename)
            table_map.setdefault(table_name, []).append(csv_filename)

        # Check for duplicates
        duplicates = {k: v for k, v in table_map.items() if len(v) > 1}
        if duplicates:
            dup_info = ", ".join(f"'{table}' from {files}" for table, files in duplicates.items())
            raise ValueError(
                f"Multiple CSV files map to the same table name: {dup_info}. "
                "Rename CSV files to avoid data loss."
            )

        # Validate tables exist
        unknown_tables = set(table_map.keys()) - existing_tables
        if unknown_tables:
            raise ValueError(
                f"CSV files target non-existent tables: {sorted(unknown_tables)}. "
                f"Only existing tables can be imported: {sorted(existing_tables)}"
            )

        # Parse all CSVs first (fail fast)
        tables_to_import: list[tuple[str, str, pd.DataFrame]] = []
        for csv_filename in csv_files:
            table_name = _extract_table_name(csv_filename)
            csv_data = zf.read(csv_filename).decode("utf-8")
            df = pd.read_csv(io.StringIO(csv_data), na_values=["\\N"], keep_default_na=False)
            df = df.where(pd.notnull(df), None)
            tables_to_import.append((csv_filename, table_name, df))

    # Import all tables in one transaction
    results: dict[str, int] = {}
    async with engine.connect() as conn:
        if is_sqlite:
            await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = OFF"))
        try:
            for csv_filename, table_name, df in tables_to_import:
                await conn.execute(text(f'DELETE FROM "{table_name}"'))
                await conn.run_sync(
                    lambda sync_conn, tn=table_name, data=df: data.to_sql(
                        tn, sync_conn, if_exists="append", index=False
                    )
                )
                results[table_name] = len(df)
                logger.info(f"Imported {len(df)} rows into {table_name}")
            await conn.commit()
        finally:
            if is_sqlite:
                await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = ON"))

    return results


def _positional_placeholders(paramstyle: str, n: int) -> str | None:
    """Build the positional placeholder list for an ``INSERT … VALUES (…)``
    clause, dispatched on the SQLAlchemy dialect's PEP 249 paramstyle.

    Returns the comma-separated placeholder string ready to splice into
    SQL, or :data:`None` when the dialect is named-style and the caller
    should fall back to ``SQLAlchemy.text()`` + dict bindings instead of
    raw DBAPI ``executemany`` over tuples.

    Supported positional styles:

    * ``qmark``   — ``?, ?, …``         (sqlite3, pyodbc)
    * ``format``  — ``%s, %s, …``       (psycopg2, aiomysql, mysql-connector)
    * ``numeric`` — ``:1, :2, …``       (oracledb)

    Named-style dialects (``named`` / ``pyformat``) return :data:`None`
    — they expect dict-shaped bindings that don't map to tuple iteration.
    """
    if paramstyle == "qmark":
        return ", ".join("?" for _ in range(n))
    if paramstyle == "format":
        return ", ".join("%s" for _ in range(n))
    if paramstyle == "numeric":
        return ", ".join(f":{i + 1}" for i in range(n))
    return None


async def _insert_flat_dataframe(
    df: pl.DataFrame,
    table: str,
    engine: AsyncEngine,
    conn: AsyncConnection,
) -> int:
    """Insert a flat-entity polars :class:`~polars.DataFrame` directly into
    the database, with no records-form materialisation.

    Bypasses :func:`insert_emit_set` for the flat hot path — flat entities
    have no FK link resolution, no directive fan-out, and no JSON-collapsed
    EAV columns, so the per-row dict construction the EmitSet path
    performs is pure overhead. Profiling on a 10k × 47-col gmail-shaped
    batch put the records→dict step at ~46 ms and the SQLAlchemy
    name-bound executemany at ~143 ms; replacing them with a Rust-side
    ``df.iter_rows()`` (tuples) + raw DBAPI ``cursor.executemany`` with
    positional placeholders lands the same batch at ~65 ms — roughly a
    3× speed-up on the insert step alone, and the records form is
    never created.

    Flow:

    1. Look up the destination table schema (cached) and intersect with
       the DataFrame's columns. Drop columns the table doesn't carry.
    2. Project the DataFrame to the intersection (Rust ``select``).
    3. Materialise tuples via ``aligned.iter_rows()`` — polars yields
       Python tuples directly from its columnar Rust buffers, no per-
       cell dict allocation.
    4. Hand the tuples to the underlying DBAPI cursor's ``executemany``
       with a positional INSERT. SQLAlchemy's ``Connection.run_sync``
       gives us the sync ``DBAPIConnection`` we need to reach the
       cursor; for async engines this dispatches to the driver's
       executor.

    Returns the number of rows inserted. Caller owns the transaction.
    """
    if df.height == 0:
        return 0

    schema = await get_table_schema(engine, table, conn=conn)
    df_col_set = set(df.columns)
    present_cols = [c for c in schema.columns if c in df_col_set]

    if not present_cols:
        return 0

    aligned = df.select(present_cols)

    paramstyle = getattr(engine.dialect, "paramstyle", "qmark")
    col_list = ", ".join(f'"{c}"' for c in present_cols)
    row_count = aligned.height

    # Hook outputs and column transforms can leave structured Python
    # values (``dict`` / ``list``) in cells when consumers fan out into
    # what would otherwise be EAV / JSON columns on the table. The
    # records-first ``insert_emit_set`` JSON-serialises those before
    # binding; the flat-streaming path used to pass the tuples straight
    # through, which produced invalid bindings (sqlite3 raises
    # ``InterfaceError: Error binding parameter`` for an unhashable
    # ``dict``; other drivers either reject the value or coerce to a
    # string repr like ``{'key': 'value'}`` that round-trips wrong).
    # ``_serialise_row`` walks each tuple once per batch; cost is
    # negligible vs the executemany itself.
    def _serialise_row(row: tuple[Any, ...]) -> tuple[Any, ...]:
        if not any(isinstance(v, (dict, list)) for v in row):
            return row
        return tuple(
            json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in row
        )

    placeholders = _positional_placeholders(paramstyle, len(present_cols))
    if placeholders is not None:
        sql = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})'
        rows_iter: Any = (_serialise_row(r) for r in aligned.iter_rows())
    else:
        # ``named`` / ``pyformat`` dialects need dict bindings. Use
        # SQLAlchemy text() so the dialect-specific param adaptation
        # happens automatically and we don't have to fork yet again per
        # driver.
        bind_names = [f"c{i}" for i in range(len(present_cols))]
        placeholders = ", ".join(f":{n}" for n in bind_names)
        sql_text = text(f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})')
        named_rows = [
            dict(zip(bind_names, _serialise_row(r), strict=True)) for r in aligned.iter_rows()
        ]
        await conn.execute(sql_text, named_rows)
        return row_count

    # ``iter_rows()`` (no ``named=True``) yields tuples in column order.
    # polars iterates lazily in Rust, allocating one Python tuple per
    # row only as the DBAPI cursor consumes it — no intermediate list
    # materialisation. The DBAPI cursor's ``executemany`` accepts any
    # iterator that yields parameter sequences, and pulls tuples one
    # at a time as it binds and dispatches each prepared row. Peak
    # Python heap for the insert step is bounded by the driver's
    # internal prepared-batch size, not by the batch row count.
    def _execute(sync_conn: Any) -> None:
        cursor = sync_conn.connection.cursor()
        try:
            cursor.executemany(sql, rows_iter)
        finally:
            cursor.close()

    await conn.run_sync(_execute)
    return row_count


async def insert_emit_set(
    emit_set: EmitSet,
    engine: AsyncEngine,
    conn: AsyncConnection,
    *,
    table_order: Sequence[str] | None = None,
    id_factory: Callable[[str], Any] | None = None,
) -> dict[str, int]:
    """Resolve an :class:`EmitSet` and insert its rows into the database.

    Resolves the emits (dedup + surrogate ids + FK link resolution + table
    ordering via :func:`resolve_emits`), strips columns not present in each
    target table's schema, and inserts the rows table-by-table on the provided
    connection. The caller owns the transaction and FK-pragma handling.

    Rows within a table are grouped by their present-column set so heterogeneous
    records (e.g. only some rows carry a resolved FK) insert correctly.

    Args:
        emit_set: Emits to insert.
        engine: Async engine (used for schema introspection).
        conn: Open connection owned by the caller; all inserts run on it and
            schema introspection reuses it (so in-memory SQLite works).
        table_order: Optional FK-topological table ordering (parents first).
        id_factory: Optional ``table -> id`` callable for surrogate ids.

    Returns:
        Mapping of table name -> number of rows inserted.
    """
    batches = resolve_emits(emit_set, table_order=table_order, id_factory=id_factory)
    results: dict[str, int] = {}
    for batch in batches:
        schema = await get_table_schema(engine, batch.table, conn=conn)
        db_cols = schema.column_names

        # Strip non-DB columns and JSON-serialize structured values (dict/list)
        # so EAV/JSON columns insert into a TEXT/JSON column, then group by
        # present-column set for executemany.
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        for record in batch.records:
            filtered = {
                # ensure_ascii=False keeps non-ASCII bytes intact for byte-stable
                # round-trips of EAV/JSON Text columns.
                k: (json.dumps(v, ensure_ascii=False) if isinstance(v, dict | list) else v)
                for k, v in record.items()
                if k in db_cols
            }
            if filtered:
                groups[tuple(filtered.keys())].append(filtered)

        inserted = 0
        for recs in groups.values():
            cols = list(recs[0].keys())
            placeholders = ", ".join(f":{c}" for c in cols)
            col_list = ", ".join(f'"{c}"' for c in cols)
            await conn.execute(
                text(f'INSERT INTO "{batch.table}" ({col_list}) VALUES ({placeholders})'),
                recs,
            )
            inserted += len(recs)

        if inserted:
            results[batch.table] = results.get(batch.table, 0) + inserted
            logger.info(f"Imported {inserted} rows into {batch.table}")

    return results


async def _insert_snapshot_emits(
    emits_by_entity: dict[str, EmitSet],
    engine: AsyncEngine,
    config: SnapshotConfig,
    *,
    confirm_clear: bool,
) -> dict[str, int]:
    """Insert per-entity EmitSets into the database in one transaction.

    Combines every entity's emits into a single set so shared dimension tables
    (e.g. a hoisted ``users`` table populated by several modules) deduplicate
    across entities, builds a global FK-topological table order, optionally
    clears the target tables (children first) when ``confirm_clear`` is True,
    then inserts in one resolution and runs post-import hooks.
    """
    if not emits_by_entity:
        return {}

    combined = EmitSet()
    table_order: list[str] = []
    per_entity_parents: dict[str, list[dict[str, Any]]] = {}
    for entity_name, emit_set in emits_by_entity.items():
        entity = config.entities[entity_name]
        for table_name in _entity_table_order(entity):
            if table_name not in table_order:
                table_order.append(table_name)
        combined.extend(emit_set.emits)
        per_entity_parents[entity_name] = [e.record for e in emit_set if e.table == entity.table]

    is_sqlite = "sqlite" in str(engine.url)

    async with engine.connect() as conn:
        if is_sqlite:
            await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = OFF"))
        try:
            if confirm_clear:
                # Clear all target tables once, children before parents.
                for table_name in reversed(table_order):
                    await conn.execute(text(f'DELETE FROM "{table_name}"'))

            # Insert everything in one resolution so cross-entity upsert dedup
            # and FK-link resolution apply.
            results = await insert_emit_set(combined, engine, conn, table_order=table_order)

            # Post-import hooks per entity, with that entity's parent
            # records as an async iterator (Phase ``PostImportHook`` was
            # tightened to ``AsyncIterable`` so the streaming path can
            # page the table without materialising the full list).
            for entity_name in emits_by_entity:
                entity = config.entities[entity_name]
                for hook in config.get_post_import_hooks(entity_name):
                    await hook(
                        entity.table,
                        _async_iter_list(per_entity_parents[entity_name]),
                        conn,
                    )

            await conn.commit()
        finally:
            if is_sqlite:
                await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = ON"))

    return results


async def apply_import_always(
    csv_dir: str | Path,
    engine: AsyncEngine,
    config: SnapshotConfig,
    *,
    entities: list[str] | None = None,
) -> dict[str, int]:
    """Apply ``import_always`` entities on top of an existing (pre-built) DB.

    Called by :func:`~mcp_middleware.csv_engine.snapshot_with_populate` when
    step 1 harvested an SME-shipped canonical DB (so the normal import step is
    skipped), but some entities are flagged ``import_always: true`` — the SME
    wants their shipped CSV/JSON to win over whatever the pre-built DB carried
    for those tables. The motivating case is a singleton override such as a
    ``current_user.csv`` re-pointing the default actor without rebuilding the
    world.

    Only the ``"clear"`` strategy is implemented: each flagged entity's target
    table(s) are emptied and the shipped rows inserted. This is *pure reuse* of
    existing machinery —

    * :func:`import_snapshot_emits` (``entities=flagged``) discovers, reads and
      transforms only the flagged entities' sources into per-entity EmitSets;
    * :func:`_insert_snapshot_emits` with ``confirm_clear=True`` builds the
      FK-topological table order **scoped to those entities**, clears their
      tables children-first, toggles the SQLite FK pragma off/on, inserts with
      cross-entity dedup, and runs post-import hooks.

    So the "clear whole flagged table + insert" behaviour, FK ordering and
    dedup all come for free from the same code a full clear import uses.

    Args:
        csv_dir: Directory the harvested source files live in (the snapshot
            ``state_dir``).
        engine: Async engine bound to the runtime DB the pre-built canonical
            was harvested onto.
        config: Snapshot config; ``config.import_always_entities()`` selects
            the flagged entities.
        entities: Optional explicit subset of the flagged entities to apply
            (default: all flagged). Names not flagged ``import_always`` are
            ignored.

    Returns:
        Per-table inserted-row counts. Empty dict when nothing is flagged, no
        matching source files are present, or every matching source produced
        no rows (a row-less override is a no-op, never a table wipe).

    Raises:
        CSVSnapshotError: If a flagged entity declares an unimplemented
            ``import_strategy`` (config load already rejects unknown strategies;
            this guards programmatically-built configs).
    """
    flagged = config.import_always_entities()
    if entities is not None:
        wanted = set(entities)
        flagged = [name for name in flagged if name in wanted]
    if not flagged:
        return {}

    # config.load already validates strategy on YAML load; re-check here so a
    # config assembled in code can't silently no-op through the clear path.
    for name in flagged:
        strategy = config.entities[name].import_strategy
        if strategy != "clear":
            raise CSVSnapshotError(
                f"entity {name!r}: import_strategy {strategy!r} is not implemented "
                f"(only 'clear' is supported)"
            )

    emits = import_snapshot_emits(Path(csv_dir), config, entities=flagged, ignore_unrecognized=True)
    # Drop entities whose source was present but produced no rows (empty file,
    # header-only CSV, all rows dropped by a hook). Clearing their table under
    # confirm_clear=True and inserting nothing would wipe pre-built data — the
    # opposite of the intended "replace with the shipped rows" semantics. An
    # empty shipment is treated as "no override", not "clear to empty".
    emits = {name: es for name, es in emits.items() if es.emits}
    if not emits:
        logger.info(
            "apply_import_always: no source rows for flagged entit%s %s in %s — nothing to apply",
            "y" if len(flagged) == 1 else "ies",
            flagged,
            csv_dir,
        )
        return {}

    results = await _insert_snapshot_emits(emits, engine, config, confirm_clear=True)
    logger.info(
        "apply_import_always: applied %d flagged entit%s (clear strategy) → %s",
        len(emits),
        "y" if len(emits) == 1 else "ies",
        results,
    )
    return results


async def _async_iter_list(records: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """Async-iterator wrapper around an in-memory record list.

    Used for accumulating entities, where the records list already exists
    and the hook just needs an :class:`AsyncIterable`-shaped surface.
    """
    for record in records:
        yield record


async def _async_iter_table_pages(
    table_name: str,
    conn: AsyncConnection,
    *,
    page_size: int = 1000,
) -> AsyncIterator[dict[str, Any]]:
    """Async-iterator paging through ``table_name`` for streaming-entity hooks.

    Issues ``SELECT * FROM "<table>" LIMIT page_size OFFSET <n>`` per
    page and yields each row as a ``dict`` keyed by column name. Peak
    memory at any point is one page (~``page_size`` records), so a
    PostImportHook on a 118k-row entity iterates without holding the
    full table in Python.

    LIMIT/OFFSET is portable across the SQL dialects csv_engine supports
    (SQLite, Postgres, MySQL) and is fine here because no other
    transaction can modify the table between pages — the same
    connection holds the streaming insert's open transaction.
    """
    offset = 0
    while True:
        result = await conn.execute(
            text(f'SELECT * FROM "{table_name}" LIMIT {page_size} OFFSET {offset}')
        )
        cols = list(result.keys())
        rows = result.fetchall()
        if not rows:
            return
        for row in rows:
            yield dict(zip(cols, row))
        offset += page_size


def _is_streamable_flat_entity(entity: EntityConfig, config: SnapshotConfig) -> bool:
    """True when the entity's **transform shape** admits per-batch streaming.

    Necessary but not sufficient. :func:`import_directory` additionally
    requires every file routed to the entity to be a ``Path`` AND to
    resolve to CSV format via :func:`resolve_format` — see
    :func:`_stream_csv_file_to_db` for why streaming is CSV-specific.
    Entities that pass this predicate but have JSON (or any non-CSV)
    files under them fall back to the accumulate path.

    Streaming is safe at the transform shape when the entity produces
    **independent emits per row**. Two blockers send an entity to the
    accumulation path:

    * **Directive fan-out** (``imp.directives is not None``). Directive
      entities produce :class:`EmitLink` cross-row references that
      :func:`resolve_emits` needs the full per-entity :class:`EmitSet`
      in memory to wire up correctly.
    * **Denormalized group-by** (``imp.group_by``). Group-by builds
      parent/child records from rows clustered by a key — that
      clustering needs the full row list visible at once.

    Post-import hooks do **not** block streaming. Their signature
    accepts an :class:`AsyncIterable` (see :data:`PostImportHook`), and
    the streaming dispatch wires a paginated DB cursor through it so
    the hook iterates without materialising the entity's records.

    Multi-entity reader output (rows already materialised in memory by
    the time the importer sees them) is filtered out one level up by
    the file-bucket check in :func:`import_directory` and does not
    reach this predicate.
    """
    imp = entity.import_config
    if imp is None:
        return False
    if imp.directives is not None:
        return False
    if imp.group_by:
        return False
    return True


async def import_directory(
    csv_dir: Path,
    engine: AsyncEngine,
    config: SnapshotConfig,
    *,
    confirm_clear: bool = False,
) -> dict[str, int]:
    """Import a directory of CSVs into the database via the config.

    Dispatches per-entity between two execution modes:

    * **Streaming flat entities** (no ``directives``, no ``group_by``):
      each CSV is read by polars in batches and the records are
      transformed + inserted into the DB per batch via
      :func:`_stream_csv_file_to_db`. Peak memory tracks one batch
      (~10 000 rows), regardless of how many rows the file contains.
      This is the path that defangs OOM on multi-GiB flat CSVs (e.g.
      Foundry-Google-Workspace's 118k-row Gmail messages corpus).
    * **Directive / denormalized / multi-entity-reader entities**:
      stay on the accumulation path — :func:`import_snapshot_emits`
      builds the full per-entity :class:`EmitSet` so :func:`resolve_emits`
      can do cross-row fan-out and cross-entity dedup, then a single
      :func:`_insert_snapshot_emits` call writes them in one
      transaction. These entities tend to be small relative to flat
      ones; accumulation is the right trade-off.

    Both modes run inside one open async connection, so ``confirm_clear``
    truncation, streaming inserts, accumulated inserts, and post-import
    hooks all execute under the same transaction. ``conn.commit()`` runs
    once at the end.

    Args:
        csv_dir: Directory containing the CSV files.
        engine: SQLAlchemy async engine.
        config: Snapshot configuration.
        confirm_clear: Clear target tables before inserting (full replace).

    Returns:
        Mapping of table name -> rows inserted across both modes.
    """
    categorized = discover_snapshot(csv_dir, config)
    if not categorized:
        logger.warning(f"No data found in {csv_dir} to import")
        return {}
    if "_unrecognized" in categorized:
        unrecognized = categorized["_unrecognized"]
        file_list = ", ".join(name for name, *_ in unrecognized)
        raise CSVSnapshotError(
            f"Cannot import: {len(unrecognized)} CSV file(s) have unrecognized schemas: {file_list}"
        )

    is_sqlite = "sqlite" in str(engine.url)

    # Sort entities into (streaming-capable, accumulation-required) buckets.
    # An entity is streamable only when each Path-based file routed to it
    # qualifies. Multi-entity-reader output (entity_name bucketed under
    # ``_MULTI_ENTITY_BUCKET``) always lands on the accumulation path —
    # those rows are already materialised in memory by the time the reader
    # returns them, so per-batch streaming wouldn't reduce peak memory.
    stream_entities: dict[str, list[tuple[str, str, Path]]] = {}
    accumulate_entities: set[str] = set()
    for entity_name, files in categorized.items():
        if entity_name.startswith("_"):
            continue
        entity = config.entities.get(entity_name)
        if not entity or not entity.import_config:
            continue
        if _is_streamable_flat_entity(entity, config):
            # All-or-nothing per entity: if every file routed here is
            # Path-based AND resolves to CSV via ``config.sources``,
            # take the streaming path. Otherwise route the entity to
            # the accumulation path so :func:`import_snapshot_emits`
            # processes the full file set in one place.
            #
            # The earlier "stream the Path subset + accumulate the rest"
            # split double-imported every Path file:
            # ``import_snapshot_emits`` re-discovers and processes *every*
            # file for the entity (it doesn't know which paths were
            # already streamed), so each Path-based file's rows landed in
            # the DB twice — once via :func:`_stream_csv_file_to_db` and
            # again as part of the accumulated EmitSet. Unique constraints
            # would surface this as an insert failure; without them the
            # row count silently doubles.
            #
            # Format check: ``_stream_csv_file_to_db`` uses ``pl.scan_csv``
            # under the hood — that's a CSV-specific streaming reader
            # and blows up on JSON with ``ComputeError: found more fields
            # than defined in 'Schema'``. Non-CSV formats route to the
            # accumulate path, which dispatches to the registered reader
            # (``read_json`` etc.) via :func:`import_snapshot_emits`.
            # We keep the streaming property for JSON entities out of
            # scope on purpose — JSON is used at SME-authored sizes where
            # OOM isn't a concern, and ``pl.scan_ndjson`` would force a
            # wire-format constraint SMEs shouldn't have to know about.
            #
            # Non-Path content under a flat entity is the rare
            # in-memory-API case (``import_directory`` itself only
            # produces Path entries, but
            # :func:`discover_snapshot`-built dispatch tables can ship
            # str / bytes when fed by other callers). When that mix
            # appears, prefer accumulation correctness over the streaming
            # perf win.
            def _is_streamable_file(rel: str, content: Any) -> bool:
                if not isinstance(content, Path):
                    return False
                fmt = resolve_format(content.name, config.sources, rel_path=rel)
                return fmt == "csv"

            if all(_is_streamable_file(r, c) for (_n, r, c) in files):
                stream_entities[entity_name] = [
                    (n, r, c) for (n, r, c) in files if isinstance(c, Path)
                ]
            else:
                accumulate_entities.add(entity_name)
        else:
            accumulate_entities.add(entity_name)

    # Build the global table order up front so PRAGMA disable + truncation +
    # both insert paths share a consistent FK-topological ordering.
    table_order: list[str] = []
    for entity_name in categorized:
        if entity_name.startswith("_"):
            continue
        entity = config.entities.get(entity_name)
        if not entity:
            continue
        for table_name in _entity_table_order(entity):
            if table_name not in table_order:
                table_order.append(table_name)

    results: dict[str, int] = {}

    async with engine.connect() as conn:
        if is_sqlite:
            await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = OFF"))
        try:
            if confirm_clear:
                # Clear children before parents.
                for table_name in reversed(table_order):
                    await conn.execute(text(f'DELETE FROM "{table_name}"'))

            # 1) Streaming flat entities — per-batch read + insert.
            #    One reused EntityImporter per entity so dedup / id state
            #    carries across files routed to the same entity.
            importers: dict[str, EntityImporter] = {}
            for entity_name, path_files in stream_entities.items():
                entity = config.entities[entity_name]
                importer = importers.setdefault(entity_name, EntityImporter(entity, config))
                for _name, _rel, csv_path in path_files:
                    file_results = await _stream_csv_file_to_db(
                        csv_path, entity_name, importer, config, engine, conn
                    )
                    for tbl, n in file_results.items():
                        results[tbl] = results.get(tbl, 0) + n

            # 2) Accumulating entities — directive / denormalized / multi-
            #    entity output. We need the full per-entity EmitSet for
            #    cross-row resolution, so build it in memory then insert.
            if accumulate_entities:
                accumulated = import_snapshot_emits(
                    csv_dir, config, entities=sorted(accumulate_entities)
                )
                if accumulated:
                    combined = EmitSet()
                    per_entity_parents: dict[str, list[dict[str, Any]]] = {}
                    for entity_name, emit_set in accumulated.items():
                        entity = config.entities[entity_name]
                        combined.extend(emit_set.emits)
                        per_entity_parents[entity_name] = [
                            e.record for e in emit_set if e.table == entity.table
                        ]
                    accum_results = await insert_emit_set(
                        combined, engine, conn, table_order=table_order
                    )
                    for tbl, n in accum_results.items():
                        results[tbl] = results.get(tbl, 0) + n

                    # Post-import hooks for accumulating entities — the
                    # records list is already in memory so we hand the
                    # hook an async-iterator over it. No extra cost beyond
                    # the existing list.
                    for entity_name in accumulated:
                        entity = config.entities[entity_name]
                        for hook in config.get_post_import_hooks(entity_name):
                            await hook(
                                entity.table,
                                _async_iter_list(per_entity_parents[entity_name]),
                                conn,
                            )

            # 3) Post-import hooks for streaming entities — paginated
            #    over the entity's table via the open connection, so a
            #    hook on a 118k-row entity iterates one page at a time
            #    without forcing csv_engine to re-accumulate records.
            for entity_name in stream_entities:
                entity = config.entities[entity_name]
                for hook in config.get_post_import_hooks(entity_name):
                    await hook(
                        entity.table,
                        _async_iter_table_pages(entity.table, conn),
                        conn,
                    )

            await conn.commit()
        finally:
            if is_sqlite:
                await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = ON"))

    return results


async def _config_import_zip(
    file_data: bytes, engine: AsyncEngine, config: SnapshotConfig
) -> dict[str, int]:
    """Import ZIP of CSVs using config for entity detection and transformation.

    Uses config signatures (or filename routing) to detect entity types,
    transforms rows via the directives, and inserts (clear + replace).
    """
    # Extract ZIP to temp dir so engine can discover files
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        with zipfile.ZipFile(io.BytesIO(file_data), "r") as zf:
            # Validate paths to prevent path traversal attacks
            for member in zf.namelist():
                member_path = (tmp_path / member).resolve()
                if not str(member_path).startswith(str(tmp_path.resolve())):
                    raise ValueError(f"Invalid ZIP entry with path traversal: {member}")
            zf.extractall(tmp_path)

        # Discover + transform every CSV into per-entity EmitSets (supports both
        # classic flat/denormalized entities and declarative fan-out directives).
        emits_by_entity = import_snapshot_emits(tmp_path, config)

    if not emits_by_entity:
        logger.warning("No data found in ZIP to import")
        return {}

    # ZIP import is a full snapshot replace (clear + insert).
    return await _insert_snapshot_emits(emits_by_entity, engine, config, confirm_clear=True)


async def import_from_zip(
    file_data: bytes,
    engine: AsyncEngine,
    config: SnapshotConfig | None = None,
) -> dict[str, int]:
    """Import a ZIP of CSV files into the database.

    Dual-mode function:
    - With config: uses entity detection, column mapping, transforms, and hooks
    - Without config: uses filename-to-table mapping with raw CSV import

    Args:
        file_data: Raw bytes of a ZIP file containing CSV files
        engine: SQLAlchemy async engine
        config: Optional snapshot configuration (None = generic mode)

    Returns:
        Dict mapping table name to row count imported

    Raises:
        ValueError: If ZIP is invalid or contains unrecognized CSVs
    """
    # Validate ZIP
    try:
        zip_buffer = io.BytesIO(file_data)
        with zipfile.ZipFile(zip_buffer, "r") as zf:
            csv_files = [f for f in zf.namelist() if is_valid_csv_file(f)]
            if not csv_files:
                raise ValueError("Invalid ZIP file: No CSV files found")
    except zipfile.BadZipFile:
        raise ValueError("Invalid ZIP file: File is not a valid ZIP archive")

    if config:
        return await _config_import_zip(file_data, engine, config)
    else:
        return await _generic_import_zip(file_data, engine)


async def _generic_import_csv(
    csv_text: str,
    table_name: str,
    engine: AsyncEngine,
    *,
    _conn: AsyncConnection | None = None,
) -> dict[str, int]:
    """Import CSV text into a table using pandas (no config). Appends rows.

    When ``_conn`` is provided the caller owns the connection and transaction;
    this function skips opening a new connection and does not commit.
    """
    import pandas as pd

    # Treat ONLY \N as NULL (consistent with the export side and
    # _generic_import_zip). ``keep_default_na=False`` disables pandas' default
    # NA tokens so empty strings round-trip as empty strings instead of being
    # coerced to NULL — empty cells lose their distinction from NULL otherwise,
    # which breaks ``export_all_tables_zip`` -> ``_generic_import_zip``
    # symmetry (where empty stays empty and ``\N`` is the only NULL token).
    df = pd.read_csv(
        io.StringIO(csv_text),
        skipinitialspace=True,
        encoding="utf-8",
        na_values=["\\N"],
        keep_default_na=False,
    )
    if df.empty:
        raise ValueError("CSV must have at least one data row")

    # Normalize column names
    df.columns = [col.lower().replace(" ", "_").replace("-", "_") for col in df.columns]

    async def _do_generic(conn: AsyncConnection) -> int:
        # Validate table exists
        existing_tables = await conn.run_sync(
            lambda sync_conn: set(sa_inspect(sync_conn).get_table_names())
        )
        if table_name not in existing_tables:
            raise ValueError(
                f"Table '{table_name}' does not exist. "
                "Please ensure the database schema is created via init_db() on startup."
            )

        def sync_import(sync_conn):
            df.to_sql(table_name, sync_conn, if_exists="append", index=False)
            return len(df)

        return await conn.run_sync(sync_import)

    if _conn is not None:
        row_count = await _do_generic(_conn)
    else:
        async with engine.connect() as conn:
            row_count = await _do_generic(conn)
            await conn.commit()

    logger.info(f"Imported {row_count} rows into {table_name} (generic mode)")
    return {table_name: row_count}


async def _import_with_directives(
    csv_text: str,
    entity: EntityConfig,
    config: SnapshotConfig,
    engine: AsyncEngine,
    directives: ImportDirectives,
    *,
    _conn: AsyncConnection | None = None,
) -> dict[str, int]:
    """Import a wide CSV for an entity that declares fan-out directives.

    Reads raw (un-normalized) rows, expands them into an EmitSet via the
    directives, and inserts FK-topologically through ``insert_emit_set``. The
    caller owns the connection when ``_conn`` is provided.
    """
    rows = _read_raw_csv_rows(csv_text)
    if not rows:
        raise ValueError("CSV must have at least one data row")

    importer = EntityImporter(entity, config)
    emit_set = importer.to_emits(rows)
    table_order = directive_table_order(directives, entity.table)
    unfiltered_parents = [e.record for e in emit_set if e.table == entity.table]

    results: dict[str, int] = {}

    async def _do_insert(conn: AsyncConnection) -> None:
        nonlocal results
        results = await insert_emit_set(emit_set, engine, conn, table_order=table_order)
        # Post-import hooks with unfiltered parent records (full metadata).
        for hook in config.get_post_import_hooks(entity.name):
            await hook(entity.table, unfiltered_parents, conn)

    if _conn is not None:
        await _do_insert(_conn)
    else:
        is_sqlite = "sqlite" in str(engine.url)
        async with engine.connect() as conn:
            if is_sqlite:
                await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = OFF"))
            try:
                await _do_insert(conn)
                await conn.commit()
            finally:
                if is_sqlite:
                    await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = ON"))

    return results


async def import_csv_entity(
    csv_text: str,
    table_name: str,
    engine: AsyncEngine,
    config: SnapshotConfig | None = None,
    *,
    _conn: AsyncConnection | None = None,
) -> dict[str, int]:
    """Import CSV text into a single table.

    Dual-mode:
    - With config: detects entity from table name, applies transforms,
      hooks (pre-transform, row, group, post-import), and inserts.
    - Without config: normalizes columns and appends via pandas.

    When ``_conn`` is provided the caller owns the connection, transaction,
    and FK pragma handling; this function only performs transforms and inserts.

    Args:
        csv_text: Raw CSV string
        table_name: Target database table
        engine: SQLAlchemy async engine
        config: Optional snapshot configuration

    Returns:
        Dict mapping table name(s) to row counts inserted

    Raises:
        ValueError: If table doesn't exist or CSV is invalid
    """
    if not table_name or not table_name.replace("_", "").isalnum():
        raise ValueError(
            f"Invalid table name '{table_name}': use only letters, numbers, and underscores"
        )

    # Find entity config for this table
    entity = config.find_entity_by_table(table_name) if config else None

    if not entity or not entity.import_config:
        # Generic mode — no config or table not in config
        return await _generic_import_csv(csv_text, table_name, engine, _conn=_conn)

    # Guard: if the caller asked for a child table but the entity uses
    # denormalized (parent+child) format, importing lines directly would
    # run the parent transforms against line-only data.  Reject early with
    # a helpful message.
    if (
        entity.child_table == table_name
        and entity.table != table_name
        and entity.import_config.group_by
    ):
        raise ValueError(
            f"Cannot import '{table_name}' directly — it is a child table of "
            f"'{entity.name}'. Import through the parent entity using "
            f"denormalized CSV with both parent and child columns "
            f"(grouped by '{entity.import_config.group_by}')."
        )

    # Config-driven mode
    entity_name = entity.name

    # Declarative fan-out path: one wide row -> many tables (records + hoisted
    # sub-entities + junction rows + EAV/JSON column), using original headers.
    if entity.import_config.directives is not None:
        return await _import_with_directives(
            csv_text, entity, config, engine, entity.import_config.directives, _conn=_conn
        )

    rows, _headers = read_csv_rows_from_string(csv_text, entity_name, config)

    if not rows:
        raise ValueError("CSV must have at least one data row")

    # Apply pre-transform hooks (e.g. format detection, account resolution)
    for hook in config.get_pre_transform_hooks(entity_name):
        result = hook(rows)
        if asyncio.iscoroutine(result):
            rows = await result
        elif result is not None:
            rows = result

    # Lazy schema resolution: fill auto_required from DB schema
    imp = entity.import_config
    if imp and imp.signatures.auto_required and not imp.signatures.required:
        schema = await get_table_schema(engine, entity.table)
        imp.signatures.required = schema.required_columns
        imp.signatures.auto_required = False
        logger.debug(f"Auto-resolved required columns for {entity_name}: {imp.signatures.required}")

    # Transform rows — seed ID counters from existing DB rows to avoid collisions
    importer = EntityImporter(entity, config)
    await importer.seed_id_counters(engine)
    # Transform into the unified multi-emit form (flat -> 1 table;
    # denormalized -> table + child_table) and insert via insert_emit_set,
    # which handles column filtering and ordered inserts.
    emit_set = importer.to_emits(rows)

    # Unfiltered parent records (into the primary table) for post-import hooks.
    unfiltered_parents = [e.record for e in emit_set if e.table == entity.table]
    if not unfiltered_parents:
        raise ValueError("No valid records after transformation")

    results: dict[str, int] = {}

    async def _do_insert(conn: AsyncConnection) -> None:
        nonlocal results
        results = await insert_emit_set(emit_set, engine, conn)

        # Run post-import hooks with unfiltered records so hooks have full metadata
        for hook in config.get_post_import_hooks(entity_name):
            await hook(entity.table, unfiltered_parents, conn)

    if _conn is not None:
        await _do_insert(_conn)
    else:
        is_sqlite = "sqlite" in str(engine.url)
        async with engine.connect() as conn:
            if is_sqlite:
                await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = OFF"))
            try:
                await _do_insert(conn)
                await conn.commit()
            finally:
                if is_sqlite:
                    await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = ON"))

    return results


async def import_multi_csv(
    csv_text: str,
    engine: AsyncEngine,
    config: SnapshotConfig | None = None,
) -> dict[str, int]:
    """Import section-delimited multi-table CSV text.

    Parses section delimiters to split text into per-entity chunks,
    then imports each in order within a single atomic transaction.

    Args:
        csv_text: Section-delimited CSV (e.g. ``# accounts\\nid,name\\n...``)
        engine: SQLAlchemy async engine
        config: Optional snapshot configuration

    Returns:
        Dict mapping table name(s) to row counts imported

    Raises:
        CSVSnapshotError: If text has no valid sections
        ValueError: If any section import fails (entire transaction rolls back)
    """
    sections = parse_multi_csv(csv_text)
    if not sections:
        raise CSVSnapshotError("No sections found in multi-table CSV")

    is_sqlite = "sqlite" in str(engine.url)
    results: dict[str, int] = {}

    async with engine.connect() as conn:
        if is_sqlite:
            await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = OFF"))
        try:
            for section_name, section_csv in sections:
                section_results = await import_csv_entity(
                    section_csv, section_name, engine, config, _conn=conn
                )
                for table, count in section_results.items():
                    results[table] = results.get(table, 0) + count

            await conn.commit()
        finally:
            if is_sqlite:
                await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = ON"))

    return results
