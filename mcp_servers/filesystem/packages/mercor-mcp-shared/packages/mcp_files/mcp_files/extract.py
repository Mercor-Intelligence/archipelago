"""Generic, best-effort content (plain-text) extraction from file bytes.

App-agnostic. Supported formats degrade gracefully when an optional dependency
is missing (the result carries ``status`` + ``warnings`` rather than raising):

* PDF (``pypdf``)
* Office Open XML: docx / xlsx / pptx (stdlib ``zipfile`` + XML)
* legacy ``.doc`` (LibreOffice / ``olefile`` / ``antiword`` / ``catdoc``)
* legacy ``.xls`` / ``.ppt`` (LibreOffice headless)
* text / markdown / csv / tsv / json / xml / html
* calendar ``.ics`` (``icalendar``) and email ``.eml`` (stdlib ``email``)
* images via OCR (``pytesseract`` + ``Pillow`` + the ``tesseract`` binary)
* ``.zip`` / ``.tar`` / ``.gz`` archives (one-level unpack)
"""

from __future__ import annotations

import email
import io
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

from .mime import (
    DOCX_MIME,
    PPTX_MIME,
    TEXT_MIMES,
    XLSX_MIME,
    extension,
    guess_mime,
    resolve_mime,
)
from .models import STATUS_EMPTY, STATUS_FAILED, STATUS_OK, ExtractedContent

# Cap extracted text so indexing/storage stays bounded (2 MiB).
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024

# Maximum archive recursion depth (prevents zip-bomb / deeply nested archives
# from consuming unbounded stack/memory during recursive extract_content calls).
MAX_ARCHIVE_DEPTH = 3

_NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_NS_SS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _HTMLTextExtractor(HTMLParser):
    _DROP = frozenset({"script", "style", "noscript", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._drop = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._DROP:
            self._drop += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._DROP and self._drop:
            self._drop -= 1

    def handle_data(self, data: str) -> None:
        if self._drop == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._chunks)).strip()


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _truncate(text: str) -> str:
    raw = text.encode("utf-8", errors="ignore")
    if len(raw) <= MAX_EXTRACTED_BYTES:
        return text
    return raw[:MAX_EXTRACTED_BYTES].decode("utf-8", errors="ignore")


def _find_soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    for candidate in (
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/bin/libreoffice",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def _convert_office_bytes_to_txt(data: bytes, suffix: str) -> tuple[str, str]:
    soffice = _find_soffice()
    if not soffice:
        return "", ""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        inp = td_path / f"input{suffix}"
        inp.write_bytes(data)
        try:
            proc = subprocess.run(
                [soffice, "--headless", "--convert-to", "txt", "--outdir", str(td_path), str(inp)],
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "", ""
        if proc.returncode != 0:
            return "", ""
        out = td_path / "input.txt"
        if out.is_file():
            return out.read_text(encoding="utf-8", errors="replace").strip(), "libreoffice"
    return "", ""


def _run_cli_on_bytes(data: bytes, suffix: str, command: str) -> str:
    exe = shutil.which(command)
    if not exe:
        return ""
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / f"input{suffix}"
        inp.write_bytes(data)
        try:
            proc = subprocess.run([exe, str(inp)], capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if proc.returncode != 0:
            return ""
        for stream in (proc.stdout, proc.stderr):
            text = stream.decode("utf-8", errors="replace").strip()
            if text:
                return text
    return ""


# ---------------------------------------------------------------------------
# Per-format extractors (bytes -> text)
# ---------------------------------------------------------------------------


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception:
        return ""
    parts: list[str] = []
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        if t:
            parts.append(t)
    return re.sub(r"[ \t]+", " ", "\n".join(parts)).strip()


def _extract_html(data: bytes) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(_decode_text(data))
        parser.close()
    except Exception:
        return _decode_text(data)
    return parser.text()


def _open_zip(data: bytes) -> zipfile.ZipFile | None:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None


def _xml_texts(zf: zipfile.ZipFile, member: str, tag: str) -> list[str]:
    try:
        raw = zf.read(member)
    except KeyError:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    return [el.text for el in root.iter(tag) if el.text]


def _extract_docx(data: bytes) -> str:
    zf = _open_zip(data)
    if zf is None:
        return ""
    parts: list[str] = []
    with zf:
        for member in ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml"):
            parts.extend(t.strip() for t in _xml_texts(zf, member, f"{_NS_W}t") if t.strip())
        for name in zf.namelist():
            if name.startswith(("word/header", "word/footer")):
                parts.extend(t.strip() for t in _xml_texts(zf, name, f"{_NS_W}t") if t.strip())
    return " ".join(parts).strip()


def _extract_xlsx(data: bytes) -> str:
    zf = _open_zip(data)
    if zf is None:
        return ""
    parts: list[str] = []
    with zf:
        parts.extend(
            t.strip()
            for t in _xml_texts(zf, "xl/sharedStrings.xml", f"{_NS_SS}t")
            if t and t.strip()
        )
        for name in zf.namelist():
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                parts.extend(t.strip() for t in _xml_texts(zf, name, f"{_NS_SS}v") if t.strip())
                parts.extend(t.strip() for t in _xml_texts(zf, name, f"{_NS_SS}t") if t.strip())
    return " ".join(parts).strip()


def _extract_pptx(data: bytes) -> str:
    zf = _open_zip(data)
    if zf is None:
        return ""
    parts: list[str] = []
    with zf:
        for name in zf.namelist():
            is_slide = name.startswith("ppt/slides/slide")
            is_notes = name.startswith("ppt/notesSlides/notesSlide")
            if (is_slide or is_notes) and name.endswith(".xml"):
                parts.extend(t.strip() for t in _xml_texts(zf, name, f"{_NS_A}t") if t.strip())
    return " ".join(parts).strip()


def _extract_csv(data: bytes) -> str:
    import csv

    text = _decode_text(data)
    rows: list[str] = []
    try:
        for i, row in enumerate(csv.reader(io.StringIO(text))):
            if i > 5000:
                rows.append("…")
                break
            rows.append(" | ".join(cell.strip() for cell in row if cell.strip()))
    except csv.Error:
        return text
    return "\n".join(rows).strip()


def _extract_ics(data: bytes) -> str:
    text = _decode_text(data)
    try:
        from icalendar import Calendar
    except ImportError:
        return text
    parts: list[str] = []
    try:
        cal = Calendar.from_ical(text)
        for component in cal.walk():
            if component.name not in ("VEVENT", "VTODO", "VJOURNAL"):
                continue
            block: list[str] = [component.name]
            keys = ("SUMMARY", "DESCRIPTION", "LOCATION", "UID", "ORGANIZER", "ATTENDEE", "STATUS")
            for key in keys:
                val = component.get(key)
                if val:
                    block.append(f"{key}: {val}")
            parts.append("\n".join(block))
    except Exception:
        return text
    return "\n\n".join(parts).strip() or text


def _extract_eml(data: bytes) -> str:
    msg = email.message_from_bytes(data)
    parts: list[str] = []
    for hdr in ("Subject", "From", "To", "Cc", "Date"):
        if msg.get(hdr):
            parts.append(f"{hdr}: {msg.get(hdr)}")
    if msg.is_multipart():
        for part in msg.walk():
            if (part.get_content_type() or "").lower().startswith("text/plain"):
                raw = part.get_payload(decode=True)
                parts.append(_decode_text(raw if isinstance(raw, bytes) else b""))
    else:
        raw = msg.get_payload(decode=True)
        parts.append(_decode_text(raw if isinstance(raw, bytes) else b""))
    return "\n\n".join(p for p in parts if p.strip()).strip()


def _extract_image_ocr(data: bytes) -> tuple[str, list[str]]:
    warnings: list[str] = []
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return "", ["OCR requires Pillow (pip install Pillow)"]
    try:
        import pytesseract
    except ImportError:
        return "", ["OCR requires pytesseract (pip install pytesseract)"]
    if not shutil.which("tesseract"):
        candidates = (
            "/opt/homebrew/bin/tesseract",
            "/usr/local/bin/tesseract",
            "/usr/bin/tesseract",
        )
        for candidate in candidates:
            if Path(candidate).is_file():
                pytesseract.pytesseract.tesseract_cmd = candidate
                break
        else:
            return "", ["Tesseract binary not found (e.g. brew install tesseract)"]
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.grayscale(img)
        img = ImageOps.autocontrast(img)
        width, height = img.size
        if width < 900:
            scale = max(2, 900 // max(width, 1))
            img = img.resize((width * scale, height * scale), Image.Resampling.LANCZOS)
        text = (pytesseract.image_to_string(img) or "").strip()
    except Exception as exc:
        return "", [f"OCR failed: {exc}"]
    if not text:
        warnings.append("OCR returned no text")
    return text, warnings


def _extract_legacy_doc(data: bytes) -> tuple[str, str, list[str]]:
    text, method = _convert_office_bytes_to_txt(data, ".doc")
    if text:
        return text, method, []
    for cmd in ("antiword", "catdoc"):
        text = _run_cli_on_bytes(data, ".doc", cmd)
        if text:
            return text, cmd, []
    return (
        "",
        "legacy_doc_failed",
        ["Legacy .doc: install LibreOffice (soffice), antiword, or catdoc for extraction"],
    )


def _extract_archive(data: bytes, filename: str) -> tuple[str, list[tuple[str, bytes]]]:
    nested: list[tuple[str, bytes]] = []
    ext = extension(filename)
    if ext == ".zip" or data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    if info.is_dir() or info.file_size > MAX_EXTRACTED_BYTES:
                        continue
                    name = Path(info.filename).name
                    if name and not name.startswith("."):
                        nested.append((name, zf.read(info.filename)))
        except zipfile.BadZipFile:
            pass
    elif ext in {".tar", ".gz", ".tgz"} or data[:2] == b"\x1f\x8b":
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
                for member in tf.getmembers():
                    if not member.isfile() or member.size > MAX_EXTRACTED_BYTES:
                        continue
                    name = Path(member.name).name
                    f = tf.extractfile(member)
                    if name and f:
                        nested.append((name, f.read()))
        except tarfile.TarError:
            pass
    summary = f"Archive {filename} contains: {', '.join(n for n, _ in nested[:50])}"
    return summary, nested


_BYTES_EXTRACTORS: dict[str, Callable[[bytes], str]] = {
    "application/pdf": _extract_pdf,
    "text/html": _extract_html,
    "application/xhtml+xml": _extract_html,
    DOCX_MIME: _extract_docx,
    XLSX_MIME: _extract_xlsx,
    PPTX_MIME: _extract_pptx,
    "text/csv": _extract_csv,
    "text/tab-separated-values": _extract_csv,
    "text/calendar": _extract_ics,
    "application/ics": _extract_ics,
    "message/rfc822": _extract_eml,
}


def extract_content(
    data: bytes, *, filename: str, mime_type: str | None = None, _depth: int = 0
) -> ExtractedContent:
    """Extract plain text from ``data``; never raises (errors → status/warnings)."""
    mime = resolve_mime(filename, mime_type, data)
    ext = extension(filename)
    warnings: list[str] = []

    if mime.startswith("image/") or ext in _IMAGE_EXTS:
        text, ocr_warnings = _extract_image_ocr(data)
        status = STATUS_OK if text else STATUS_EMPTY
        method = "ocr" if text else "image_stub"
        return ExtractedContent(_truncate(text), method, status, ocr_warnings)

    if mime in {"application/zip"} or ext in {".zip", ".tar", ".gz", ".tgz", ".rar"}:
        if _depth >= MAX_ARCHIVE_DEPTH:
            return ExtractedContent(
                "",
                "archive_depth_limit",
                STATUS_EMPTY,
                [f"Archive nesting limit ({MAX_ARCHIVE_DEPTH}) reached; skipping {filename}"],
            )
        summary, nested = _extract_archive(data, filename)
        supported_exts = {".zip", ".tar", ".gz", ".tgz"}
        supported_mimes = {"application/zip"}
        if not nested and ext not in supported_exts and mime not in supported_mimes:
            # Unsupported or unreadable archive format (e.g. .rar).
            return ExtractedContent(
                "",
                "archive_unsupported",
                STATUS_EMPTY,
                [f"Unsupported archive format: {ext or mime}"],
            )
        texts = [summary]
        for nname, ndata in nested[:30]:
            sub = extract_content(
                ndata, filename=nname, mime_type=guess_mime(nname, None), _depth=_depth + 1
            )
            if sub.text:
                texts.append(f"--- {nname} ---\n{sub.text}")
            for w in sub.warnings:
                warnings.append(f"{nname}: {w}")
        joined = "\n\n".join(texts).strip() if nested else ""
        status = STATUS_OK if joined and nested else STATUS_EMPTY
        return ExtractedContent(_truncate(joined), "archive_unpack", status, warnings)

    if mime == "application/msword" or ext == ".doc":
        text, method, doc_warnings = _extract_legacy_doc(data)
        status = STATUS_OK if text else STATUS_EMPTY
        return ExtractedContent(_truncate(text), method, status, doc_warnings)

    if mime in {"application/vnd.ms-excel", "application/vnd.ms-powerpoint"}:
        suffix = ".xls" if "excel" in mime else ".ppt"
        text, method = _convert_office_bytes_to_txt(data, suffix)
        if text:
            return ExtractedContent(_truncate(text), method or "libreoffice", STATUS_OK, warnings)
        return ExtractedContent(
            "",
            "legacy_office_stub",
            STATUS_EMPTY,
            [f"Legacy {suffix} requires LibreOffice headless (soffice) for extraction"],
        )

    handler = _BYTES_EXTRACTORS.get(mime)
    if handler is not None:
        try:
            text = _truncate(handler(data))
        except Exception as exc:
            return ExtractedContent("", mime, STATUS_FAILED, [str(exc)])
        return ExtractedContent(text, mime, STATUS_OK if text else STATUS_EMPTY, warnings)

    if mime.startswith("text/") or mime in TEXT_MIMES:
        return ExtractedContent(_truncate(_decode_text(data)), mime, STATUS_OK, warnings)

    text = _decode_text(data)
    printable = sum(c.isprintable() or c in "\n\r\t" for c in text) / max(1, len(text))
    if printable > 0.9 and text:
        return ExtractedContent(_truncate(text), "utf8_fallback", STATUS_OK, warnings)

    return ExtractedContent("", "unsupported", STATUS_EMPTY, [f"No extractor for mime={mime}"])
