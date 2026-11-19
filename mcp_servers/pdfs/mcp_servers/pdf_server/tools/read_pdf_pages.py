import base64
import io
import os
from typing import Annotated, Any, cast

import fitz  # PyMuPDF
import pypdf
from loguru import logger
from models.pdf_read import (
    FormFieldInfo,
    ImageInfo,
    PdfPagesRead,
    ReadPdfPagesResult,
    StrikethroughInfo,
)
from PIL import Image
from pydantic import Field
from pypdf.errors import FileNotDecryptedError
from pypdf.generic import TextStringObject
from utils.decorators import make_async_background
from utils.image_cache import (
    IMAGE_CACHE,
    IMAGE_QUALITY,
    MAX_IMAGE_HEIGHT,
    MAX_IMAGE_WIDTH,
)
from utils.ocr import ocr_available, ocr_page_image, ocr_unavailable_reason
from utils.path_utils import PathTraversalError, resolve_under_root
from utils.pdf_encoding import apply_winansi_bullet_mapping
from utils.pdf_layout import has_interleaved_text, page_ink, unplaced_rotated_text
from utils.pdf_occlusion import occluded_text
from utils.pdf_protection import (
    PASSWORD_PROTECTED_NOTE,
    PdfPasswordIncorrectError,
    PdfPasswordRequiredError,
    image_cache_prefix,
    password_error_message,
    password_protected_error,
    unlock_pdf_bytes,
)
from utils.schema import GeminiBaseModel
from utils.structured_output import StructuredText

# Maximum file size to load (100MB)
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024

# pypdf's layout extractor walks the page content stream only — it has no `Do`
# handling — so text drawn through a Form XObject, rotated text, and text in a
# font it cannot interpret are all dropped from its output. Reading-order
# extraction follows those paths, so it is the yardstick for whether layout mode
# saw the whole page. Layout mode is kept while it stays this close to the
# reading-order character count; below that it has lost content, and returning
# complete text matters more than preserving column alignment.
LAYOUT_COMPLETENESS_RATIO = 0.95

# Real documents embed image XObjects Pillow refuses — JPEG2000 and JBIG2
# streams, CCITT masks, bytes whose length disagrees with /Width x /Height. That
# is a property of the document rather than a malfunction, so the note the caller
# gets has to be short and identical on every read of the same file. The
# exception's repr is neither: it names the `_io.BytesIO` buffer Pillow was
# handed, including its process memory address, which changes between calls.
UNDECODABLE_IMAGE_NOTE = (
    "Page {page_num}: an embedded image could not be decoded and was skipped"
)

# Text a page paints and then covers over is in the file but not on the page, so
# it is dropped. Saying so keeps the difference between what the document holds
# and what this tool reports from being invisible to the caller.
HIDDEN_TEXT_NOTE = (
    "Page {page_num}: {count} text span(s) hidden under later opaque paint were omitted"
)

# Text the page sets at an angle survives none of the extractors below intact.
# Layout mode strips rotated runs instead of placing them, and the two PyMuPDF
# paths that assemble text into horizontal lines by y band -- the position-sorted
# text and the unoccluded rebuild that mirrors it -- break the run into its words
# and file each under whatever line its own y range falls in, which scatters a
# drawing's dimension callout across unrelated ones. Either way the run does not
# reach the caller as the run the page painted, and it is a few dozen characters
# against a couple of thousand, so the completeness ratio below is satisfied and
# the page is served anyway -- a chart whose units are gone reads as a chart that
# never had any. The note carries the text so the caller gets the content as well
# as the fact that the page text is short of it.
ROTATED_TEXT_NOTE = (
    "Page {page_num}: text set at an angle, not reproduced as written in the page "
    "text above: {text}"
)

# A page with no text layer has no characters to extract, so what the caller
# gets is tesseract's transcription of a picture of the page. Served unlabelled
# it is indistinguishable from text the document actually holds: a scanned load
# chart reads back `595 KN` where the page says `535 kN`, a status matrix reads
# `$4` where it says `S4`, and nothing in the response says which of the two
# kinds of text this was.
#
# The note is a fixed marker and reports no per-word confidence, because the
# engine's confidence does not tell the two kinds of word apart. Measured on
# those two pages: `A/C` read as `AIC` at 96%, the `535 kN` misread scored 79%,
# and three of the four `$4` occurrences scored 51-83% -- all of them above any
# cutoff that would have caught the fourth. Naming a handful of low scorers
# would therefore present several hundred words per page, misreads among them,
# as the ones that did not need checking.
#
# The note names no tool, deliberately. A rollout can serve this server's tools
# under per-world aliases, and the gateway rewrites only the list_tools payload
# -- a tool result is passed back to the model verbatim, and a call to a name
# the rollout aliased away is refused as an unknown tool. So a tool name here
# would both hand over the canonical name aliasing exists to withhold and point
# at a call that fails. Describe the capability instead.
OCR_TEXT_NOTE = (
    "Page {page_num}: this page's text was transcribed by OCR from an image of "
    "the page -- the page carries no embedded text -- and may contain misreads, "
    "so confirm exact values (codes, quantities, identifiers) against a rendered "
    "image of the page before relying on them."
)

# Text the page positions past its own edges is in the file but nowhere a reader
# could see it, so it is dropped for the same reason text under opaque paint is.
# The note carries a count rather than the text: a run no viewer draws is
# usually an editing leftover, and reproducing it here would put back the
# fragment the page was served without.
OFFPAGE_TEXT_NOTE = (
    "Page {page_num}: {count} text line(s) positioned outside the page were omitted"
)


def _visible_char_count(text: str) -> int:
    """Count the characters a reader would see, ignoring layout padding.

    Layout mode pads with spaces and newlines to place text horizontally, so raw
    lengths are not comparable between the two extractors.
    """
    return len("".join(text.split()))


def _compress_image_to_base64(
    image_bytes: bytes, width: int, height: int, color_space: str
) -> str:
    """Compress and convert image to base64.

    Args:
        image_bytes: Raw image bytes from PDF (already decompressed by pypdf)
        width: Image width in pixels
        height: Image height in pixels
        color_space: PDF color space (e.g., /DeviceRGB, /DeviceGray, /DeviceCMYK)

    Returns:
        Base64 encoded string of compressed JPEG image
    """
    try:
        # Determine PIL mode from PDF color space
        if color_space == "/DeviceRGB":
            mode = "RGB"
        elif color_space == "/DeviceGray":
            mode = "L"
        elif color_space == "/DeviceCMYK":
            mode = "CMYK"
        else:
            # Try to decode as RGB by default
            mode = "RGB"

        # Create PIL Image from raw bytes
        try:
            img = Image.frombytes(mode, (width, height), image_bytes)
        except ValueError:
            buffer = io.BytesIO(image_bytes)
            img = Image.open(buffer)

        if img.mode in ("RGBA", "LA"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        elif img.mode == "P":
            img = img.convert("RGBA")
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        elif img.mode == "CMYK":
            img = img.convert("RGB")
        elif img.mode != "RGB" and img.mode != "L":
            img = img.convert("RGB")

        if img.width > MAX_IMAGE_WIDTH or img.height > MAX_IMAGE_HEIGHT:
            img.thumbnail((MAX_IMAGE_WIDTH, MAX_IMAGE_HEIGHT), Image.Resampling.LANCZOS)

        output_buffer = io.BytesIO()
        img.save(output_buffer, format="JPEG", quality=IMAGE_QUALITY, optimize=True)
        compressed_bytes = output_buffer.getvalue()

        return base64.b64encode(compressed_bytes).decode("utf-8")

    except Exception:
        raise


def _extract_images_from_page(
    page: pypdf.PageObject,
    page_num: int,
    cache_prefix: str,
    errors: list[str] | None = None,
) -> list[ImageInfo]:
    """Extract images from a PDF page and store in memory cache.

    Args:
        page: PyPDF page object
        page_num: Page number (1-indexed)
        cache_prefix: Cache key prefix from ``image_cache_prefix``: the resolved
            physical path of the PDF (per-actor roots keep cached images
            tenant-scoped), plus a password token when the document needed one
        errors: Optional list to append error messages to

    Returns:
        List of ImageInfo objects for images found on the page
    """
    images = []

    try:
        resources = page.get("/Resources")
        if resources is None:
            return images

        xobjects = resources.get("/XObject")
        if xobjects is None:
            return images

        image_count = 0
        for _xobj_idx, (obj_name, obj_ref) in enumerate(xobjects.items()):
            try:
                obj = obj_ref.get_object()

                if obj.get("/Subtype") != "/Image":
                    continue

                width = int(obj.get("/Width", 0))
                height = int(obj.get("/Height", 0))

                color_space = str(obj.get("/ColorSpace", "/DeviceRGB"))

                image_bytes = obj.get_data()

                base64_data = _compress_image_to_base64(
                    image_bytes, width, height, color_space
                )

                annotation_key = f"page{page_num}_img{image_count}"

                cache_key = f"{cache_prefix}::{annotation_key}"
                IMAGE_CACHE.set(cache_key, base64_data)

                image_info = ImageInfo(
                    annotation=annotation_key,
                    page_number=page_num,
                    image_index=image_count,
                    width=width if width > 0 else None,
                    height=height if height > 0 else None,
                )
                images.append(image_info)
                image_count += 1

            except Exception as exc:
                # The XObject name and the exception stay server-side: they are
                # the operator's handle on which image failed and why, and
                # neither is something the caller can act on.
                logger.debug(
                    f"read_pdf_pages: page {page_num} image {obj_name} "
                    f"could not be decoded: {repr(exc)}"
                )
                if errors is not None:
                    errors.append(UNDECODABLE_IMAGE_NOTE.format(page_num=page_num))
                continue

    except Exception as exc:
        if errors is not None:
            errors.append(f"Page {page_num} image extraction: {repr(exc)}")

    return images


def warm_page_image_cache(
    physical_path: str, page_num: int, password: str | None = None
) -> int:
    """Extract one page's embedded images into ``IMAGE_CACHE`` on demand.

    ``read_image`` calls this when an annotation is not cached, so retrieving an
    image never depends on a prior ``read_pdf_pages`` call in the same process.
    Only the requested page is parsed.

    Args:
        physical_path: Resolved physical path of the PDF
        page_num: Page number (1-indexed)
        password: The document's password, when it has a user password. The
            images are cached under ``image_cache_prefix(physical_path, ...)``.

    Returns:
        The number of images cached for that page.

    Raises:
        ValueError: The document is missing, unreadable, too large, or has no
            such page.
        PdfPasswordRequiredError / PdfPasswordIncorrectError: the document
            needs a password and none, or the wrong one, was given (both are
            ``ValueError`` subclasses).
    """
    if not os.path.exists(physical_path):
        raise ValueError("the document does not exist")
    if not os.path.isfile(physical_path):
        raise ValueError("the path is not a file")

    file_size = os.path.getsize(physical_path)
    if file_size > MAX_FILE_SIZE_BYTES:
        size_mb = file_size / (1024 * 1024)
        max_mb = MAX_FILE_SIZE_BYTES / (1024 * 1024)
        raise ValueError(
            f"the document is too large ({size_mb:.1f}MB, max {max_mb:.0f}MB)"
        )

    with open(physical_path, "rb") as f:
        file_bytes = f.read()

    file_bytes, unlocked = unlock_pdf_bytes(file_bytes, password)
    cache_prefix = image_cache_prefix(physical_path, password if unlocked else None)

    pdf_reader = pypdf.PdfReader(io.BytesIO(file_bytes))
    try:
        total_pages = len(pdf_reader.pages)
    except FileNotDecryptedError as exc:
        raise ValueError(PASSWORD_PROTECTED_NOTE) from exc
    if page_num < 1 or page_num > total_pages:
        raise ValueError(
            f"page {page_num} is out of range (the document has {total_pages} page(s))"
        )

    errors: list[str] = []
    images = _extract_images_from_page(
        pdf_reader.pages[page_num - 1], page_num, cache_prefix, errors
    )
    if errors:
        logger.warning(
            f"read_image: on-demand extraction of page {page_num} reported errors: {errors}"
        )
    return len(images)


def _extract_strikethrough_from_pypdf_page(
    page: pypdf.PageObject, page_num: int, errors: list[str] | None = None
) -> list[StrikethroughInfo]:
    """Extract StrikeOut annotations from a PDF page using pypdf.

    Args:
        page: PyPDF page object
        page_num: Page number (1-indexed)
        errors: Optional list to append error messages to

    Returns:
        List of StrikethroughInfo objects for StrikeOut annotations found on the page
    """
    strikethrough_items = []

    try:
        annotations = page.get("/Annots")
        if annotations is None:
            return strikethrough_items

        for annot_idx, annot_ref in enumerate(annotations):
            try:
                annot = annot_ref.get_object()
                subtype = annot.get("/Subtype")

                if subtype == "/StrikeOut":
                    contents = annot.get("/Contents")
                    if contents is not None:
                        if isinstance(contents, TextStringObject):
                            contents = str(contents)
                        elif isinstance(contents, bytes):
                            contents = contents.decode("utf-8", errors="replace")
                        else:
                            contents = str(contents) if contents else None
                    else:
                        contents = None

                    rect = annot.get("/Rect")
                    rect_coords = None
                    if rect:
                        try:
                            rect_coords = [float(x) for x in rect]
                        except (TypeError, ValueError):
                            rect_coords = None

                    strikethrough_info = StrikethroughInfo(
                        page_number=page_num,
                        contents=contents,
                        rect=rect_coords,
                    )
                    strikethrough_items.append(strikethrough_info)

            except Exception as exc:
                if errors is not None:
                    errors.append(
                        f"Page {page_num} annotation {annot_idx}: {repr(exc)}"
                    )
                continue

    except Exception as exc:
        if errors is not None:
            errors.append(f"Page {page_num} strikethrough extraction: {repr(exc)}")

    return strikethrough_items


# How many characters of the text it crosses a line must span to count as a
# strikethrough rule. PDFs that paint their text as vector outlines expose every
# glyph contour through get_drawings(), and those contours include
# near-horizontal segments at the glyph's own mid-height — the same place a
# strikethrough sits. Width is what separates the two: glyph ink never spans
# more than about one character, while a rule runs across the text it cancels.
MIN_STRIKETHROUGH_CHAR_WIDTHS = 2.0

# A rule can never be wider than the text it crosses, so on a two-character span
# the character minimum above lands on the span's own width and a rule drawn
# across it fails by rounding alone. Spans of two characters or more therefore
# cap the minimum here. Single characters keep the uncapped minimum: a dash-like
# glyph is itself a horizontal line exactly covering its span, so capping it
# would make every em dash on an outlined-glyph page read as struck through.
MAX_STRIKETHROUGH_SPAN_FRACTION = 0.9

# How much of a line has to fall inside the text it crosses before it reads as a
# strikethrough rather than a rule that happens to pass through. A strikethrough
# is placed by the text it cancels and stops with it; a table rule, a drawing
# border, a title-block line, a chart gridline or an annotation leader is placed
# by the page and runs on past the text into the margin, the next cell or the
# drawing. Over the 266 documents of the ten worlds this was reported from the
# two separate cleanly: every line that visibly strikes text keeps at least
# 0.975 of its length inside that text's line box, while the lines that strike
# nothing -- the invoice row rules, the title-block rule the ME 02 zone labels
# sit on, the retainer's running-header rule, the civil drawings' leaders --
# reach 0.571 at most. This sits in the middle of that gap, leaving about a
# fifth of a line's length of room on either side of it.
MIN_RULE_TEXT_CONTAINMENT = 0.8

# A line of text whose baseline is not horizontal cannot be struck by one of
# these segments: a strike follows the text it cancels, so on rotated text it is
# rotated too and is not a horizontal line at all. Without this the diagonal
# "SUPERSEDED" and "NOT RELEASED" stamps and the sheet footers set sideways down
# the margin of the civil drawings all read as struck, because a rotated run's
# bounding box is deep enough for any rule on the page to land within a fifth of
# a text height of its centre. The bound is on slope rather than angle, so text
# set upside down -- direction (-1, 0) -- still qualifies.
MAX_BASELINE_SLOPE = 0.05

# The shortest run of characters a line has to cross before that run is reported
# as struck. A strikethrough cancels a word or a value; a line that reaches into
# a single glyph of one is something else passing through, which is what the
# routing sheet's cell separator dashes do to the last letter of the operation
# text in the column beside them. One character is also the most a glyph's own
# contour can cover, so this is the bound MIN_STRIKETHROUGH_CHAR_WIDTHS already
# puts on the line's width, restated against what the line turned out to cross.
MIN_STRUCK_CHARACTERS = 2


def _horizontal_rules(fitz_page: fitz.Page) -> list[tuple[float, float, float]]:
    """Every near-horizontal line the page draws, as ``(x0, x1, y)``.

    Each segment is narrowed to the clip in force when it was drawn, the way
    ``pdf_occlusion`` narrows fills. Documents edited line by line are assembled
    by replaying the whole page inside a clip around each edited line, so every
    rule on the page is issued once per such line and only the sliver inside the
    clip is painted. Read at face value those replays put a full-width rule
    through most of the page, which is how the governance standard reported its
    live Section 5 text as struck.
    """
    rules: list[tuple[float, float, float]] = []
    clips: list[tuple[int, fitz.Rect]] = []
    for path in fitz_page.get_drawings(extended=True):
        level = path.get("level", 0)
        while clips and clips[-1][0] >= level:
            clips.pop()
        if path.get("type") in ("clip", "group"):
            scissor = path.get("scissor")
            if scissor is not None:
                clips.append((level, fitz.Rect(scissor)))
            continue
        for item in path.get("items", []):
            if item[0] != "l" or abs(item[1].y - item[2].y) >= 3:
                continue
            x0, x1 = min(item[1].x, item[2].x), max(item[1].x, item[2].x)
            y = (item[1].y + item[2].y) / 2
            for _level, scissor in clips:
                x0, x1 = max(x0, scissor.x0), min(x1, scissor.x1)
                if not scissor.y0 <= y <= scissor.y1:
                    x1 = x0
            if x1 > x0:
                rules.append((x0, x1, y))
    return rules


def _struck_runs(
    span: dict[str, Any], crossings: list[tuple[float, float]]
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """The runs of characters in *span* that the rules in *crossings* cross.

    A rule is reported with the text it actually crosses rather than with the
    whole span it lands in. A redline through one value leaves the replacement
    beside it in force, and PyMuPDF routinely hands both back as one span: the
    retainer's "trailing twelve (12) months eighteen (18) months" is a single
    span of which only the first half is struck, and reporting the span told the
    caller the governing eighteen months had been deleted too.

    A character counts as crossed when its own midpoint falls under a rule, so a
    rule stopping a fraction of a point inside the final glyph still takes that
    glyph while the space past it is left out.

    The box comes back in MuPDF's space, as the character boxes it is built
    from: the caller maps it through ``_pdf_space_rect`` before reporting it.
    """
    runs: list[tuple[str, tuple[float, float, float, float]]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        while current and current[0]["c"].isspace():
            current.pop(0)
        while current and current[-1]["c"].isspace():
            current.pop()
        if len(current) < MIN_STRUCK_CHARACTERS:
            current.clear()
            return
        box = fitz.EMPTY_RECT()
        for char in current:
            box |= fitz.Rect(char["bbox"])
        runs.append(
            ("".join(char["c"] for char in current), (box.x0, box.y0, box.x1, box.y1))
        )
        current.clear()

    for char in span.get("chars", []):
        middle = (char["bbox"][0] + char["bbox"][2]) / 2
        if any(x0 <= middle <= x1 for x0, x1 in crossings):
            current.append(char)
        else:
            flush()
    flush()
    return runs


def _pdf_space_rect(fitz_page: fitz.Page, bbox: tuple[float, ...]) -> list[float]:
    """Put a PyMuPDF box back into the bottom-left-origin space ``rect`` promises.

    PyMuPDF reports text and drawing geometry in MuPDF's own space, whose origin
    is the top-left corner of the page's cropbox, while ``StrikethroughInfo.rect``
    is documented as -- and the ``/StrikeOut`` path above already reports -- the
    PDF convention with the origin at the bottom-left. The conversion is the
    inverse of the PDF-to-MuPDF transform, which mirrors y about the cropbox's
    top edge *and* carries its x offset: mirroring about the page height alone
    leaves a page whose boxes do not start at (0, 0) wrong on both axes.

    ``Page.transformation_matrix`` is that transform only on an unrotated page.
    With ``/Rotate`` set, PyMuPDF discards MuPDF's own page transform and
    substitutes ``Matrix(1, 0, 0, -1, 0, cropbox.height)``, which has no x offset
    and measures y from the cropbox's height instead of its position -- so a
    rotated page whose boxes are offset comes back wrong by that offset. Rebuild
    the matrix from the page boxes instead, neither of which carries a rotation
    term: ``Page.mediabox`` is the raw ``/MediaBox``, and ``Page.cropbox`` is the
    raw ``/CropBox`` with y mirrored about ``mediabox.y1``, which makes
    ``mediabox.y1 - cropbox.y0`` the cropbox's top edge back in PDF space.
    """
    cropbox = fitz_page.cropbox
    to_mupdf = fitz.Matrix(1, 0, 0, -1, -cropbox.x0, fitz_page.mediabox.y1 - cropbox.y0)
    rect = fitz.Rect(bbox) * ~to_mupdf
    return [rect.x0, rect.y0, rect.x1, rect.y1]


def _extract_visual_strikethrough_from_page(
    fitz_page: fitz.Page, page_num: int, errors: list[str] | None = None
) -> list[StrikethroughInfo]:
    """Detect visual strikethrough (lines drawn through text) using PyMuPDF.

    Args:
        fitz_page: PyMuPDF page object
        page_num: Page number (1-indexed)
        errors: Optional list to append error messages to

    Returns:
        List of StrikethroughInfo objects for visual strikethrough found on the
        page, each carrying only the characters a rule runs through.
    """
    results = []
    try:
        rules = _horizontal_rules(fitz_page)
        if not rules:
            return results

        text_dict = cast(dict[str, Any], fitz_page.get_text("rawdict"))
        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                if abs(line.get("dir", (1.0, 0.0))[1]) > MAX_BASELINE_SLOPE:
                    continue
                line_x0, _, line_x1, _ = line.get("bbox", (0.0, 0.0, 0.0, 0.0))
                for span in line.get("spans", []):
                    chars = span.get("chars", [])
                    text = "".join(char["c"] for char in chars).strip()
                    bbox = span.get("bbox")
                    if not text or not bbox:
                        continue
                    x0, y0, x1, y1 = bbox
                    text_height = y1 - y0
                    span_width = x1 - x0
                    center_y = (y0 + y1) / 2
                    tolerance = text_height * 0.2
                    min_length = MIN_STRIKETHROUGH_CHAR_WIDTHS * span_width / len(text)
                    if len(text) >= 2:
                        min_length = min(
                            min_length, MAX_STRIKETHROUGH_SPAN_FRACTION * span_width
                        )

                    crossings = [
                        (rule_x0, rule_x1)
                        for rule_x0, rule_x1, rule_y in rules
                        if rule_x0 <= x1
                        and rule_x1 >= x0
                        and abs(rule_y - center_y) <= tolerance
                        and rule_x1 - rule_x0 >= min_length
                        and min(rule_x1, line_x1) - max(rule_x0, line_x0)
                        >= MIN_RULE_TEXT_CONTAINMENT * (rule_x1 - rule_x0)
                    ]
                    if not crossings:
                        continue
                    for struck, struck_box in _struck_runs(span, crossings):
                        results.append(
                            StrikethroughInfo(
                                page_number=page_num,
                                contents=struck,
                                rect=_pdf_space_rect(fitz_page, struck_box),
                            )
                        )
    except Exception as exc:
        if errors is not None:
            errors.append(f"Page {page_num} visual strikethrough: {repr(exc)}")
    return results


# Interactive form (AcroForm) fields keep their value in the field's `/V`, which
# names no position on the page. MuPDF paints the widget's appearance stream when
# it extracts text, so the values do reach the caller -- but as bare strings in
# annotation order, detached from the labels the page content stream draws. A
# filled order form reads back as `Facility:` with nothing after it, and the value
# `NW Regional Med Center` on its own line a hundred lines further down. The
# name is the only thing that puts a value back with its label, and it exists
# only on the field object, so the fields are reported again under their own
# heading with the association intact.
FORM_FIELDS_HEADING = "Form fields:"

# Check boxes and radio buttons draw a tick, not their export value, so their
# `/V` ("On", "Yes", a radio group's chosen state) is a state name rather than
# page text. Everything else paints its value verbatim.
_BUTTON_FIELD_TYPES = frozenset({"CheckBox", "RadioButton"})

# A signature holds a certificate rather than a value a reader could have typed,
# so there is no text to put next to a label.
_SIGNATURE_FIELD_TYPE = "Signature"

# `/V` of an unset check box or of a radio widget that is not the chosen one.
_UNCHECKED_STATE = "off"

# Annotation `/F` bits 2 (Hidden) and 6 (NoView): the widget is not painted for a
# reader, so its value is not page content either. ReadOnly and Locked are not in
# this set -- a form locked after being filled is the normal state of a completed
# form, and those are exactly the values that have to come back.
_HIDDEN_ANNOT_FLAGS = 2 | 32

# A field value is a form entry, not a document body. Anything past this is a
# pasted-in blob that would bury the rest of the page.
MAX_FORM_FIELD_VALUE_CHARS = 500

# Widgets on one visual row rarely share an exact `/Rect` top, so reading order
# groups them into rows by a fraction of their own height before ordering each
# row left to right.
_FORM_FIELD_ROW_TOLERANCE = 0.5


def _error(message: str) -> StructuredText[ReadPdfPagesResult]:
    return StructuredText(message, ReadPdfPagesResult(error=message))


def _form_field_value(raw: Any) -> str:
    """Normalize a widget's ``/V`` to one line of text.

    Multi-select choice fields hand back a sequence of selections; text fields
    may carry newlines. Both are flattened so each field occupies exactly one
    line of the ``Form fields:`` block.
    """
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        parts = [" ".join(str(item).split()) for item in cast(list[Any], raw)]
        return ", ".join(part for part in parts if part)
    return " ".join(str(raw).split())


def _extract_form_fields_from_page(
    fitz_doc: fitz.Document,
    fitz_page: fitz.Page,
    page_num: int,
    errors: list[str] | None = None,
) -> tuple[list[FormFieldInfo], int]:
    """Collect the page's filled form fields in reading order.

    Args:
        fitz_doc: PyMuPDF document the page belongs to (holds the annotation
            objects whose flags decide whether a widget is painted at all)
        fitz_page: PyMuPDF page object
        page_num: Page number (1-indexed)
        errors: Optional list to append error messages to

    Returns:
        The FormFieldInfo objects for widgets on this page that hold a value,
        ordered top-to-bottom then left-to-right with repeated name/value pairs
        collapsed, and the number of characters those widgets contribute to
        reading-order extraction. The second number counts every painting
        widget — before de-duplication and before truncation, and buttons not at
        all, since they draw a tick rather than their export value — because it
        describes what MuPDF put in the text, not what is reported here.
    """
    placed: list[tuple[fitz.Rect, FormFieldInfo]] = []
    seen: set[tuple[str, str]] = set()
    painted_chars = 0

    try:
        widgets = list(fitz_page.widgets())
    except Exception as exc:
        if errors is not None:
            errors.append(f"Page {page_num} form fields: {repr(exc)}")
        return [], 0

    for widget in widgets:
        try:
            field_type = str(widget.field_type_string or "")
            if field_type == _SIGNATURE_FIELD_TYPE:
                continue

            value = _form_field_value(widget.field_value)
            if field_type in _BUTTON_FIELD_TYPES:
                if value.casefold() == _UNCHECKED_STATE:
                    continue
            if not value:
                continue

            name = (widget.field_name or widget.field_label or "").strip()
            if not name:
                continue

            flags = fitz_doc.xref_get_key(widget.xref, "F")
            if flags[0] == "int" and int(flags[1]) & _HIDDEN_ANNOT_FLAGS:
                continue

            if field_type not in _BUTTON_FIELD_TYPES:
                painted_chars += _visible_char_count(value)

            if len(value) > MAX_FORM_FIELD_VALUE_CHARS:
                value = (
                    f"{value[:MAX_FORM_FIELD_VALUE_CHARS]}"
                    f"... (truncated, {len(value)} characters)"
                )

            if (name, value) in seen:
                continue
            seen.add((name, value))

            placed.append(
                (
                    fitz.Rect(widget.rect),
                    FormFieldInfo(
                        name=name,
                        value=value,
                        field_type=field_type,
                        page_number=page_num,
                    ),
                )
            )
        except Exception as exc:
            if errors is not None:
                errors.append(f"Page {page_num} form fields: {repr(exc)}")
            continue

    placed.sort(key=lambda entry: (entry[0].y0, entry[0].x0))

    ordered: list[FormFieldInfo] = []
    row: list[tuple[fitz.Rect, FormFieldInfo]] = []
    row_top = 0.0
    row_tolerance = 0.0
    for rect, info in placed:
        if row and rect.y0 - row_top > row_tolerance:
            ordered.extend(entry[1] for entry in sorted(row, key=lambda e: e[0].x0))
            row = []
        if not row:
            row_top = rect.y0
            row_tolerance = max(rect.height, 1.0) * _FORM_FIELD_ROW_TOLERANCE
        row.append((rect, info))
    ordered.extend(entry[1] for entry in sorted(row, key=lambda e: e[0].x0))

    return ordered, painted_chars


def _form_fields_block(fields: list[FormFieldInfo]) -> str:
    """Render *fields* as the trailing ``Form fields:`` block, or "" if empty."""
    if not fields:
        return ""
    return "\n".join([FORM_FIELDS_HEADING, *(str(field) for field in fields)])


class ReadPdfPagesInput(GeminiBaseModel):
    file_path: str = Field(
        ...,
        description="Absolute path to the PDF file. Must start with '/' and end with '.pdf'.",
    )
    pages: list[int] | None = Field(
        None,
        description="Optional list of 1-indexed page numbers to read (e.g., [1, 3, 5]). "
        "When None or omitted, all pages in the document are read.",
    )
    password: str | None = Field(
        None,
        description="Password for a password-protected (encrypted) PDF. Omit it for "
        "ordinary PDFs and for PDFs that open with an empty password. When a document "
        "needs a password and none is given, the tool says so; a wrong password returns "
        "a distinct 'password ... is incorrect' error.",
    )


@make_async_background
def read_pdf_pages(
    input: Annotated[
        ReadPdfPagesInput,
        Field(
            description="PDF reading settings, including the file path and optional page selection."
        ),
    ],
) -> StructuredText[ReadPdfPagesResult]:
    """Read PDF pages and return text, images, and strikethrough annotations.

    Reads the specified (or all) pages of a PDF and returns per-page text,
    image annotation keys (format 'page{N}_img{M}') for later retrieval
    via read_image, and any strikethrough annotations found. Filled
    interactive form (AcroForm) fields are listed by name under a
    'Form fields:' heading at the end of the page that holds them. Use this
    tool to read and analyze PDF content. For a password-protected PDF, pass
    its password in ``password``.
    """
    file_path = input.file_path
    pages = input.pages
    password = input.password

    if not file_path.startswith("/"):
        return _error("File path must start with /")
    if not file_path.lower().endswith(".pdf"):
        return _error("File path must end with .pdf")

    try:
        target_path = resolve_under_root(file_path)
    except PathTraversalError:
        return _error(f"Invalid path: {file_path}")

    try:
        if not os.path.exists(target_path):
            return _error(f"File not found: {file_path}")
        if not os.path.isfile(target_path):
            return _error(f"Not a file: {file_path}")

        # Check file size before loading
        file_size = os.path.getsize(target_path)
        if file_size > MAX_FILE_SIZE_BYTES:
            size_mb = file_size / (1024 * 1024)
            max_mb = MAX_FILE_SIZE_BYTES / (1024 * 1024)
            return _error(f"File too large: {size_mb:.1f}MB (max: {max_mb:.0f}MB)")

        # Read file bytes once, then use for both pypdf and fitz
        with open(target_path, "rb") as f:
            file_bytes = f.read()

        content = {}
        all_images = []
        all_strikethrough = []
        all_form_fields: list[FormFieldInfo] = []
        errors = []

        # Decrypt up front with pypdf; PyMuPDF below opens the same plain bytes.
        try:
            file_bytes, unlocked = unlock_pdf_bytes(file_bytes, password)
        except (PdfPasswordRequiredError, PdfPasswordIncorrectError) as exc:
            return _error(password_error_message(file_path, exc))
        cache_prefix = image_cache_prefix(target_path, password if unlocked else None)

        pdf_reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        try:
            total_pages = len(pdf_reader.pages)
        except FileNotDecryptedError:
            return _error(password_protected_error(file_path))

        # Determine which pages to read
        if pages is None or not pages:
            pages_to_read = list(range(1, total_pages + 1))
        else:
            pages_to_read = [p for p in pages if 1 <= p <= total_pages]

        # Open fitz document early for text extraction and strikethrough
        fitz_doc: fitz.Document | None = None
        try:
            fitz_doc = fitz.open(stream=file_bytes, filetype="pdf")
        except Exception as exc:
            errors.append(f"PyMuPDF open failed (will use pypdf only): {repr(exc)}")

        for page_num in pages_to_read:
            try:
                pypdf_page = pdf_reader.pages[page_num - 1]

                # Preserve the page's spatial layout so columns and tables remain
                # aligned instead of being flattened into reading-order fragments.
                layout_text = ""
                try:
                    apply_winansi_bullet_mapping(pypdf_page)
                    layout_text = (
                        pypdf_page.extract_text(extraction_mode="layout") or ""
                    )
                except Exception:
                    pass

                # PyMuPDF reads the whole page in reading order; it is both the
                # fallback for PDFs pypdf cannot extract at all and the reference
                # for how much of the page layout mode actually captured.
                # Position-sorted text is served only for pages layout
                # extraction cannot place -- those that paint into their own
                # gaps, and those that position text off the page entirely:
                # sorting interleaves multi-column layouts.
                reading_order_text = ""
                sorted_text = ""
                page_form_fields: list[FormFieldInfo] = []
                painted_field_chars = 0
                unoccluded = None
                offpage_lines = 0
                if fitz_doc is not None:
                    try:
                        fitz_page = fitz_doc[page_num - 1]
                        page_form_fields, painted_field_chars = (
                            _extract_form_fields_from_page(
                                fitz_doc, fitz_page, page_num, errors
                            )
                        )
                        reading_order_text = cast(str, fitz_page.get_text("text")) or ""
                        # Text the page covered over has to go before any of the
                        # extractors below are consulted: all of them read the
                        # content stream, so all of them report it.
                        unoccluded = occluded_text(fitz_page)
                        ink = page_ink(fitz_page)
                        offpage_lines = ink.offpage_lines
                        if unoccluded is None and (
                            offpage_lines or has_interleaved_text(fitz_page)
                        ):
                            # The clip keeps glyphs that overrun the page edge,
                            # which PyMuPDF drops by default and layout
                            # extraction -- what these pages are served instead
                            # of -- keeps. It stops at the ink that overruns,
                            # so runs the page positions off it entirely, which
                            # layout extraction splices into the line they share
                            # a baseline with, are left out of both.
                            sorted_text = (
                                cast(
                                    str,
                                    fitz_page.get_text(
                                        "text", sort=True, clip=ink.clip
                                    ),
                                )
                                or ""
                            )
                    except Exception:
                        pass

                text = layout_text
                layout_chars = _visible_char_count(layout_text)
                reading_order_chars = _visible_char_count(reading_order_text)
                # Widget appearance streams are content MuPDF reads and pypdf
                # structurally cannot, so counting them makes every filled form
                # look like a page layout mode truncated. Discounting them is
                # what keeps a filled form on layout extraction, which is the
                # only extractor that keeps a value on the same line as the
                # label the page content stream drew next to it. The discount
                # corrects a comparison, so it applies only where there is
                # layout text to compare: a page layout mode could not read at
                # all still falls back to reading-order text rather than being
                # taken for a scanned one.
                comparable_chars = reading_order_chars
                if layout_chars:
                    comparable_chars -= painted_field_chars
                if unoccluded is not None:
                    text = unoccluded.text
                    errors.append(
                        HIDDEN_TEXT_NOTE.format(
                            page_num=page_num, count=unoccluded.hidden_spans
                        )
                    )
                    logger.debug(
                        f"read_pdf_pages: page {page_num} paints over "
                        f"{unoccluded.hidden_spans} text span(s) with an opaque "
                        "fill, which every extractor still reports; returning the "
                        "page without them"
                    )
                elif _visible_char_count(sorted_text):
                    text = sorted_text
                    if offpage_lines:
                        errors.append(
                            OFFPAGE_TEXT_NOTE.format(
                                page_num=page_num, count=offpage_lines
                            )
                        )
                    logger.debug(
                        f"read_pdf_pages: page {page_num} paints text into a gap an "
                        "earlier run left, or positions it off the page entirely, "
                        "either of which layout extraction relocates into a line it "
                        "does not belong to; returning position-sorted text instead"
                    )
                elif (
                    reading_order_chars
                    and layout_chars < comparable_chars * LAYOUT_COMPLETENESS_RATIO
                ):
                    text = reading_order_text
                    logger.debug(
                        f"read_pdf_pages: page {page_num} layout extraction kept "
                        f"{layout_chars} of {reading_order_chars} characters; "
                        "returning reading-order text instead"
                    )

                # Asked about the text the page is actually being served, so it
                # covers every branch above. The guard compares the page's angled
                # runs against that text, so a branch that does carry a run --
                # plain reading-order text keeps each line whole -- raises no note
                # on its own, and the two that assemble by y band get one for the
                # runs they scattered. It is also given the glyphs this page
                # covered over, so an angled run the page hid is not read back
                # out as text the caller is missing.
                if fitz_doc is not None:
                    try:
                        for omitted in unplaced_rotated_text(
                            fitz_doc[page_num - 1],
                            text,
                            unoccluded.hidden
                            if unoccluded is not None
                            else frozenset(),
                        ):
                            errors.append(
                                ROTATED_TEXT_NOTE.format(
                                    page_num=page_num, text=omitted
                                )
                            )
                    except Exception as exc:
                        errors.append(f"Page {page_num} rotated text: {repr(exc)}")

                # Fall back to OCR for image-only/scanned pages when available
                if not text.strip() and ocr_available() and fitz_doc is not None:
                    try:
                        fitz_page = fitz_doc[page_num - 1]
                        mat = fitz.Matrix(2, 2)  # 2x zoom for better OCR
                        pix = fitz_page.get_pixmap(matrix=mat)
                        img_bytes = pix.tobytes("png")
                        ocr_text = ocr_page_image(img_bytes, format="PNG")
                        if ocr_text:
                            text = ocr_text
                            errors.append(OCR_TEXT_NOTE.format(page_num=page_num))
                    except Exception as exc:
                        errors.append(
                            f"Page {page_num}: OCR fallback failed: {repr(exc)}"
                        )

                if not text.strip():
                    ocr_reason = ocr_unavailable_reason()
                    if ocr_reason:
                        errors.append(
                            f"Page {page_num}: no text extracted (page may be scanned/image-only); {ocr_reason}"
                        )
                    else:
                        errors.append(
                            f"Page {page_num}: no text extracted (page may be scanned/image-only)"
                        )

                fields_block = _form_fields_block(page_form_fields)
                if fields_block:
                    text = f"{text}\n\n{fields_block}" if text.strip() else fields_block
                all_form_fields.extend(page_form_fields)

                content[page_num] = text

                # Extract images (uses pypdf)
                page_images = _extract_images_from_page(
                    pypdf_page, page_num, cache_prefix, errors
                )
                all_images.extend(page_images)

                # Extract annotation-based strikethrough (uses pypdf)
                page_strikethrough = _extract_strikethrough_from_pypdf_page(
                    pypdf_page, page_num, errors
                )
                all_strikethrough.extend(page_strikethrough)

                # Extract visual strikethrough (uses fitz)
                if fitz_doc is not None:
                    try:
                        fitz_page = fitz_doc[page_num - 1]
                        visual_strikethrough = _extract_visual_strikethrough_from_page(
                            fitz_page, page_num, errors
                        )
                        all_strikethrough.extend(visual_strikethrough)
                    except Exception as exc:
                        errors.append(
                            f"Visual strikethrough page {page_num}: {repr(exc)}"
                        )

            except Exception as exc:
                errors.append(f"Page {page_num}: {repr(exc)}")

        if fitz_doc is not None:
            try:
                fitz_doc.close()
            except Exception:
                pass

    except Exception as exc:
        return _error(f"Failed to process PDF: {repr(exc)}")

    try:
        result = PdfPagesRead(
            content=content,
            total_pages=total_pages,
            requested_pages=pages_to_read,
            images=all_images,
            strikethrough=all_strikethrough,
            form_fields=all_form_fields,
            errors=errors if errors else None,
        )
        return StructuredText(str(result), ReadPdfPagesResult.from_pages_read(result))
    except Exception as exc:
        return _error(f"Failed to create result: {repr(exc)}")
