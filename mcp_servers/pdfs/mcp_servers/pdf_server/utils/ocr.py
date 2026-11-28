"""Optional OCR support for image-only/scanned PDF pages.

Uses pytesseract, which shells out to the `tesseract` binary. When either is
missing, `ocr_unavailable_reason()` says which so callers can report it.

Transcribing a page is two steps inside tesseract: layout analysis decides
which pixels are lines of text, then recognition reads them. A line the first
step discards never reaches the second, so it is absent from the transcription
with no marker, no low confidence score and no gap -- the remaining lines close
up behind it. Part of that layout analysis is a table finder, and on a page
whose table meets a rule it can discard one whole row that way: the row's words
come back at 95-96% confidence once the finder is off, and are missing from
`image_to_data` entirely once it is on.

Turning the finder off everywhere is not the fix, because it is the same trade
in the other direction -- on a fully ruled grid it is the pass with the finder
*on* that reads cells the other one drops. So the page is read twice and the
second read is only ever allowed to put back lines the first one never
produced; see `_restore_dropped_lines`.

"Never produced" is decided on the page, not on the text. Two reads of one
scan disagree about single characters all the time -- ``EMP-3381`` against
``EMP.3381``, ``Caleb`` against ``Ca1eb`` -- so a second-read line whose text
the first read lacks is usually a re-reading of a line the first read *has*,
and splicing it in would print that row twice. A line is restored only where
the first read placed no legible word at all, and only when the second read is
sure of it.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass

_OCR_AVAILABLE = False
_pytesseract = None

# Tesseract's layout analysis minus the table finder. A build that does not
# know the variable prints a warning and reads the page the same way, which
# simply leaves the second read with nothing to put back.
_LINE_RECOVERY_CONFIG = "-c textord_tabfind_find_tables=0"

# The mean word confidence a second-read line needs before it is put back. The
# discarded row it exists for reads at 95-96; the specks, rules and stamps a
# first read rightly ignored and a second read turns into "words" sit far below.
_RESTORE_MIN_CONFIDENCE = 80.0

# A first-read word below this confidence is one tesseract could not read --
# the smear it makes of a row the table finder mangled reads at single digits --
# so it does not count as the first read having covered that stretch.
_COVERING_MIN_CONFIDENCE = 50.0

# How much of the shorter of two lines' heights they must share before they are
# the same stretch of the page. Neighbouring lines' boxes touch; one line read
# twice overlaps itself almost entirely.
_SAME_BAND = 0.3

try:
    import pytesseract

    _pytesseract = pytesseract
    _OCR_AVAILABLE = True
except ImportError:
    pass


def ocr_unavailable_reason() -> str | None:
    """Return why OCR cannot run, or None when pytesseract and tesseract are both present."""
    if not _OCR_AVAILABLE or _pytesseract is None:
        return "OCR unavailable: pytesseract is not installed"
    cmd = getattr(_pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    if shutil.which(cmd) is None:
        return f"OCR unavailable: tesseract binary '{cmd}' not found"
    return None


def ocr_available() -> bool:
    """Return True if OCR can run (pytesseract importable and tesseract binary on PATH)."""
    return ocr_unavailable_reason() is None


@dataclass(frozen=True)
class _Line:
    """One line of a read: its words as tesseract spelled them, where, how sure."""

    text: str
    left: int
    top: int
    right: int
    bottom: int
    confidence: float


@dataclass(frozen=True)
class _Word:
    left: int
    top: int
    right: int
    bottom: int
    confidence: float


def _read_tsv(tsv: str) -> tuple[list[_Line], list[_Word]]:
    """The lines and words of one tesseract TSV, in the order it reports them."""
    grouped: dict[tuple[str, str, str, str], list[tuple[_Word, float, str]]] = {}
    words: list[_Word] = []
    for row in tsv.splitlines()[1:]:
        fields = row.split("\t")
        if len(fields) < 12 or fields[0] != "5":
            continue
        text = "\t".join(fields[11:]).strip()
        try:
            left, top, width, height = (int(v) for v in fields[6:10])
            confidence = float(fields[10])
        except ValueError:
            continue
        if not text or confidence < 0:
            continue
        word = _Word(left, top, left + width, top + height, confidence)
        words.append(word)
        grouped.setdefault((fields[1], fields[2], fields[3], fields[4]), []).append(
            (word, confidence, text)
        )
    lines = [
        _Line(
            text=" ".join(text for _, _, text in members),
            left=min(word.left for word, _, _ in members),
            top=min(word.top for word, _, _ in members),
            right=max(word.right for word, _, _ in members),
            bottom=max(word.bottom for word, _, _ in members),
            confidence=sum(conf for _, conf, _ in members) / len(members),
        )
        for members in grouped.values()
    ]
    return lines, words


def _shares_band(line: _Line, word: _Word) -> bool:
    """Do `line` and `word` sit on the same stretch of the page?"""
    across = min(line.right, word.right) - max(line.left, word.left)
    down = min(line.bottom, word.bottom) - max(line.top, word.top)
    shorter = min(line.bottom - line.top, word.bottom - word.top)
    return across > 0 and shorter > 0 and down >= _SAME_BAND * shorter


def _normalized(text: str) -> str:
    """Collapse a line to what makes it the same line: its words, case-folded."""
    return re.sub(r"\s+", " ", text).strip().casefold()


def _restore_dropped_lines(
    primary: str,
    primary_lines: list[_Line],
    primary_words: list[_Word],
    recovery: list[_Line],
) -> str:
    """Return `primary` with the lines only the second read produced spliced in.

    `primary` is returned verbatim -- every line it has, in its order, with its
    spacing -- so a second read can only ever add. A second-read line is added
    only when all three hold:

    - the first read placed no legible word anywhere on it, so it is a line the
      first read's layout analysis discarded (or smeared past reading) rather
      than one it read differently;
    - its words are not already somewhere in the first read's text, which also
      covers a row the second read split into one line per cell;
    - its mean word confidence reaches `_RESTORE_MIN_CONFIDENCE`.

    It is placed after the last first-read line above it on the page, preferring
    one in the same column, so a restored table row lands among its neighbours.
    """
    text_lines = primary.splitlines()
    page_text = _normalized(primary)
    # Each first-read line in the TSV is one line of the text, in order.
    content = [index for index, line in enumerate(text_lines) if line.strip()]
    anchored = len(content) == len(primary_lines) and all(
        _normalized(text_lines[index]) == _normalized(line.text)
        for index, line in zip(content, primary_lines, strict=True)
    )
    if not anchored:
        return primary

    inserts: dict[int, list[_Line]] = {}
    for line in recovery:
        text = _normalized(line.text)
        if not text or text in page_text:
            continue
        if line.confidence < _RESTORE_MIN_CONFIDENCE:
            continue
        if any(
            word.confidence >= _COVERING_MIN_CONFIDENCE and _shares_band(line, word)
            for word in primary_words
        ):
            continue
        middle = (line.top + line.bottom) / 2
        above = [
            (index, first)
            for index, first in zip(content, primary_lines, strict=True)
            if (first.top + first.bottom) / 2 < middle
        ]
        same_column = [
            index
            for index, first in above
            if min(first.right, line.right) > max(first.left, line.left)
        ]
        after = max(same_column or [index for index, _ in above] or [-1])
        inserts.setdefault(after, []).append(line)
    if not inserts:
        return primary

    restored = [line.text for line in sorted(inserts.get(-1, ()), key=_top)]
    for index, text in enumerate(text_lines):
        restored.append(text)
        restored.extend(line.text for line in sorted(inserts.get(index, ()), key=_top))
    return "\n".join(restored)


def _top(line: _Line) -> int:
    return line.top


def ocr_page_image(image_bytes: bytes, *, format: str = "PNG") -> str | None:
    """Run OCR on an image and return extracted text.

    Args:
        image_bytes: Raw image bytes (PNG, JPEG, etc.)
        format: Image format hint for PIL (default "PNG")

    Returns:
        Extracted text string, or None if OCR failed or is unavailable.
    """
    if not ocr_available() or _pytesseract is None:
        return None

    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))
        # One run gives both the transcription and where each word of it is.
        outputs = _pytesseract.run_and_get_multiple_output(img, ["txt", "tsv"])
        text, tsv = (str(output or "") for output in outputs)
    except Exception:
        return None

    try:
        primary_lines, primary_words = _read_tsv(tsv)
        recovery, _ = _read_tsv(
            str(_pytesseract.image_to_data(img, config=_LINE_RECOVERY_CONFIG) or "")
        )
        text = _restore_dropped_lines(text, primary_lines, primary_words, recovery)
    except Exception:
        # The transcription the page already has is worth more than the lines
        # a failed second read might have added.
        pass
    return text.strip() or None


type OcrLine = tuple[str, tuple[float, float, float, float]]


def ocr_page_lines(image_bytes: bytes) -> list[OcrLine] | None:
    """Run OCR on a page image and return its lines with their boxes, in reading order.

    The boxes are in the image's own pixel space, as ``(x0, y0, x1, y1)``.
    Callers that need only *where* the page shows its text -- not tesseract's
    reading of the characters -- use this rather than `ocr_page_image`.

    Returns:
        The lines tesseract found, in the order its layout analysis reads them,
        or None when OCR is unavailable or fails.
    """
    if not ocr_available() or _pytesseract is None:
        return None
    try:
        import io

        from PIL import Image

        data = _pytesseract.image_to_data(
            Image.open(io.BytesIO(image_bytes)),
            output_type=_pytesseract.Output.DICT,
        )
    except Exception:
        return None

    lines: dict[tuple[int, int, int], list[int]] = {}
    for index, word in enumerate(data.get("text", [])):
        if not str(word).strip():
            continue
        key = (
            int(data["block_num"][index]),
            int(data["par_num"][index]),
            int(data["line_num"][index]),
        )
        lines.setdefault(key, []).append(index)
    result: list[OcrLine] = []
    for key in sorted(lines):
        words = lines[key]
        x0 = min(float(data["left"][i]) for i in words)
        y0 = min(float(data["top"][i]) for i in words)
        x1 = max(float(data["left"][i]) + float(data["width"][i]) for i in words)
        y1 = max(float(data["top"][i]) + float(data["height"][i]) for i in words)
        text = " ".join(str(data["text"][i]).strip() for i in words)
        result.append((text, (x0, y0, x1, y1)))
    return result
