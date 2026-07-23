"""Core data models for generic file import / metadata / content extraction.

These dataclasses are intentionally storage-agnostic and domain-agnostic so any
MCP server (Gmail, Drive, Zoho, Teams, …) can reuse the same shapes when it
imports attachments / documents, extracts metadata + text, and round-trips
files through a populate → snapshot cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Extraction status vocabulary shared by ``ExtractedContent.status``.
STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


@dataclass
class SourceRef:
    """Pointer to a single importable file.

    A ``SourceRef`` describes *where* a file comes from without committing to
    how its bytes are read. ``path`` is either a filesystem path or the
    sentinel ``"inline:<filename>"`` when bytes are carried in ``extra["data"]``
    as base64.

    Attributes:
        filename: Display / detection name (used for extension + MIME hints).
        mime_type: Optional caller-provided MIME hint (may be empty).
        path: Filesystem path, or ``"inline:<filename>"`` for inline bytes.
        source_app: Logical origin (``"filesystem"``, ``"gmail"``, ``"csv"`` …).
        extra: Arbitrary passthrough metadata echoed into outputs.
    """

    filename: str
    mime_type: str = ""
    path: str = ""
    source_app: str = "filesystem"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class FileMetadata:
    """Structural metadata derived from a file's bytes + source.

    Attributes:
        filename: File display name.
        extension: Lower-cased suffix including the dot (``".pdf"``); ``""`` if none.
        mime_type: Resolved MIME type (extension + magic-byte sniffing).
        size_bytes: Raw byte length.
        sha256: Hex SHA-256 of the bytes (also used to derive ``ImportedFile.file_id``).
        source_app: Logical origin, copied from the source.
        extra: Passthrough metadata copied from the source.
    """

    filename: str
    extension: str
    mime_type: str
    size_bytes: int
    sha256: str
    source_app: str = "filesystem"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractedContent:
    """Result of extracting plain text from a file's bytes.

    Attributes:
        text: Extracted plain text (possibly truncated; empty on failure).
        method: Extractor that produced the text (e.g. ``"application/pdf"``).
        status: One of ``ok`` / ``empty`` / ``failed`` / ``skipped``.
        warnings: Non-fatal diagnostics (missing optional deps, OCR notes …).
    """

    text: str
    method: str
    status: str
    warnings: list[str] = field(default_factory=list)

    @property
    def is_ok(self) -> bool:
        return self.status == STATUS_OK and bool(self.text)


@dataclass
class ImportedFile:
    """A fully imported file: identity + metadata + extracted content.

    Attributes:
        file_id: Stable id, ``"sha256:<hex>"`` of the bytes (content-addressed).
        source: Originating :class:`SourceRef`.
        metadata: Derived :class:`FileMetadata`.
        content: Derived :class:`ExtractedContent` (may be ``status="skipped"``).
        data: Raw bytes, retained only when the caller asks for it.
    """

    file_id: str
    source: SourceRef
    metadata: FileMetadata
    content: ExtractedContent
    data: bytes | None = None
