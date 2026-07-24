"""Source-format readers: bytes/text -> intermediate JSON representation (IR).

The IR is the contract between *source formats* (CSV, JSON, future XML/Parquet)
and the directive-driven mapping layer (:func:`transform_with_directives`).

Two reader shapes
-----------------

* **Single-entity reader** — ``(bytes | str) -> Records`` where
  ``Records = list[dict[str, Any]]``. One file produces rows for one entity;
  the file is routed to its entity by filename glob or header signature.
  CSV and the built-in JSON reader are single-entity. Use when the source
  format is naturally "one file per entity, wide rows."

* **Multi-entity reader** — ``(bytes | str) -> MultiEntityRecords`` where
  ``MultiEntityRecords = dict[str, Records]``. One file produces rows for
  *several* entities (e.g. a REST API response that returns leads + accounts
  + users in one payload). The reader's own keys ARE the entity routing;
  filename/header detection is skipped. Each (entity, rows) pair still runs
  through that entity's directive transform — constants, computed columns,
  FK extraction, dedup, etc. continue to work declaratively.

Each record is a "wide row" keyed by its *original* source-side field name
(``"Owner.id"``, ``"Deal Name"`` — not snake-cased). Values preserve native
types when the source format has them (JSON numbers, booleans, nested objects)
and are strings when the source can only carry text (CSV). The directive
transform tolerates either: scalar coercion happens lazily, nested
``dict``/``list`` values are stored as-is in the EAV column, and dotted-path
field lookups can reach into nested objects.

Readers are plain callables registered in a small format registry. Per-source
dispatch is configured at the snapshot level via
:class:`~mcp_middleware.csv_engine.config.SourceMapping` — a list of
``(glob, format)`` pairs that picks a reader per file.

Example: registering a single-entity custom format
--------------------------------------------------

    from mcp_middleware.csv_engine.readers import register_reader

    def read_yaml_records(source: bytes | str) -> list[dict[str, Any]]:
        import yaml
        if isinstance(source, bytes):
            source = source.decode("utf-8")
        data = yaml.safe_load(source) or []
        if not isinstance(data, list):
            raise ValueError("YAML source must be a top-level list of records")
        return [r for r in data if isinstance(r, dict)]

    register_reader("yaml", read_yaml_records)

Example: registering a multi-entity custom format
-------------------------------------------------

    def read_zoho_api_response(source: bytes | str) -> dict[str, list[dict]]:
        import json
        body = json.loads(source if isinstance(source, str) else source.decode())
        return {
            "leads":    body.get("modules", {}).get("Leads", []),
            "accounts": body.get("modules", {}).get("Accounts", []),
            "users":    body.get("users", []),
        }

    register_reader("zoho_api", read_zoho_api_response, multi_entity=True)

The YAML snapshot config routes files through registered readers:

    sources:
      - glob: "*.csv"
        format: csv
      - glob: "*.yaml"
        format: yaml
      - glob: "snapshot_*.json"
        format: zoho_api
"""

from __future__ import annotations

import base64
import io
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from .schema import TableSchema, polars_overrides

__all__ = [
    "DEFAULT_FORMAT",
    "MultiEntityReader",
    "MultiEntityRecords",
    "Reader",
    "Record",
    "Records",
    "SourceInfo",
    "get_header_reader",
    "get_reader",
    "is_binary_reader",
    "is_multi_entity_reader",
    "read_csv",
    "read_csv_headers_path",
    "read_csv_headers_text",
    "read_file_content",
    "read_json",
    "read_json_headers",
    "register_reader",
    "resolve_format",
]


# -----------------------------------------------------------------------------
# IR types
# -----------------------------------------------------------------------------

Record = dict[str, Any]
Records = list[Record]
MultiEntityRecords = dict[str, Records]


@dataclass(frozen=True)
class SourceInfo:
    """Per-source context passed to readers alongside the file's content.

    Lets a reader emit rows whose values depend on the file itself — its
    ``filename`` (used for MIME / extension detection in the ``file_content``
    reader; used for richer error messages elsewhere), and any future
    per-source metadata (mtime, source_app, …) added without breaking the
    Reader signature again.

    Attributes:
        filename: The on-disk file name (``Path.name``), without directories.
        extra: Reserved for future per-source metadata. Empty for now.
    """

    filename: str
    extra: dict[str, Any] = field(default_factory=dict)


Reader = Callable[[bytes | str, SourceInfo], Records]
MultiEntityReader = Callable[[bytes | str, SourceInfo], MultiEntityRecords]
HeaderReader = Callable[[bytes | str, SourceInfo], list[str]]


DEFAULT_FORMAT = "csv"


# -----------------------------------------------------------------------------
# Built-in readers
# -----------------------------------------------------------------------------


def _as_text(source: bytes | str) -> str:
    """Decode bytes as UTF-8; pass through str unchanged."""
    if isinstance(source, bytes):
        return source.decode("utf-8")
    return source


def _as_csv_text(source: bytes | str) -> str:
    """Decode CSV bytes as ``utf-8-sig`` and strip inline NUL bytes.

    Two real-world quirks the default ``read_csv`` is hardened against:

    * **BOM.** Excel / Google Sheets export "CSV UTF-8" with a leading
      ``\\ufeff`` byte-order mark. ``utf-8`` keeps it glued to the first
      header, silently breaking entity detection; ``utf-8-sig`` strips it
      and is a strict superset (clean UTF-8 decodes identically). Polars's
      stock ``read_csv`` also handles BOMs natively, but we still decode
      through ``utf-8-sig`` so the post-decoding NUL-strip below sees the
      same string shape regardless of input encoding.
    * **Inline NUL bytes (``\\x00``).** Polars itself preserves NULs inside
      string cells, but several downstream consumers (and the legacy
      ``csv.DictReader`` contract this reader replaces) treat NULs as
      illegal control characters. Stripping them once at decode time keeps
      the IR shape identical to the pre-polars era.

    ``str`` inputs are passed through (NULs still stripped) — they have
    already been decoded by the caller.
    """
    if isinstance(source, bytes):
        text = source.decode("utf-8-sig")
    else:
        text = source
    if "\x00" in text:
        text = text.replace("\x00", "")
    return text


def _schema_overrides_from_info(info: SourceInfo) -> dict[str, pl.DataType] | None:
    """Pull a ``polars`` ``schema_overrides`` mapping out of ``SourceInfo.extra``.

    Callers (the importer, primarily) inject ``info.extra["schema"]`` as a
    :class:`~mcp_middleware.csv_engine.schema.TableSchema` instance when they
    know the target table's shape; the reader then asks
    :func:`polars_overrides` for the per-column dtype map and hands it to
    ``pl.read_csv``. Without a schema, the reader falls back to
    ``infer_schema_length=0`` (force every column to ``Utf8``) — same
    observable behaviour, fewer hooks to wire.
    """
    schema = info.extra.get("schema") if info.extra else None
    if isinstance(schema, TableSchema):
        return polars_overrides(schema)
    return None


def read_csv(source: bytes | str, info: SourceInfo) -> Records:
    """Parse CSV text/bytes into the IR (polars-backed).

    Preserves the *original* header text on each record's keys (no snake-case
    normalization) — directives reference original column names and the key
    normalizer defines downstream EAV keys.

    Values are always strings (CSV has no type system, and the polars reader
    is pinned to ``Utf8`` either via ``schema_overrides`` from
    :func:`SourceInfo.extra["schema"] <SourceInfo>` or — when no schema is
    supplied — via ``infer_schema_length=0``). The all-strings contract is
    deliberate: it kills the bool-inference bug class where a column whose
    cells happen to be only ``"True"`` / ``"False"`` would otherwise get
    auto-inferred as ``pl.Boolean`` and break any downstream ``.str.*`` or
    ``parse_bool``. It also matches the legacy ``csv.DictReader`` contract
    one-for-one, so no consumer needs to update its
    ``parse_decimal`` / ``parse_bool`` / ``parse_date`` call sites.

    Fully-empty rows are skipped. The BOM / NUL hardening from the legacy
    reader is preserved (see :func:`_as_csv_text`).

    The ``info`` parameter is read for the optional ``info.extra["schema"]``
    dtype-override hint; ignoring it (``SourceInfo(filename="…")``) is fine
    and matches the legacy reader's behaviour.
    """
    text = _as_csv_text(source)
    overrides = _schema_overrides_from_info(info)
    try:
        if overrides is not None:
            df = pl.read_csv(io.StringIO(text), schema_overrides=overrides)
        else:
            df = pl.read_csv(io.StringIO(text), infer_schema_length=0)
    except pl.exceptions.NoDataError as exc:
        raise ValueError("CSV source has no headers") from exc

    if not df.columns:
        raise ValueError("CSV source has no headers")

    out: Records = []
    for row in df.iter_rows(named=True):
        # Coerce nulls (empty cells) to "" to match the legacy
        # ``csv.DictReader`` contract — downstream code routinely checks
        # ``v.strip()`` and an empty-string "" is fine, a None would crash.
        clean = {k: (v if v is not None else "") for k, v in row.items()}
        if any(v.strip() for v in clean.values() if isinstance(v, str)):
            out.append(clean)
    return out


def read_csv_headers_text(source: bytes | str, _: SourceInfo) -> list[str]:
    """Return the CSV header row in its original order (no normalization).

    Shares :func:`read_csv`'s BOM-tolerance + NUL-stripping (the BOM in
    particular is critical here — a header read in plain UTF-8 mode glues
    ``\\ufeff`` onto the first column and breaks entity detection).
    """
    text = _as_csv_text(source)
    try:
        # ``has_header=True`` (the default) + ``n_rows=0`` reads only the
        # header line. Avoids materialising any row data.
        df = pl.read_csv(io.StringIO(text), n_rows=0, infer_schema_length=0)
    except pl.exceptions.NoDataError:
        return []
    return list(df.columns)


# Bounded header scan: start small, double up to the cap, then fall back to
# the legacy whole-file read. 64 KB covers every real header in one read; the
# 8 MB cap bounds worst-case memory at ~3 orders of magnitude below the
# multi-GiB warehouse CSVs that OOM the populate sandbox on a full read.
_HEADER_SCAN_INITIAL_BYTES = 64 * 1024
_HEADER_SCAN_MAX_BYTES = 8 * 1024 * 1024


_UTF8_BOM = b"\xef\xbb\xbf"

# _header_row_end sentinel: a quote precedes the first newline, so the row
# boundary cannot be determined without a full CSV-dialect parse.
_QUOTED_HEADER = -2


def _header_row_end(raw: bytes) -> int:
    """Index of the newline that ends the header row in ``raw``.

    Returns ``-1`` when no newline has been read yet (caller grows the
    prefix) and ``_QUOTED_HEADER`` when a ``"`` appears before the first
    newline — polars' quote handling (RFC 4180 escapes, literal mid-field
    quotes, quoted cells spanning newlines) is deliberately NOT re-implemented
    here, because any divergence silently yields wrong headers; the caller
    falls back to the legacy whole-file parse instead.

    Byte-domain on purpose: in UTF-8, ``"`` (0x22) and ``\\n`` (0x0A) never
    occur inside a multibyte sequence, so a truncated prefix needs no
    decode-repair to scan. Mirrors polars on full text for the boundary:
    skip a leading BOM and empty lines (the header is the first non-empty
    row), then find that row's terminating newline.
    """
    start = len(_UTF8_BOM) if raw.startswith(_UTF8_BOM) else 0
    while True:
        if raw.startswith(b"\n", start):
            start += 1
        elif raw.startswith(b"\r\n", start):
            start += 2
        else:
            break
    newline = raw.find(b"\n", start)
    if newline == -1:
        return -1
    quote = raw.find(b'"', start)
    if quote != -1 and quote < newline:
        return _QUOTED_HEADER
    return newline


def read_csv_headers_path(csv_path: Path) -> list[str]:
    """Bounded-memory :func:`read_csv_headers_text` for an on-disk CSV.

    Reads a growing byte prefix (``_HEADER_SCAN_INITIAL_BYTES``, doubling up
    to ``_HEADER_SCAN_MAX_BYTES``) until it contains a complete unquoted
    header row, and parses exactly that slice. Peak memory stays
    proportional to the header row, not the file: the whole-file
    ``read_bytes()`` this replaces OOM-killed populate on multi-GiB
    warehouse CSVs (a 20 GiB ``emails.csv`` vs a 64 GB sandbox).

    Parity contract: identical results to ``read_csv_headers_text(
    csv_path.read_bytes(), …)`` — same BOM / NUL hardening, same polars
    header parse. The bounded fast path only fires for the shape it can
    prove safe (an unquoted header row inside the cap — every real
    warehouse CSV); anything else (a quote before the first newline,
    headers over 8 MB, no newline at all) falls back to the legacy
    whole-file read: never wrong and never worse than the code this
    replaces, but those shapes keep its memory cost.
    """
    info = SourceInfo(filename=csv_path.name)
    raw = b""
    target = _HEADER_SCAN_INITIAL_BYTES
    with csv_path.open("rb") as fh:
        while True:
            raw += fh.read(target - len(raw))
            if len(raw) < target:
                # Short read on a regular file = EOF: whole file in hand,
                # byte-identical to the legacy read_bytes() parse.
                return read_csv_headers_text(raw, info)
            # Scan a NUL-stripped view so the row boundary matches what
            # polars sees after _as_csv_text's hardening (a NUL adjacent to
            # the header newline must not hide a blank line from the skip).
            scan = raw.replace(b"\x00", b"") if b"\x00" in raw else raw
            end = _header_row_end(scan)
            if end >= 0:
                return read_csv_headers_text(scan[: end + 1], info)
            if end == _QUOTED_HEADER or target >= _HEADER_SCAN_MAX_BYTES:
                break
            target = min(target * 2, _HEADER_SCAN_MAX_BYTES)
    return read_csv_headers_text(csv_path.read_bytes(), info)


def read_json(source: bytes | str, _: SourceInfo) -> Records:
    """Parse JSON text/bytes into the IR.

    The source must be a top-level list of objects (records). Native value
    types (numbers, booleans, nested dicts/lists) are preserved. Records that
    are not dicts are dropped with a permissive shape check.

    Raises:
        ValueError: If the JSON is not a top-level list, or fails to parse.
    """
    text = _as_text(source)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON source is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(
            f"JSON source must be a top-level list of records, got {type(data).__name__}"
        )
    out: Records = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        # Coerce non-string keys defensively (JSON keys are strings, but a
        # mistake in a hand-rolled loader could pass an int).
        out.append({str(k): v for k, v in entry.items()})
    return out


def read_json_headers(source: bytes | str, _: SourceInfo) -> list[str]:
    """Return the field names of the first record (a JSON file's "headers").

    Returns an empty list when the source is empty or has no records.
    """
    text = _as_text(source)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    for entry in data:
        if isinstance(entry, dict):
            return [str(k) for k in entry]
    return []


def read_file_content(source: bytes | str, info: SourceInfo) -> Records:
    """Read a binary document file and emit a one-row record with its content.

    Bridges :mod:`mcp_files`'s extraction toolkit (PDF / Office Open XML /
    legacy ``.doc`` / HTML / text / calendar / email / image OCR / archives)
    into the csv_engine reader registry: one input file produces one record
    whose columns are file identity (``file_id``, ``sha256``), file metadata
    (``filename``, ``extension``, ``mime_type``, ``size_bytes``), the faithful
    raw bytes (``content``), and best-effort extracted text (``extracted_text``,
    ``extract_status``, ``extract_method``, ``warnings``).

    ``content`` vs ``extracted_text`` — these are distinct, not copies:

    * ``content`` is the **original file bytes**, base64-encoded so they survive
      the text/columnar snapshot boundary. Decode it (``base64.b64decode``) to
      recover the exact input file — a valid ``.xlsx`` / ``.pdf`` / ``.docx``,
      byte-for-byte. This is what a file-download endpoint should serve back
      under the file's real ``mime_type``.
    * ``extracted_text`` is the **parsed, human/LLM-readable text** for search /
      RAG. For a binary document it is NOT the file — it is the text pulled out
      of it. For a plain-text file the two round-trip to the same content.

    Do not alias ``content`` to ``extracted_text``: for binary documents that
    yields a corrupt download (plaintext served under an ``xlsx`` mimeType).

    Apps map those columns to DB columns through ordinary directives —
    ``id_from: file_id``, ``json_collapse`` for the EAV remainder, ``extract``
    to hoist sub-entities (e.g. message sender) etc. With ``content`` carried
    through, the wide row now fully round-trips the raw file.

    This reader is registered as :data:`binary <register_reader>` so the
    importer reads ``path.read_bytes()`` instead of UTF-8 ``read_text``.

    Args:
        source: The raw file bytes. ``str`` input is encoded as UTF-8.
        info: Source metadata, primarily :attr:`SourceInfo.filename` (used
            for extension / MIME detection and as the row's ``filename``).

    Raises:
        ImportError: If :mod:`mcp_files` is not installed. Apps that want the
            file_content reader must include ``mcp_files`` in their deps.
    """
    try:
        from mcp_files import extract_content, extract_metadata
        from mcp_files.models import SourceRef
    except ImportError as exc:  # pragma: no cover - exercised only on missing dep
        raise ImportError(
            "read_file_content requires mcp_files. Install with "
            "`uv add mcp-files` or include `mcp_files` in your project's deps."
        ) from exc

    data = source if isinstance(source, bytes) else source.encode("utf-8")
    ref = SourceRef(filename=info.filename)
    # ``extract_metadata`` computes the sha256 internally; reuse ``meta.sha256``
    # for both the ``sha256`` column and the ``"sha256:<hex>"`` file id rather
    # than re-hashing the same bytes two more times via ``file_id_for(data)`` +
    # ``compute_sha256(data)``. For a 2 MiB document this drops the per-file
    # hash cost from 3x to 1x.
    meta = extract_metadata(data, ref)
    extracted = extract_content(data, filename=info.filename, mime_type=meta.mime_type)
    return [
        {
            "file_id": f"sha256:{meta.sha256}",
            "filename": meta.filename,
            "extension": meta.extension,
            "mime_type": meta.mime_type,
            "size_bytes": meta.size_bytes,
            "sha256": meta.sha256,
            "content": base64.b64encode(data).decode("ascii"),
            "extracted_text": extracted.text,
            "extract_status": extracted.status,
            "extract_method": extracted.method,
            "warnings": list(extracted.warnings),
        }
    ]


# -----------------------------------------------------------------------------
# Registry
# -----------------------------------------------------------------------------

_READERS: dict[str, Reader | MultiEntityReader] = {
    "csv": read_csv,
    "json": read_json,
    "file_content": read_file_content,
}
_HEADER_READERS: dict[str, HeaderReader] = {
    "csv": read_csv_headers_text,
    "json": read_json_headers,
}
_MULTI_ENTITY: set[str] = set()
# Formats whose readers expect raw bytes (binary documents — PDF / DOCX /
# images / etc.). The importer reads ``path.read_bytes()`` instead of UTF-8
# ``read_text`` for these. ``file_content`` ships in this set by default.
_BINARY: set[str] = {"file_content"}


def register_reader(
    name: str,
    reader: Reader | MultiEntityReader,
    header_reader: HeaderReader | None = None,
    *,
    multi_entity: bool = False,
    binary: bool = False,
) -> None:
    """Register a named reader.

    Args:
        name: Format identifier (used in YAML ``sources[].format``).
        reader: Callable ``(content, SourceInfo) -> Records`` (or
            ``MultiEntityRecords``) that parses bytes/text into the IR.
            Single-entity readers return ``Records``; multi-entity readers
            return ``MultiEntityRecords`` (a ``dict[entity_name, Records]``).
        header_reader: Optional header-only fast path used by validation and
            header-signature entity detection (single-entity readers only).
            If omitted, the fallback parses the full source and inspects the
            first record's keys.
        multi_entity: When True, marks the reader as producing
            ``MultiEntityRecords`` and skips filename/header entity routing
            (the reader's output keys ARE the routing). Header readers are
            unused for multi-entity formats.
        binary: When True, the importer reads source files as raw bytes
            (``path.read_bytes()``) instead of UTF-8 text. Required for
            binary document formats (PDF / DOCX / images / archives). Apps
            that register custom binary formats should pass ``binary=True``
            and accept ``bytes`` in their reader.

    Threading & isolation
    ---------------------

    The registry is **process-global** and not isolated across threads or
    test workers (matching the stdlib ``codecs`` / matplotlib backend
    pattern). Register custom readers once at process startup, before any
    concurrent imports run; do not mutate the registry from worker threads
    or from a per-test fixture that runs under ``pytest-xdist``. If you
    need per-test isolation (e.g. each test asserts a different reader for
    the same ``name``), drive the engine through a parametrized factory
    that returns the reader directly rather than reaching through the
    global registry. Re-registering an existing name silently overwrites
    the previous binding — last write wins.
    """
    _READERS[name] = reader
    if header_reader is not None:
        _HEADER_READERS[name] = header_reader
    if multi_entity:
        _MULTI_ENTITY.add(name)
    else:
        _MULTI_ENTITY.discard(name)
    if binary:
        _BINARY.add(name)
    else:
        _BINARY.discard(name)


def get_reader(name: str) -> Reader | MultiEntityReader:
    """Return the reader registered under ``name``.

    Raises:
        KeyError: If no reader is registered for that format name.
    """
    try:
        return _READERS[name]
    except KeyError as exc:
        known = ", ".join(sorted(_READERS))
        raise KeyError(f"No reader registered for format {name!r}. Known: {known}") from exc


def get_header_reader(name: str) -> HeaderReader:
    """Return the header-reader for ``name``, falling back to the full reader.

    Only meaningful for single-entity readers — multi-entity readers don't
    participate in header-signature entity detection.
    """
    if name in _HEADER_READERS:
        return _HEADER_READERS[name]
    # Fallback: parse the full source and return the first record's keys.
    full_reader = get_reader(name)

    def _fallback(source: bytes | str, info: SourceInfo) -> list[str]:
        records = full_reader(source, info)
        if isinstance(records, dict):
            # Multi-entity reader — caller shouldn't be asking for headers, but
            # if it does, return the first entity's first record's keys.
            for entity_rows in records.values():
                if entity_rows:
                    return list(entity_rows[0])
            return []
        return list(records[0]) if records else []

    return _fallback


def is_multi_entity_reader(name: str) -> bool:
    """True when the reader registered under ``name`` emits
    ``MultiEntityRecords`` and bypasses filename/header entity routing.

    Unknown formats return False (the dispatcher will surface the unknown
    format error elsewhere via :func:`get_reader`)."""
    return name in _MULTI_ENTITY


def is_binary_reader(name: str) -> bool:
    """True when the reader registered under ``name`` expects raw bytes.

    The importer uses this to choose between ``path.read_bytes()`` and
    ``path.read_text(encoding="utf-8")`` per source file. Unknown formats
    return False (text mode, matching the historical default)."""
    return name in _BINARY


# -----------------------------------------------------------------------------
# Glob matching (Python 3.12-compatible)
# -----------------------------------------------------------------------------


def _glob_match(subject: str, pattern: str) -> bool:
    """Match ``subject`` against a glob ``pattern`` with full-path semantics.

    Equivalent to ``PurePosixPath(subject).full_match(pattern)`` introduced in
    Python 3.13, but implemented via :mod:`re` so it works on Python 3.12+.

    Rules:

    * ``*``   — zero or more characters excluding ``/``
    * ``?``   — exactly one character excluding ``/``
    * ``**/`` — zero or more complete path components (including none)
    * ``**``  — zero or more characters including ``/``

    Examples::

        _glob_match("foo.csv",     "*.csv")       # True  (root-level only)
        _glob_match("sub/foo.csv", "*.csv")       # False (crosses separator)
        _glob_match("foo.csv",     "**/*.csv")    # True  (zero dir components)
        _glob_match("sub/foo.csv", "**/*.csv")    # True
        _glob_match("foo.csv",     "*/**/*.csv")  # False (requires ≥1 dir)
        _glob_match("sub/foo.csv", "*/**/*.csv")  # True
    """
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern[i : i + 3] == "**/":
            # Zero or more path components followed by a slash.
            parts.append("(?:.*/)?")
            i += 3
        elif pattern[i : i + 2] == "**":
            # Trailing **: match the rest of the path.
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return bool(re.fullmatch("".join(parts), subject))


# -----------------------------------------------------------------------------
# Format dispatch
# -----------------------------------------------------------------------------


def resolve_format(
    filename: str,
    sources: list[Any] | None = None,
    *,
    default: str = DEFAULT_FORMAT,
    rel_path: str | None = None,
) -> str:
    """Pick a reader format for ``filename`` from a list of glob/format pairs.

    Each item in ``sources`` must expose ``.glob`` and ``.format`` attributes
    (e.g. :class:`~mcp_middleware.csv_engine.config.SourceMapping`). The first
    matching glob wins. When ``sources`` is empty/None, returns ``default``
    (preserves backward compatibility with CSV-only configurations).

    Args:
        filename: Bare filename (``path.name``).  Used as the match subject
                  for basename-only patterns (no ``/`` or ``**``), so patterns
                  like ``*.csv`` match files in sub-folders too.
        sources: List of :class:`SourceMapping`-like objects.
        default: Format to return when no source glob matches.
        rel_path: Path relative to the source directory
                  (e.g. ``"subdir/foo.csv"`` or ``"foo.csv"`` for root-level
                  files). Used as the match subject for path-aware patterns
                  (those containing ``/`` or ``**``).
    """
    if not sources:
        return default
    for src in sources:
        # Patterns with "/" or "**" are path-aware — match against the full
        # relative path so that "data/**/*.json" finds nested files.
        # Basename-only patterns like "*.csv" or "snapshot_*.json" must match
        # against just the filename so they still find files in sub-folders.
        if "/" in src.glob or "**" in src.glob:
            subject = rel_path if rel_path is not None else filename
        else:
            subject = filename
        if _glob_match(subject, src.glob):
            return src.format
    return default
