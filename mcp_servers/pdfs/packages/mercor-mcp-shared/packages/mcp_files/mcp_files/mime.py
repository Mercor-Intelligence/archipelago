"""MIME-type detection: extension map + magic-byte sniffing.

App-agnostic helpers shared by the importer / extractor. Detection order is:
caller hint → extension table → stdlib ``mimetypes`` → magic-byte sniff for
ambiguous ``application/octet-stream`` / ``application/zip`` payloads.
"""

from __future__ import annotations

import io
import mimetypes
import zipfile
from pathlib import Path

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

_EXT_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".ics": "text/calendar",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".xml": "application/xml",
    ".docx": DOCX_MIME,
    ".xlsx": XLSX_MIME,
    ".pptx": PPTX_MIME,
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".ppt": "application/vnd.ms-powerpoint",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".eml": "message/rfc822",
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".svg": "image/svg+xml",
}

# MIME types whose payload is safe to decode as plain text.
TEXT_MIMES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/tab-separated-values",
        "text/calendar",
        "application/ics",
        "application/json",
        "application/xml",
        "text/xml",
    }
)


def extension(filename: str) -> str:
    """Return the lower-cased suffix (including dot), or ``""`` when absent."""
    return Path(filename.strip().lower()).suffix.lower()


def guess_mime(filename: str, mime_hint: str | None = None) -> str:
    """Resolve a MIME type from a caller hint, extension, or stdlib guess."""
    if mime_hint and mime_hint.strip() and mime_hint.strip() != "application/octet-stream":
        return mime_hint.strip().lower()
    ext = extension(filename)
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    guessed, _ = mimetypes.guess_type(filename)
    return (guessed or "application/octet-stream").lower()


def sniff_office_zip(data: bytes) -> str | None:
    """Detect docx/xlsx/pptx from ZIP central-directory member names."""
    if not data.startswith(b"PK\x03\x04"):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile:
        return None
    if "word/document.xml" in names:
        return DOCX_MIME
    if "xl/workbook.xml" in names:
        return XLSX_MIME
    if any(n.startswith("ppt/presentation") for n in names):
        return PPTX_MIME
    return None


def resolve_mime(filename: str, mime_hint: str | None, data: bytes) -> str:
    """Resolve MIME with magic-byte sniffing for ambiguous container types."""
    mime = guess_mime(filename, mime_hint)
    if mime in {"application/octet-stream", "application/zip"}:
        sniffed = sniff_office_zip(data)
        if sniffed:
            return sniffed
    return mime
