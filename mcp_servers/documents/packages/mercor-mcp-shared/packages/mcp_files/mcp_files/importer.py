"""Generic file import: discover sources, read bytes, build ImportedFile.

Three source shapes are supported, all yielding :class:`SourceRef`:

* **directory** — every file under a folder (optionally recursive)
* **CSV manifest** — one row per file; a path column is resolved against
  ``--files-root`` if given, else a sibling ``files/`` dir next to the CSV,
  else the CSV directory itself; a base64 ``data`` column enables inline bytes
* **inline** — a ``SourceRef`` whose ``path`` is ``"inline:<name>"`` with bytes
  carried in ``extra["data"]`` (base64)

``import_file`` / ``import_files`` then attach metadata + extracted content.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import logging
import tarfile
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path

from .extract import MAX_EXTRACTED_BYTES, extract_content
from .metadata import extract_metadata, file_id_for
from .mime import extension, guess_mime, sniff_office_zip
from .models import STATUS_SKIPPED, ExtractedContent, ImportedFile, SourceRef

log = logging.getLogger("mcp_files")

# Default recursion depth for ``follow_archives`` (top-level archive = depth 1).
# Bounds work for pathological "zip bomb" nesting; combined with the per-entry
# ``MAX_EXTRACTED_BYTES`` size cap this keeps nested discovery safe by default.
DEFAULT_MAX_ARCHIVE_DEPTH = 3

# Extensions that always indicate a container we should unpack.
_ARCHIVE_EXTS = frozenset({".zip", ".tar", ".gz", ".tgz"})

# CSV columns (in priority order) that may carry a path to the file bytes.
_PATH_COLUMNS = (
    "local_path",
    "file_path",
    "path",
    "filepath",
    "storage_path",
    "content_file",
    "content_path",
    "attachment_files",
)
_NAME_COLUMNS = ("filename", "name")
_MIME_COLUMNS = ("mime_type", "mimetype")

csv.field_size_limit(10_000_000)


def iter_directory(path: Path, *, recursive: bool = False) -> Iterator[SourceRef]:
    """Yield a :class:`SourceRef` for every file under ``path``."""
    if path.is_file():
        yield SourceRef(path.name, guess_mime(path.name, None), str(path.resolve()))
        return
    globber = path.rglob("*") if recursive else path.glob("*")
    for fp in sorted(globber):
        if fp.is_file() and not fp.name.startswith("."):
            yield SourceRef(fp.name, guess_mime(fp.name, None), str(fp.resolve()))


def _pick(row: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if row.get(key):
            return row[key]
    return None


def _resolve_manifest_path(rel: str, csv_path: Path, files_root: Path | None) -> str:
    p = Path(rel)
    if p.is_absolute():
        return str(p)
    candidates: list[Path] = []
    if files_root:
        candidates.append(files_root / p)
    else:
        candidates.append(csv_path.parent / "files" / p)
        candidates.append(csv_path.parent / p)
    chosen = next((c for c in candidates if c.is_file()), candidates[0])
    return str(chosen)


def iter_csv_manifest(csv_path: Path, files_root: Path | None = None) -> Iterator[SourceRef]:
    """Yield :class:`SourceRef` rows from a manifest CSV.

    Recognized columns: a name column (``filename``/``name``), optional MIME
    (``mime_type``/``mimetype``), a path column (see ``_PATH_COLUMNS``) or a
    base64 ``data`` column. All other columns pass through to ``extra``.
    """
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return
        for row in reader:
            filename = (_pick(row, _NAME_COLUMNS) or "").strip()
            if not filename:
                continue
            mime = (_pick(row, _MIME_COLUMNS) or "").strip()
            rel = _pick(row, _PATH_COLUMNS)
            skip = set(_NAME_COLUMNS) | set(_MIME_COLUMNS)
            extra = {k: v for k, v in row.items() if k not in skip and v}
            if rel:
                path_str = _resolve_manifest_path(rel, csv_path, files_root)
            elif row.get("data"):
                path_str = f"inline:{filename}"
            else:
                log.warning("skip manifest row without path/data: %s", filename)
                continue
            yield SourceRef(
                filename=filename,
                mime_type=mime,
                path=path_str,
                source_app=row.get("source_app") or "csv",
                extra=extra,
            )


def read_source_bytes(source: SourceRef) -> bytes | None:
    """Read the raw bytes for ``source`` (filesystem path or inline base64)."""
    if source.path.startswith("inline:"):
        data_b64 = source.extra.get("data") or ""
        if not data_b64:
            return None
        try:
            return base64.b64decode(data_b64)
        except (ValueError, binascii.Error):
            return None
    p = Path(source.path)
    if not p.is_file():
        log.warning("missing file: %s", p)
        return None
    return p.read_bytes()


def _looks_like_archive(filename: str, data: bytes) -> bool:
    """True when ``data`` is a real archive we should unpack.

    ZIP magic bytes also front docx/xlsx/pptx, so a bare ``PK`` payload is only
    treated as an archive when :func:`sniff_office_zip` does *not* recognize it
    as an Office Open XML document.
    """
    ext = extension(filename)
    if ext in {".tar", ".gz", ".tgz"} or data[:2] == b"\x1f\x8b":
        return True
    if ext == ".zip":
        return True
    if data[:4] == b"PK\x03\x04":
        return sniff_office_zip(data) is None
    return False


def _unpack_archive(data: bytes, filename: str) -> Iterator[tuple[str, bytes]]:
    """Yield ``(internal_path, bytes)`` for each file inside one archive level.

    Skips directories, dotfiles, and members larger than
    :data:`~mcp_files.extract.MAX_EXTRACTED_BYTES`. Internal paths are kept
    verbatim so callers can record provenance. Malformed archives yield nothing.
    """
    ext = extension(filename)
    is_tar = ext in {".tar", ".gz", ".tgz"} or (data[:2] == b"\x1f\x8b" and ext != ".zip")
    if is_tar:
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
                for member in tf.getmembers():
                    if not member.isfile() or member.size > MAX_EXTRACTED_BYTES:
                        continue
                    if Path(member.name).name.startswith("."):
                        continue
                    handle = tf.extractfile(member)
                    if handle is not None:
                        yield member.name, handle.read()
        except (tarfile.TarError, OSError):
            return
        return
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir() or info.file_size > MAX_EXTRACTED_BYTES:
                    continue
                name = Path(info.filename).name
                if not name or name.startswith("."):
                    continue
                yield info.filename, zf.read(info.filename)
    except (zipfile.BadZipFile, OSError):
        return


def iter_nested_files(
    source: SourceRef,
    data: bytes,
    *,
    max_depth: int = DEFAULT_MAX_ARCHIVE_DEPTH,
    _depth: int = 0,
) -> Iterator[tuple[SourceRef, bytes]]:
    """Recursively yield ``(SourceRef, bytes)`` for files nested in ``data``.

    When ``data`` is an archive, each contained file is yielded with a
    :class:`SourceRef` whose ``path`` is ``"<container_path>!<internal_path>"``
    and whose ``extra`` records ``archive_path`` / ``archive_internal_path`` /
    ``nested_depth`` for provenance. Archives nested inside archives are
    expanded up to ``max_depth`` levels. Non-archives yield nothing.
    """
    if _depth >= max_depth or not _looks_like_archive(source.filename, data):
        return
    for internal_path, nested_data in _unpack_archive(data, source.filename):
        name = Path(internal_path).name
        nested_source = SourceRef(
            filename=name,
            mime_type="",
            path=f"{source.path}!{internal_path}",
            source_app=source.source_app,
            extra={
                **source.extra,
                "archive_path": source.path,
                "archive_internal_path": internal_path,
                "nested_depth": _depth + 1,
            },
        )
        yield nested_source, nested_data
        yield from iter_nested_files(
            nested_source, nested_data, max_depth=max_depth, _depth=_depth + 1
        )


def import_file(
    data: bytes,
    source: SourceRef,
    *,
    extract: bool = True,
    retain_bytes: bool = False,
) -> ImportedFile:
    """Build an :class:`ImportedFile` from bytes + source (metadata + content)."""
    metadata = extract_metadata(data, source)
    if extract:
        content = extract_content(data, filename=source.filename, mime_type=metadata.mime_type)
    else:
        content = ExtractedContent("", "skipped", STATUS_SKIPPED, [])
    return ImportedFile(
        file_id=file_id_for(data),
        source=source,
        metadata=metadata,
        content=content,
        data=data if retain_bytes else None,
    )


def iter_sources(
    inputs: Iterable[str | Path | SourceRef],
    *,
    files_root: Path | None = None,
    recursive: bool = False,
) -> Iterator[SourceRef]:
    """Expand mixed inputs (paths, CSV manifests, SourceRefs) into SourceRefs."""
    for item in inputs:
        if isinstance(item, SourceRef):
            yield item
            continue
        path = Path(item)
        if path.suffix.lower() == ".csv" and path.is_file():
            yield from iter_csv_manifest(path, files_root)
        elif path.exists():
            yield from iter_directory(path, recursive=recursive)
        else:
            log.warning("skip missing input: %s", path)


def import_files(
    inputs: Iterable[str | Path | SourceRef],
    *,
    files_root: Path | None = None,
    recursive: bool = False,
    extract: bool = True,
    retain_bytes: bool = False,
    follow_archives: bool = False,
    max_archive_depth: int = DEFAULT_MAX_ARCHIVE_DEPTH,
) -> Iterator[ImportedFile]:
    """Import every file referenced by ``inputs``; skips unreadable sources.

    When ``follow_archives`` is set, each archive (zip/tar/gz) is also expanded
    and the nested files imported as their own :class:`ImportedFile` records
    (up to ``max_archive_depth`` levels), in addition to the archive itself.
    """
    for source in iter_sources(inputs, files_root=files_root, recursive=recursive):
        data = read_source_bytes(source)
        if data is None:
            continue
        yield import_file(data, source, extract=extract, retain_bytes=retain_bytes)
        if follow_archives:
            for nested_source, nested_data in iter_nested_files(
                source, data, max_depth=max_archive_depth
            ):
                yield import_file(
                    nested_data, nested_source, extract=extract, retain_bytes=retain_bytes
                )
