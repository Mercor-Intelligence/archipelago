"""Text extraction for keyword indexing, one reader per file type.

Office files are zip archives of XML, read with the standard library. PDF is the
only type that needs a third-party reader (pypdfium2), imported only when a PDF
is actually read. Each reader yields ``Section(location, text)`` so a search hit
can name its page, slide, sheet or line range.
"""

import codecs
import posixpath
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from tools.eml_reader import EML_MAX_BYTES, render_eml

LINES_PER_SECTION = 100
# One file can't use more than this much of the index's text budget.
MAX_CHARS_PER_FILE = 20 * 1024 * 1024
# Long text is indexed in rows of about this size, so no single row is huge.
MAX_SECTION_CHARS = 20_000
# An Office part that unpacks past this is treated as a compression bomb.
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
_BINARY_SNIFF_BYTES = 8192
PARAGRAPHS_PER_SECTION = 40
ROWS_PER_SECTION = 100

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


@dataclass(frozen=True)
class Section:
    location: str | None
    text: str


class ExtractionError(Exception):
    """The file could not be read; the message says why."""


class NoTextError(ExtractionError):
    """The file was read but holds no text (e.g. a scanned PDF)."""


class TimeLimitReached(Exception):
    """The per-file time limit ran out; sections yielded so far are kept."""


class TextLimitReached(Exception):
    """The text limit was hit before the end of the file; sections yielded so far are kept."""


class _Deadline:
    def __init__(self, seconds: float):
        self._end = time.monotonic() + seconds

    def check(self) -> None:
        if time.monotonic() > self._end:
            raise TimeLimitReached


# --------------------------------------------------------------------------- text


def _decode(raw: bytes, *, cut: bool) -> str:
    try:
        # A read cut short can end mid-character; drop that tail instead of failing.
        return codecs.getincrementaldecoder("utf-8-sig")().decode(raw, final=not cut)
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1252")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _read_text(path: Path, deadline: _Deadline, max_chars: int) -> Iterator[Section]:
    with path.open("rb") as handle:
        raw = handle.read(max_chars * 4)
    if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
        raise ExtractionError("binary content, not text")
    cut = path.stat().st_size > len(raw)
    decoded = _decode(raw, cut=cut)
    truncated = cut or len(decoded) > max_chars
    lines = decoded[:max_chars].splitlines()
    for start in range(0, len(lines), LINES_PER_SECTION):
        deadline.check()
        chunk = lines[start : start + LINES_PER_SECTION]
        end = start + len(chunk)
        yield Section(f"lines {start + 1}-{end}", "\n".join(chunk))
    if truncated:
        raise TextLimitReached


def _read_eml(path: Path, deadline: _Deadline, max_chars: int) -> Iterator[Section]:
    if path.stat().st_size > EML_MAX_BYTES:
        raise ExtractionError("email is larger than the 25 MB read limit")
    try:
        text = render_eml(path.read_bytes())
    except ValueError as exc:
        raise ExtractionError(str(exc)) from exc
    deadline.check()
    yield Section(None, text[:max_chars])
    if len(text) > max_chars:
        raise TextLimitReached


# ------------------------------------------------------------------------- office


def _open_zip(path: Path) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ExtractionError(f"not a valid Office file ({exc})") from exc
    oversized = [
        i.filename for i in archive.infolist() if i.file_size > MAX_UNPACKED_BYTES
    ]
    if oversized:
        archive.close()
        raise ExtractionError(
            f"{oversized[0]} unpacks to more than {MAX_UNPACKED_BYTES // 1024 // 1024} MB"
        )
    return archive


def _parse_member(archive: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        with archive.open(name) as member:
            return ET.parse(member).getroot()
    except KeyError:
        return None
    except ET.ParseError as exc:
        raise ExtractionError(f"broken XML in {name} ({exc})") from exc


def _relationship_targets(
    archive: zipfile.ZipFile, rels_name: str, base_dir: str
) -> dict[str, str]:
    root = _parse_member(archive, rels_name)
    if root is None:
        return {}
    targets: dict[str, str] = {}
    for rel in root.iter(f"{_REL}Relationship"):
        target = rel.get("Target", "")
        if target.startswith("/"):
            resolved = target.lstrip("/")
        else:
            resolved = posixpath.normpath(posixpath.join(base_dir, target))
        targets[rel.get("Id", "")] = resolved
    return targets


def _read_docx(path: Path, deadline: _Deadline, max_chars: int) -> Iterator[Section]:
    with _open_zip(path) as archive:
        if "word/document.xml" not in archive.namelist():
            raise ExtractionError("not a Word document (word/document.xml missing)")
        paragraphs: list[str] = []
        first = 1
        count = 0
        chars = 0
        with archive.open("word/document.xml") as member:
            try:
                for _event, elem in ET.iterparse(member, events=("end",)):
                    if elem.tag != f"{_W}p":
                        continue
                    text = "".join(t.text or "" for t in elem.iter(f"{_W}t")).strip()
                    elem.clear()
                    if not text:
                        continue
                    count += 1
                    paragraphs.append(text)
                    chars += len(text)
                    if len(paragraphs) == PARAGRAPHS_PER_SECTION:
                        yield Section(
                            f"paragraphs {first}-{count}", "\n".join(paragraphs)
                        )
                        paragraphs, first = [], count + 1
                        deadline.check()
                    if chars >= max_chars:
                        break
            except ET.ParseError as exc:
                raise ExtractionError(
                    f"broken XML in word/document.xml ({exc})"
                ) from exc
        if paragraphs:
            yield Section(f"paragraphs {first}-{count}", "\n".join(paragraphs))
        if chars >= max_chars:
            raise TextLimitReached


def _slide_names(archive: zipfile.ZipFile) -> list[str]:
    presentation = _parse_member(archive, "ppt/presentation.xml")
    targets = _relationship_targets(archive, "ppt/_rels/presentation.xml.rels", "ppt")
    names = archive.namelist()
    if presentation is not None and targets:
        ordered = [
            targets.get(slide.get(f"{_R}id", ""), "")
            for slide in presentation.iter(f"{_P}sldId")
        ]
        ordered = [name for name in ordered if name in names]
        if ordered:
            return ordered
    numbered = [
        (int(match.group(1)), name)
        for name in names
        if (match := re.fullmatch(r"ppt/slides/slide(\d+)\.xml", name))
    ]
    return [name for _number, name in sorted(numbered)]


def _read_pptx(path: Path, deadline: _Deadline, max_chars: int) -> Iterator[Section]:
    with _open_zip(path) as archive:
        slides = _slide_names(archive)
        if not slides:
            raise ExtractionError("not a PowerPoint file (no slides found)")
        chars = 0
        for number, name in enumerate(slides, start=1):
            deadline.check()
            root = _parse_member(archive, name)
            if root is None:
                continue
            lines = [
                "".join(t.text or "" for t in para.iter(f"{_A}t")).strip()
                for para in root.iter(f"{_A}p")
            ]
            text = "\n".join(line for line in lines if line)
            if text:
                yield Section(f"slide {number}", text)
                chars += len(text)
                if chars >= max_chars:
                    raise TextLimitReached


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    root = _parse_member(archive, "xl/sharedStrings.xml")
    if root is None:
        return []
    return [
        "".join(t.text or "" for t in si.iter(f"{_S}t")) for si in root.iter(f"{_S}si")
    ]


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{_S}t"))
    value = cell.find(f"{_S}v")
    if value is None or value.text is None:
        return ""
    if kind == "s":
        try:
            return shared[int(value.text)]
        except (ValueError, IndexError):
            return ""
    return value.text


def _read_xlsx(path: Path, deadline: _Deadline, max_chars: int) -> Iterator[Section]:
    with _open_zip(path) as archive:
        workbook = _parse_member(archive, "xl/workbook.xml")
        if workbook is None:
            raise ExtractionError("not an Excel workbook (xl/workbook.xml missing)")
        targets = _relationship_targets(archive, "xl/_rels/workbook.xml.rels", "xl")
        shared = _shared_strings(archive)
        names = set(archive.namelist())
        chars = 0
        for sheet in workbook.iter(f"{_S}sheet"):
            sheet_name = sheet.get("name", "")
            member = targets.get(sheet.get(f"{_R}id", ""), "")
            if member not in names:
                continue
            rows: list[str] = []
            first_row = last_row = 0
            with archive.open(member) as handle:
                try:
                    for _event, elem in ET.iterparse(handle, events=("end",)):
                        if elem.tag != f"{_S}row":
                            continue
                        row_number = int(elem.get("r") or last_row + 1)
                        cells = [_cell_text(c, shared) for c in elem.iter(f"{_S}c")]
                        elem.clear()
                        line = "\t".join(cell for cell in cells if cell)
                        if not line:
                            continue
                        if not rows:
                            first_row = row_number
                        last_row = row_number
                        rows.append(line)
                        chars += len(line)
                        if len(rows) == ROWS_PER_SECTION:
                            yield Section(
                                f"sheet '{sheet_name}', rows {first_row}-{last_row}",
                                "\n".join(rows),
                            )
                            rows = []
                            deadline.check()
                        if chars >= max_chars:
                            break
                except ET.ParseError as exc:
                    raise ExtractionError(f"broken XML in {member} ({exc})") from exc
            if rows:
                yield Section(
                    f"sheet '{sheet_name}', rows {first_row}-{last_row}",
                    "\n".join(rows),
                )
            if chars >= max_chars:
                raise TextLimitReached


# ---------------------------------------------------------------------------- pdf


def _read_pdf(path: Path, deadline: _Deadline, max_chars: int) -> Iterator[Section]:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise ExtractionError("the PDF reader (pypdfium2) is not installed") from exc

    try:
        pdf = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        raise ExtractionError(f"cannot open PDF ({exc})") from exc
    try:
        found_text = False
        chars = 0
        for index in range(len(pdf)):
            deadline.check()
            page = pdf[index]
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_bounded().strip()
                finally:
                    textpage.close()
            finally:
                page.close()
            if text:
                found_text = True
                yield Section(f"page {index + 1}", text)
                chars += len(text)
                if chars >= max_chars:
                    raise TextLimitReached
        if not found_text:
            raise NoTextError("no text layer (probably a scanned PDF)")
    finally:
        pdf.close()


_READERS = {
    "txt": _read_text,
    "md": _read_text,
    "csv": _read_text,
    "eml": _read_eml,
    "docx": _read_docx,
    "pptx": _read_pptx,
    "xlsx": _read_xlsx,
    "pdf": _read_pdf,
}

# Cheap types first, so most files are searchable early in a long build.
COST_ORDER = {
    "txt": 0,
    "md": 0,
    "csv": 0,
    "eml": 0,
    "docx": 1,
    "pptx": 1,
    "xlsx": 1,
    "pdf": 2,
}


def file_type(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


def extract_sections(
    path: Path, *, max_seconds: float, max_chars: int
) -> Iterator[Section]:
    """Yield the file's text in sections.

    Raises ExtractionError (or NoTextError) when the file cannot be read, and
    TimeLimitReached when ``max_seconds`` runs out part-way through.
    """
    reader = _READERS.get(file_type(path))
    if reader is None:
        raise ExtractionError(f"unsupported file type: {path.suffix or '(none)'}")
    sections = reader(path, _Deadline(max_seconds), min(max_chars, MAX_CHARS_PER_FILE))
    return _split_long(sections)


def _split_long(sections: Iterator[Section]) -> Iterator[Section]:
    for section in sections:
        text = section.text
        while len(text) > MAX_SECTION_CHARS:
            cut = text.rfind(" ", 0, MAX_SECTION_CHARS)
            cut = cut if cut > MAX_SECTION_CHARS // 2 else MAX_SECTION_CHARS
            yield Section(section.location, text[:cut])
            text = text[cut:].lstrip()
        if text:
            yield Section(section.location, text)
