"""Metadata extraction: content-addressed id, size, MIME, extension."""

from __future__ import annotations

import hashlib

from .mime import extension, resolve_mime
from .models import FileMetadata, SourceRef


def compute_sha256(data: bytes) -> str:
    """Return the hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def file_id_for(data: bytes) -> str:
    """Return a stable content-addressed id (``"sha256:<hex>"``)."""
    return f"sha256:{compute_sha256(data)}"


def extract_metadata(data: bytes, source: SourceRef) -> FileMetadata:
    """Derive :class:`FileMetadata` from raw bytes + a :class:`SourceRef`.

    MIME is resolved from the source hint, the filename extension, and
    magic-byte sniffing (so a mislabeled ``.bin`` docx is still detected).
    """
    return FileMetadata(
        filename=source.filename,
        extension=extension(source.filename),
        mime_type=resolve_mime(source.filename, source.mime_type or None, data),
        size_bytes=len(data),
        sha256=compute_sha256(data),
        source_app=source.source_app,
        extra=dict(source.extra),
    )
