"""Print facts of PDF pages: boxes, declared standard, barcodes, fonts, colours, images and text positions."""

from __future__ import annotations

import asyncio
import io
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree

import numpy as np
from PIL import Image
from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject

from ...tool_paths import staged_bin_dir
from ..models import TransformationOutput
from ..style_metadata_cache import DEGRADED_MARKER, cached_style_text
from .barcodes import AddonCheck, find_barcodes
from .print_facts import drawing_lines, rendered_colour_lines, standard_lines

BARCODE_DPI = 400
# A poster at 400 dpi is hundreds of megapixels, so large pages render coarser.
_BARCODE_MAX_PIXELS = 60_000_000
_MAX_BARCODES = 20
# EAN-13 allows bars down to 0.264 mm (80% size); ZXing needs about 3 px per bar to read them.
_MIN_MODULE_MM = 0.264
_MIN_MODULE_PIXELS = 3
_COLOUR_DPI = 50
_COLOUR_MAX_PIXELS = 4_000_000
_POPPLER_TIMEOUT = 30
# Each page renders twice, so a read measures a few pages and says where the next starts.
DEFAULT_MAX_PAGES = 5
MEASURE_SECONDS = 50
_PDF_BOXES = {
    "/MediaBox": "mediabox",
    "/CropBox": "cropbox",
    "/BleedBox": "bleedbox",
    "/TrimBox": "trimbox",
    "/ArtBox": "artbox",
}
_XHTML = "{http://www.w3.org/1999/xhtml}"


class PopplerUnavailableError(Exception):
    """A poppler binary that is missing or cannot run, as against one that ran and failed."""


def _poppler(*args: str, deadline: float) -> bytes:
    """Run a poppler tool, the staged copy when there is one, and kill it at ``deadline``."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("ran past the measuring deadline")
    bin_dir = staged_bin_dir("poppler")
    binary = str(Path(bin_dir) / args[0]) if bin_dir else args[0]
    try:
        return subprocess.run(  # noqa: S603
            [binary, *args[1:]],
            capture_output=True,
            check=True,
            timeout=min(_POPPLER_TIMEOUT, left),
        ).stdout
    except OSError as e:
        raise PopplerUnavailableError(str(e)) from e
    except subprocess.CalledProcessError as e:
        # 126 and 127: the binary is there but cannot run, as with a missing library.
        if e.returncode in (126, 127):
            raise PopplerUnavailableError(f"{args[0]} exited {e.returncode}") from e
        raise


def _pt(value: float) -> str:
    return f"{value:.1f}"


def _box_lines(pdf_page: PageObject) -> list[str]:
    """The page's boxes in points, from the top-left corner of its unrotated CropBox."""
    unit = pdf_page.user_unit
    crop = pdf_page.cropbox
    left, top = float(crop.left), float(crop.top)
    lines = []
    for key, attr in _PDF_BOXES.items():
        if key not in ("/MediaBox", "/CropBox") and key not in pdf_page:
            continue
        box = getattr(pdf_page, attr)
        x0, y0 = (float(box.left) - left) * unit, (top - float(box.top)) * unit
        x1, y1 = (float(box.right) - left) * unit, (top - float(box.bottom)) * unit
        lines.append(
            f"  {key[1:]} {x0:.3f}, {y0:.3f} to {x1:.3f}, {y1:.3f} "
            f"({(x1 - x0) / 72:.3f} x {(y1 - y0) / 72:.3f} in)"
        )
    return lines


def _write_cropped_unrotated(pdf_page: PageObject, dest: Path) -> None:
    """One page whose MediaBox is the CropBox, with no /Rotate and no /UserUnit.

    Poppler's ``-cropbox`` output is inconsistent on rotated pages, so it never sees
    one, and it ignores /UserUnit, so the page is scaled to 1/72-inch units instead.
    """
    writer = PdfWriter()
    flat = writer.add_page(pdf_page)
    flat.mediabox = pdf_page.cropbox
    flat[NameObject("/Rotate")] = NumberObject(0)
    if flat.user_unit != 1:
        flat.scale_by(flat.user_unit)
        del flat[NameObject("/UserUnit")]
    with dest.open("wb") as f:
        writer.write(f)


def _barcode_lines(target: Path, area_sq_in: float, deadline: float) -> list[str]:
    dpi = BARCODE_DPI
    if area_sq_in > 0:
        dpi = min(dpi, int((_BARCODE_MAX_PIXELS / area_sq_in) ** 0.5))
    raster = io.BytesIO(
        _poppler("pdftoppm", "-r", str(dpi), "-gray", str(target), deadline=deadline)
    )
    with Image.open(raster) as img:
        gray = np.asarray(img if img.mode == "L" else img.convert("L"))
    # A page at the pixel cap is 60 MB per copy, so the encoded one goes before ZXing reads the page.
    del raster
    scale = 72 / dpi
    found = find_barcodes(gray)
    lines = [f"Barcodes ({len(found)}), decoded from the page rendered at {dpi} dpi:"]
    low_dpi = (
        f" This page was rendered at only {dpi} dpi to stay within the pixel "
        "limit, so a barcode that fails here may still scan at full size."
        if _MIN_MODULE_MM / 25.4 * dpi < _MIN_MODULE_PIXELS
        else ""
    )
    if not found:
        lines.append(
            "  No EAN-13 or UPC-A barcode was decoded on this page. If the page "
            "shows one, its bars did not decode here, so they would likely not "
            "scan: they are too small, blurry, faint or damaged. This reader does "
            f"not read other formats, such as Code 128 or QR.{low_dpi}"
        )
    for code in found[:_MAX_BARCODES]:
        x0, y0, x1, y1 = (v * scale for v in code.box)
        # An EAN-13 starting with 0 is the same symbol as a UPC-A.
        name = (
            f"UPC-A {code.digits[1:]} (EAN-13 {code.digits})"
            if code.digits.startswith("0")
            else f"EAN-13 {code.digits}"
        )
        check = "check digit correct" if code.check_ok else "not a valid number"
        # A misread bar and a wrongly printed check digit fail alike, so the cause is not named.
        failed = (
            ""
            if code.check_ok
            else " These digits are the best read and fail the check digit: the bars "
            "are too small or blurry to read cleanly, or encode a wrong number, and "
            "either way would not scan reliably."
            + (
                f" Other reads of the same bars gave {', '.join(code.other_reads)}."
                if code.other_reads
                else ""
            )
            + low_dpi
        )
        addon = (
            "no add-on decoded"
            if not code.addon
            else f"{len(code.addon)}-digit add-on {code.addon}"
            if code.addon_check is AddonCheck.CONFIRMED
            else f"{len(code.addon)}-digit add-on read as {code.addon} but unverified, "
            "since every read of it failed its checksum"
            if code.addon_check is AddonCheck.FAILED
            else f"{len(code.addon)}-digit add-on read as {code.addon}, which can't be "
            "confirmed because the main number also failed its check"
        )
        if code.module is None or code.quiet_before is None or code.quiet_after is None:
            lines.append(
                f"  {name}, {check}, {addon}. Found near {_pt(x0)}, "
                f"{_pt(y0)}. Its size, module width and quiet zones are not "
                "measured: the bars are not square to the page, too few scan "
                f"lines decoded to tell, or the bars could not be traced.{failed}"
            )
            continue
        after = code.quiet_after * scale
        modules = code.quiet_after / code.module
        tail = (
            f"{_pt(after)} pt ({modules:.1f} modules) between the last bar and the add-on"
            if code.addon
            else f"{_pt(after)} pt after the last"
        )
        lines.append(
            f"  {name}, {check}, {addon}. Main symbol bars {_pt(x0)}, {_pt(y0)} "
            f"to {_pt(x1)}, {_pt(y1)} ({(x1 - x0) / 72:.3f} x {(y1 - y0) / 72:.3f} "
            f"in). Module {code.module / dpi * 25.4:.3f} mm. Paper-white space "
            f"{_pt(code.quiet_before * scale)} pt before the first bar and {tail}.{failed}"
        )
    if len(found) > _MAX_BARCODES:
        lines.append(
            f"  {len(found) - _MAX_BARCODES} more barcodes on this page are not listed."
        )
    return lines


def _image_lines(target: Path, deadline: float) -> list[str]:
    listing = _poppler("pdfimages", "-list", str(target), deadline=deadline).decode(
        errors="replace"
    )
    lines = []
    for row in listing.splitlines()[2:]:
        cols = row.split()
        if len(cols) < 14 or cols[2] != "image":
            continue
        width, height = int(cols[3]), int(cols[4])
        # Read from the end: an inline image's object ID is one column, "[inline]".
        x_col, y_col = cols[-4], cols[-3]
        # pdfimages prints "-" for a resolution it cannot compute.
        if not (x_col.isdigit() and y_col.isdigit()):
            lines.append(f"  {width} x {height} px, resolution unknown")
            continue
        x_ppi, y_ppi = int(x_col), int(y_col)
        placed = (
            f", placed {width / x_ppi:.3f} x {height / y_ppi:.3f} in"
            if x_ppi and y_ppi
            else ""
        )
        lines.append(f"  {width} x {height} px at {x_ppi} x {y_ppi} ppi{placed}")
    return ["Images (pixels, effective resolution, placed size):"] + (
        lines or ["  None."]
    )


def _text_lines(target: Path, deadline: float) -> list[str]:
    layout = _poppler("pdftotext", "-bbox-layout", str(target), "-", deadline=deadline)
    lines = []
    for line in ElementTree.fromstring(layout).iter(f"{_XHTML}line"):
        words = " ".join(w.text or "" for w in line.iter(f"{_XHTML}word")).strip()
        if not words:
            continue
        x0, y0, x1, y1 = (
            float(line.get(k, 0)) for k in ("xMin", "yMin", "xMax", "yMax")
        )
        lines.append(f"  {_pt(x0)}, {_pt(y0)} to {_pt(x1)}, {_pt(y1)}  {words}")
    return ["Text lines (left, top to right, bottom, then the text):"] + (
        lines or ["  None. Text drawn as outlines or images is not listed."]
    )


def _rendered_colours(target: Path, area_sq_in: float, deadline: float) -> list[str]:
    dpi = _COLOUR_DPI
    if area_sq_in > 0:
        dpi = max(1, min(dpi, int((_COLOUR_MAX_PIXELS / area_sq_in) ** 0.5)))
    raster = _poppler("pdftoppm", "-r", str(dpi), str(target), deadline=deadline)
    with Image.open(io.BytesIO(raster)) as img:
        rgb = np.asarray(img.convert("RGB"))
    return rendered_colour_lines(rgb, dpi)


def _measured(label: str, build: Callable[[], list[str]]) -> list[str]:
    try:
        return build()
    except PopplerUnavailableError:
        return [
            f"[{DEGRADED_MARKER}] poppler could not run on this grader, so {label} "
            "could not be measured. That says nothing about the file."
        ]
    except (TimeoutError, subprocess.TimeoutExpired) as e:
        return [f"[{DEGRADED_MARKER}] The {label} could not be measured in time: {e}"]
    except Exception as e:  # noqa: BLE001, the judge should see the failure
        return [f"The {label} could not be measured: {e}"]


def _page_lines(
    reader: PdfReader, pdf_page: PageObject, page: int, deadline: float
) -> list[str]:
    crop = pdf_page.cropbox
    unit = pdf_page.user_unit
    area = float(crop.width) * float(crop.height) * unit * unit / 72 / 72
    sections = [f"Page {page}:", "Page boxes:", *_box_lines(pdf_page)]
    if unit != 1:
        sections.append(
            f"  The page sets /UserUnit {unit:g}, so one PDF unit is {unit:g} points; "
            "every value here is already converted to points."
        )
    if pdf_page.rotation % 360:
        sections.append(
            f"  The page displays rotated {pdf_page.rotation % 360} degrees "
            "clockwise; every position here is before that rotation."
        )
    with tempfile.TemporaryDirectory() as tmp:
        flat = Path(tmp) / "page.pdf"
        measures: tuple[tuple[str, Callable[[], list[str]]], ...] = (
            ("fonts and colours", lambda: drawing_lines(pdf_page, reader, deadline)),
            ("barcodes", lambda: _barcode_lines(flat, area, deadline)),
            ("rendered colours", lambda: _rendered_colours(flat, area, deadline)),
            ("images", lambda: _image_lines(flat, deadline)),
            ("text", lambda: _text_lines(flat, deadline)),
        )
        try:
            _write_cropped_unrotated(pdf_page, flat)
        except Exception as e:  # noqa: BLE001, the judge should see the failure
            measures = measures[:1]  # the only measure that reads the original page
            sections.append(f"The page could not be prepared for measuring: {e}")
        for label, build in measures:
            sections.extend(_measured(label, build))
    return sections


def _measure(
    data: bytes, file_name: str, first_page: int, max_pages: int, deadline: float
) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        count = len(reader.pages)
    except Exception as e:  # noqa: BLE001, the judge should see the failure
        return f"Could not read {file_name} as a PDF: {e}"
    if not 1 <= first_page <= count:
        return f"{file_name} has {count} page(s); page {first_page} does not exist."
    last = min(count, first_page + max_pages - 1)
    sections = [
        f"{file_name}, {count} page(s). Positions are in points (72 per inch) "
        "from the top-left corner of each page's CropBox, unrotated.",
        *_measured("print standard", lambda: standard_lines(reader)),
    ]
    for page in range(first_page, last + 1):
        if time.monotonic() >= deadline:
            last = page - 1
            sections.append(
                f"[{DEGRADED_MARKER}] Measuring ran out of time before page {page}."
            )
            break
        sections.extend(_page_lines(reader, reader.pages[page - 1], page, deadline))
    if last < count:
        sections.append(
            f"Only pages {first_page}-{last} of {count} are measured here; "
            f"first_page={last + 1} measures from page {last + 1}."
        )
    return "\n".join(sections)


async def pdf_to_style_metadata(
    file_bytes: bytes,
    file_name: str,
    /,
    *,
    first_page: int = 1,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> TransformationOutput:
    """Print facts a rendered page can only estimate, for ``max_pages`` pages from ``first_page``."""

    async def extract(data: bytes, name: str) -> str:
        deadline = time.monotonic() + MEASURE_SECONDS
        return await asyncio.to_thread(
            _measure, data, name, max(first_page, 1), max(max_pages, 1), deadline
        )

    text = await cached_style_text(
        file_bytes, file_name, f"pdf:{first_page}:{max_pages}", extract
    )
    return TransformationOutput(text=text)
