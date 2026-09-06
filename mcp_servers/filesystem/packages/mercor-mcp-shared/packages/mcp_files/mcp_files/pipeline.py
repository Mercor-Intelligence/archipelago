"""Populate + snapshot orchestration for generic files.

* :func:`populate_files` — import files (dir / CSV manifest / inline) into any
  :class:`~mcp_files.store.FileStore`, attaching metadata + extracted text.
* :func:`snapshot_files` — export a store back to a portable, round-trippable
  drop: a ``files.csv`` manifest + a sibling ``files/`` payload dir +
  ``content/`` extracted text. The output re-imports cleanly via
  :func:`~mcp_files.importer.iter_csv_manifest`.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .importer import import_files
from .models import ImportedFile, SourceRef
from .store import FileStore

_SNAPSHOT_COLUMNS = (
    "file_id",
    "filename",
    "mime_type",
    "extension",
    "size_bytes",
    "sha256",
    "source_app",
    "content_file",
    "extract_status",
    "extract_method",
)


@dataclass
class PopulateStats:
    imported: int = 0
    extracted_ok: int = 0
    skipped: int = 0


@dataclass
class SnapshotStats:
    files: int = 0
    blobs: int = 0


def populate_files(
    inputs: Iterable[str | Path | SourceRef],
    store: FileStore,
    *,
    files_root: Path | None = None,
    recursive: bool = False,
    extract: bool = True,
    retain_bytes: bool = True,
    follow_archives: bool = False,
) -> PopulateStats:
    """Import ``inputs`` into ``store`` (metadata + optional text extraction).

    With ``follow_archives`` set, files nested inside archives are imported as
    their own records too (see :func:`~mcp_files.importer.import_files`).
    """
    stats = PopulateStats()
    for imported in import_files(
        inputs,
        files_root=files_root,
        recursive=recursive,
        extract=extract,
        retain_bytes=retain_bytes,
        follow_archives=follow_archives,
    ):
        store.upsert(imported)
        stats.imported += 1
        if imported.content.is_ok:
            stats.extracted_ok += 1
        else:
            stats.skipped += 1
    return stats


def _payload_relpath(imported: ImportedFile) -> str:
    """Pick a stable, filesystem-safe relative path for a file's blob."""
    content_file = imported.metadata.extra.get("content_file")
    if content_file:
        rel = Path(str(content_file))
        if not rel.is_absolute():
            parts = [p for p in rel.parts if p not in ("", ".", "..")]
            if parts:
                return str(Path(*parts))
    safe = imported.file_id.split(":", 1)[-1]
    return f"{safe}/{imported.metadata.filename}"


def snapshot_files(store: FileStore, out_dir: Path | str) -> SnapshotStats:
    """Export ``store`` to ``out_dir`` as a round-trippable files drop."""
    out = Path(out_dir)
    files_dir = out / "files"
    content_dir = out / "content"
    out.mkdir(parents=True, exist_ok=True)

    stats = SnapshotStats()
    rows: list[dict[str, str]] = []
    for imported in store.iter_files():
        rel = _payload_relpath(imported)
        data = imported.data if imported.data is not None else store.read_bytes(imported.file_id)
        if data is not None:
            target = files_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            stats.blobs += 1

        if imported.content.text:
            safe = imported.file_id.split(":", 1)[-1]
            content_path = content_dir / f"{safe}.txt"
            content_path.parent.mkdir(parents=True, exist_ok=True)
            content_path.write_text(imported.content.text, encoding="utf-8")

        rows.append(
            {
                "file_id": imported.file_id,
                "filename": imported.metadata.filename,
                "mime_type": imported.metadata.mime_type,
                "extension": imported.metadata.extension,
                "size_bytes": str(imported.metadata.size_bytes),
                "sha256": imported.metadata.sha256,
                "source_app": imported.metadata.source_app,
                "content_file": rel,
                "extract_status": imported.content.status,
                "extract_method": imported.content.method,
            }
        )
        stats.files += 1

    with (out / "files.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(_SNAPSHOT_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)

    return stats
