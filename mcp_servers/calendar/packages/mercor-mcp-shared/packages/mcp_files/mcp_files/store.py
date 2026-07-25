"""Storage abstraction for imported files.

``FileStore`` is the contract every app implements (or reuses) to persist
imported files. Two reference implementations ship here:

* :class:`InMemoryFileStore` — ephemeral, ideal for tests / one-shot pipelines.
* :class:`FilesystemFileStore` — durable layout under a root directory, with
  per-file metadata JSON, extracted text, and (optionally) raw blobs.

Apps with a database back their own ``FileStore`` (e.g. writing rows into
``drive_files`` / ``gmail_attachments``) while reusing the import + extract +
snapshot machinery unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import ExtractedContent, FileMetadata, ImportedFile, SourceRef


@runtime_checkable
class FileStore(Protocol):
    """Persistence contract for imported files."""

    def upsert(self, imported: ImportedFile) -> None:
        """Insert or replace a file (idempotent by ``file_id``)."""
        ...

    def iter_files(self) -> Iterator[ImportedFile]:
        """Yield every stored file."""
        ...

    def read_bytes(self, file_id: str) -> bytes | None:
        """Return raw bytes for ``file_id`` when retained, else ``None``."""
        ...


class InMemoryFileStore:
    """Dict-backed :class:`FileStore` (keeps bytes in memory)."""

    def __init__(self) -> None:
        self._files: dict[str, ImportedFile] = {}

    def upsert(self, imported: ImportedFile) -> None:
        self._files[imported.file_id] = imported

    def iter_files(self) -> Iterator[ImportedFile]:
        yield from self._files.values()

    def read_bytes(self, file_id: str) -> bytes | None:
        item = self._files.get(file_id)
        return item.data if item else None

    def __len__(self) -> int:
        return len(self._files)


class FilesystemFileStore:
    """Durable :class:`FileStore` writing a portable on-disk layout.

    Layout under ``root``::

        root/
          manifest.jsonl              # one JSON line per file (audit trail)
          by_id/<safe_id>/meta.json   # SourceRef + FileMetadata + content meta
          by_id/<safe_id>/content.txt # extracted text (when non-empty)
          blobs/<safe_id>/<filename>  # raw bytes (when retained)

    ``<safe_id>`` strips the ``"sha256:"`` prefix so it is filesystem-safe.
    """

    def __init__(self, root: Path | str, *, store_blobs: bool = True) -> None:
        self.root = Path(root)
        self.store_blobs = store_blobs
        self._manifest = self.root / "manifest.jsonl"

    @staticmethod
    def _safe_id(file_id: str) -> str:
        return file_id.split(":", 1)[-1]

    def upsert(self, imported: ImportedFile) -> None:
        safe = self._safe_id(imported.file_id)
        doc_dir = self.root / "by_id" / safe
        doc_dir.mkdir(parents=True, exist_ok=True)

        meta = {
            "file_id": imported.file_id,
            "source": asdict(imported.source),
            "metadata": asdict(imported.metadata),
            "content": {
                "method": imported.content.method,
                "status": imported.content.status,
                "warnings": imported.content.warnings,
                "char_count": len(imported.content.text),
            },
        }
        (doc_dir / "meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if imported.content.text:
            (doc_dir / "content.txt").write_text(imported.content.text, encoding="utf-8")

        if self.store_blobs and imported.data is not None:
            blob_dir = self.root / "blobs" / safe
            blob_dir.mkdir(parents=True, exist_ok=True)
            (blob_dir / imported.metadata.filename).write_bytes(imported.data)

        self.root.mkdir(parents=True, exist_ok=True)
        with self._manifest.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "file_id": imported.file_id,
                        "filename": imported.metadata.filename,
                        "mime_type": imported.metadata.mime_type,
                        "size_bytes": imported.metadata.size_bytes,
                        "sha256": imported.metadata.sha256,
                        "status": imported.content.status,
                        "method": imported.content.method,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    def iter_files(self) -> Iterator[ImportedFile]:
        by_id = self.root / "by_id"
        if not by_id.is_dir():
            return
        for meta_path in sorted(by_id.glob("*/meta.json")):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            content_path = meta_path.parent / "content.txt"
            text = content_path.read_text(encoding="utf-8") if content_path.is_file() else ""
            source = SourceRef(**meta["source"])
            metadata = FileMetadata(**meta["metadata"])
            content_meta = meta.get("content", {})
            content = ExtractedContent(
                text=text,
                method=content_meta.get("method", ""),
                status=content_meta.get("status", ""),
                warnings=content_meta.get("warnings", []),
            )
            yield ImportedFile(
                file_id=meta["file_id"],
                source=source,
                metadata=metadata,
                content=content,
                data=self.read_bytes(meta["file_id"]),
            )

    def read_bytes(self, file_id: str) -> bytes | None:
        blob_dir = self.root / "blobs" / self._safe_id(file_id)
        if not blob_dir.is_dir():
            return None
        for child in blob_dir.iterdir():
            if child.is_file():
                return child.read_bytes()
        return None
