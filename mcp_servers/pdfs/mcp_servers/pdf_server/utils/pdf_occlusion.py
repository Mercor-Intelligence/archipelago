"""Page text with the glyphs later opaque paint covered up removed.

A page can correct a figure by overprinting it: the stale run is drawn, an
opaque rectangle is filled over it, and the replacement is drawn almost on top.
A reader sees only the replacement. Every extractor in this server walks the
content stream instead, so both runs come back and a corrected cash balance
reads as ``$1,002$3,247`` -- characters that are all genuinely in the file, in
an arrangement the page does not show.

Paint order is what separates them. ``Page.get_texttrace`` reports each glyph
with the sequence number of the operation that drew it and ``Page.get_drawings``
numbers fills from the same counter, so a glyph is hidden exactly when an opaque
fill covering it carries a higher number. Background shading -- zebra rows,
header bars, highlighted cells -- is painted *before* the text that sits on it
and carries a lower one, so it never hides anything.

A scanned page shows a picture of its text and carries the transcription as an
invisible layer so the picture can be searched. Those glyphs draw nothing; their
boxes are the scanner's claim about where the ink in the picture is. Where the
claim holds, a fill over a layer glyph covers the picture ink it transcribes --
a redaction box or a patch over a scan hides that text from a reader exactly as
it hides painted text -- so the layer is held to paint order like any other
text. Where the claim is wrong -- a layer written into one corner of the sheet,
or at a fraction of page scale -- a fill lands on glyphs transcribing text from
somewhere else entirely, and dropping them deletes text the page plainly shows
while leaving what the fill really covered in place. `layer_on_page` checks the
claim against the rendered page, and only a layer that passes is occluded.

Pages that cover nothing up keep the extraction they get today: `occluded_text`
returns None for them and the caller's fast path is untouched. For the rest it
rebuilds the page from PyMuPDF's own characters with the hidden glyphs dropped,
through the same word grouping and line assembly ``get_text("text", sort=True)``
performs, so what the caller gets is that reading-sequence text minus what the
page covered up.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from typing import Any, cast

import fitz  # PyMuPDF

# How much of a glyph an opaque fill has to cover before the glyph counts as
# hidden. Across the 133 documents / 830 pages of the two worlds this was
# reported from, coverage of a glyph by a later opaque fill is bimodal: 257
# glyphs are grazed by under a tenth of their area -- rule lines, table borders
# and box edges clipping a descender -- and 92 are covered whole, with nothing
# in between. Any bound inside that gap separates them; this one sits at the top
# of the range the report asked for, seven times the largest incidental overlap
# measured and well clear of the full coverage a correction produces.
COVERAGE_THRESHOLD = 0.7

# PyMuPDF groups words into a line when their top or bottom coordinates differ
# by no more than this. Its own default for ``get_text(sort=True)``, repeated
# here because the assembly below has to agree with that function exactly.
LINE_TOLERANCE = 3.0

# ``get_texttrace`` reports a span's text render mode as its ``type``, and 3 is
# PDF's invisible mode -- the one a scanner's OCR writes its transcription in,
# under the picture of the page. Such a span paints nothing a fill could cover.
INVISIBLE_TEXT = 3

# A searchable layer is taken to sit on the picture it transcribes when at least
# this share of its letters and digits lands on ink that looks like text: some
# dark pixels, but not a solid block. Measured at 72 dpi: every page of layers
# tesseract writes over a rendered page, and of layers placed on their own
# glyphs, scores 0.95-1.0; the corner-written layers of the reported world's
# scans score 0.09-0.44 (a dark header band under the corner is solid ink, and
# is why solid blocks do not count). The bound sits in that gap.
LAYER_ON_PAGE_SHARE = 0.8

# What counts as text-like ink inside one glyph box, as the dark share of its
# pixels. Below the floor the box is blank paper; above the ceiling it is a
# filled area -- a header bar, a table shading -- that any box would land on.
TEXT_INK_FLOOR = 0.02
TEXT_INK_CEILING = 0.9

# Enough glyphs to judge a layer by, and a cap so a dense page is judged on a
# sample rather than every glyph. Below the floor there is nothing to judge, and
# the layer is held to paint order as before.
MIN_LAYER_SAMPLE = 10
MAX_LAYER_SAMPLE = 400

# A pixel this dark (0-255 grey) is ink.
INK_LEVEL = 128

type GlyphKey = tuple[str, float, float]
type _Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class UnoccludedPage:
    """A page's text with the glyphs later paint covered up removed.

    `hidden` is carried so anything else that reads the page's glyphs can agree
    with this one about which of them the page shows. Without it a second pass
    over the content stream reports ink this one deliberately dropped.
    """

    text: str
    hidden_spans: int
    hidden: frozenset[GlyphKey]


def glyph_key(char: str, origin: tuple[float, float]) -> GlyphKey:
    """Identify one painted glyph across PyMuPDF's two views of the page.

    ``get_texttrace`` carries paint order, ``get_text("rawdict")`` carries the
    line and word grouping; only the second can be assembled into text and only
    the first can say what is hidden. A glyph's character and pen position come
    from the same text matrix in both, so they join the two views -- unlike the
    bounding boxes, which rawdict widens to its line's full height.
    """
    return (char, round(origin[0], 3), round(origin[1], 3))


def paints_ink(span: dict[str, Any]) -> bool:
    """Does this ``get_texttrace`` span lay down ink, or only make a page searchable?"""
    return span.get("type") != INVISIBLE_TEXT


def layer_on_page(page: fitz.Page) -> bool:
    """Does the page's invisible searchable layer sit on the picture it transcribes?

    A layer written by OCR over a scan puts each glyph where the scanner read
    it, so its boxes land on the picture's ink. One that does not -- a
    transcription dumped into a corner of the sheet in a small font -- lands
    its boxes on margin, or on whatever ink happens to be there, and its
    positions say nothing about where on the page its text is.

    The page is rendered once at 72 dpi and the layer's letters and digits are
    sampled (punctuation is left out: a dot leader is all paper). A glyph under
    an opaque fill painted after it is skipped, since the render shows the fill
    there and not the picture. See `LAYER_ON_PAGE_SHARE` for the bound.

    Args:
        page: The PyMuPDF page.

    Returns:
        True when the page has no invisible text, when too little of it is
        uncovered to judge, or when it lands on the picture's ink; False when
        its positions do not describe the page.
    """
    fills = _opaque_fill_rects(page)
    glyphs: list[tuple[float, float, float, float]] = []
    for span in page.get_texttrace():
        if paints_ink(span):
            continue
        seqno = span.get("seqno")
        later = [
            fill for fill_seqno, fill in fills if seqno is None or fill_seqno > seqno
        ]
        for char in span.get("chars", []):
            if not chr(char[0]).isalnum():
                continue
            box = char[3]
            if any(
                fill[0] < box[2]
                and fill[2] > box[0]
                and fill[1] < box[3]
                and fill[3] > box[1]
                for fill in later
            ):
                continue
            glyphs.append(box)
    if len(glyphs) < MIN_LAYER_SAMPLE:
        return True
    step = max(1, len(glyphs) // MAX_LAYER_SAMPLE)
    sample = glyphs[::step]

    pixmap = page.get_pixmap(dpi=72, colorspace=fitz.csGRAY, alpha=False)
    samples = pixmap.samples
    width, height, stride = pixmap.width, pixmap.height, pixmap.stride
    scale_x = width / page.rect.width if page.rect.width else 1.0
    scale_y = height / page.rect.height if page.rect.height else 1.0
    origin = page.rect.tl
    # Glyph boxes are in the page's unrotated space; the render is rotated.
    # Bring each box into the render's space, or a /Rotate page's own,
    # registered layer is judged off its picture.
    rotate = page.rotation_matrix
    on_ink = judged = 0
    for box in sample:
        x0, y0, x1, y1 = fitz.Rect(box) * rotate if page.rotation else box
        px0 = max(0, int((x0 - origin.x) * scale_x))
        py0 = max(0, int((y0 - origin.y) * scale_y))
        px1 = min(width, int((x1 - origin.x) * scale_x) + 1)
        py1 = min(height, int((y1 - origin.y) * scale_y) + 1)
        if px1 <= px0 or py1 <= py0:
            judged += 1
            continue
        dark = sum(
            1
            for row in range(py0, py1)
            for value in samples[row * stride + px0 : row * stride + px1]
            if value < INK_LEVEL
        )
        share = dark / ((px1 - px0) * (py1 - py0))
        judged += 1
        if TEXT_INK_FLOOR < share < TEXT_INK_CEILING:
            on_ink += 1
    return on_ink >= LAYER_ON_PAGE_SHARE * judged


def _opaque_fill_rects(page: fitz.Page) -> list[tuple[int, _Box]]:
    """Every opaque rectangle the page fills, in the order it filled them in.

    Each rectangle is narrowed to the clip in force when it was painted: a fill
    issued inside a clipping path covers only their intersection, and claiming
    the whole rectangle would hide text the page still shows.
    """
    rects: list[tuple[int, _Box]] = []
    clips: list[tuple[int, fitz.Rect]] = []
    for path in page.get_drawings(extended=True):
        level = path.get("level", 0)
        while clips and clips[-1][0] >= level:
            clips.pop()
        kind = path.get("type")
        if kind in ("clip", "group"):
            scissor = path.get("scissor")
            if scissor is not None:
                clips.append((level, fitz.Rect(scissor)))
            continue
        if kind not in ("f", "fs") or path.get("fill") is None:
            continue
        # A translucent highlight leaves the text under it legible, so it is not
        # an occluder. PDF's default alpha is opaque, which is what a missing
        # value means.
        opacity = path.get("fill_opacity")
        if opacity is not None and opacity < 1.0:
            continue
        seqno = path.get("seqno")
        if seqno is None:
            continue
        # A fill-and-stroke path also paints its outline, centred on the edge,
        # so an opaque outline carries the cover half its width past the fill.
        # The reported table edit whites out its old grid that way: the old
        # bottom boundary sits a quarter point inside the fill's edge, and only
        # the white outline covers the rest of its stroke.
        reach = 0.0
        stroke_opacity = path.get("stroke_opacity")
        if (
            kind == "fs"
            and path.get("color") is not None
            and (stroke_opacity is None or stroke_opacity >= 1.0)
        ):
            reach = (path.get("width") or 0.0) / 2
        for item in path.get("items", []):
            # Rectangles only. The bounding box of a curve or a diagonal claims
            # ground the fill never covers, and over-claiming here deletes text
            # the page does show.
            if item[0] != "re":
                continue
            rect = fitz.Rect(item[1]) + (-reach, -reach, reach, reach)
            for _, scissor in clips:
                rect &= scissor
            if not rect.is_empty:
                rects.append((seqno, (rect.x0, rect.y0, rect.x1, rect.y1)))
    rects.sort(key=lambda fill: fill[0])
    return rects


def opaque_fills(page: fitz.Page) -> list[tuple[int, _Box]]:
    """The page's opaque rectangles as ``(seqno, box)``, in the order it filled them."""
    return _opaque_fill_rects(page)


def _covers(glyph: _Box, fill: _Box) -> bool:
    """Does `fill` cover enough of `glyph` for the glyph to be hidden?"""
    gx0, gy0, gx1, gy1 = glyph
    area = (gx1 - gx0) * (gy1 - gy0)
    if area <= 0:
        return False
    fx0, fy0, fx1, fy1 = fill
    overlap = max(0.0, min(gx1, fx1) - max(gx0, fx0)) * max(
        0.0, min(gy1, fy1) - max(gy0, fy0)
    )
    return overlap >= COVERAGE_THRESHOLD * area


def _hidden_glyphs(page: fitz.Page) -> tuple[set[GlyphKey], int]:
    """The glyphs an opaque fill painted over afterwards, and how many spans.

    An invisible searchable layer is considered only when `layer_on_page`
    finds it sitting on the picture it transcribes, for the reason the module
    docstring gives. The page is rendered for that check only once a layer
    glyph is found under a later fill, so a scan nothing paints over costs
    nothing extra.

    Returns:
        The keys of the hidden glyphs and the number of text spans they came
        from -- what the caller reports so the omission is not silent.
    """
    fills = _opaque_fill_rects(page)
    if not fills:
        return set(), 0
    # The fills are in paint order, so a span can bisect straight to the ones
    # drawn after it. Most pages shade a table before they typeset it, which
    # leaves that slice empty and settles the span in a comparison or two --
    # the difference between a constant cost per page and the product of its
    # glyph and fill counts.
    seqnos = [seqno for seqno, _ in fills]
    last_fill = seqnos[-1]

    spans = page.get_texttrace()
    per_span: list[set[GlyphKey]] = []
    flagged: Counter[GlyphKey] = Counter()
    layer_judged: bool | None = None
    for span in spans:
        seqno = span.get("seqno")
        if seqno is None or seqno >= last_fill:
            continue
        sx0, sy0, sx1, sy1 = span["bbox"]
        covering = [
            fill
            for _, fill in fills[bisect_right(seqnos, seqno) :]
            if fill[0] < sx1 and fill[2] > sx0 and fill[1] < sy1 and fill[3] > sy0
        ]
        if not covering:
            continue
        if not paints_ink(span):
            if layer_judged is None:
                layer_judged = layer_on_page(page)
            if not layer_judged:
                continue
        hidden = {
            glyph_key(chr(char[0]), char[2])
            for char in span.get("chars", [])
            if any(_covers(char[3], fill) for fill in covering)
        }
        if hidden:
            per_span.append(hidden)
            flagged.update(hidden)
    if not flagged:
        return set(), 0

    # Ink the page also lays down where nothing covers it stays on the page: a
    # glyph drawn twice at one position -- how some documents fake a bold
    # weight -- survives a fill over one of the two strokes. Counting every
    # occurrence is the only way to tell those apart, and is worth a second
    # walk only once a page is known to hide something.
    painted: Counter[GlyphKey] = Counter()
    for span in spans:
        if not paints_ink(span) and not layer_judged:
            continue
        for char in span.get("chars", []):
            key = glyph_key(chr(char[0]), char[2])
            if key in flagged:
                painted[key] += 1
    keys = {key for key, count in flagged.items() if count >= painted[key]}
    return keys, sum(1 for hidden in per_span if hidden & keys)


def _words(page: fitz.Page, hidden: set[GlyphKey]) -> list[tuple[fitz.Rect, str]]:
    """The page's words, as PyMuPDF splits them, without the hidden glyphs.

    Mirrors ``TextPage.extractWORDS``: characters are collected per line and cut
    at the delimiters MuPDF uses, and a word's box is the union of the boxes of
    the characters left in it.
    """
    raw = cast(
        dict[str, Any],
        page.get_text(
            "rawdict",
            flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES,
            # An unbounded clip keeps glyphs that overrun the page edge, which
            # PyMuPDF otherwise drops and wide table cells in these documents
            # rely on.
            clip=fitz.INFINITE_RECT(),
        ),
    )
    words: list[tuple[fitz.Rect, str]] = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            letters: list[str] = []
            box = fitz.EMPTY_RECT()
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    letter = char["c"]
                    if ord(letter) <= 32:
                        if letters and not box.is_empty:
                            words.append((box, "".join(letters)))
                        letters, box = [], fitz.EMPTY_RECT()
                        continue
                    if glyph_key(letter, char["origin"]) in hidden:
                        continue
                    letters.append(letter)
                    box |= fitz.Rect(char["bbox"])
            if letters and not box.is_empty:
                words.append((box, "".join(letters)))
    return words


def _in_reading_sequence(
    words: list[tuple[fitz.Rect, str]],
) -> list[tuple[fitz.Rect, str]]:
    """Order words down the page and left to right, as PyMuPDF's sort does."""
    ordered = sorted(words, key=lambda word: (word[0].y1, word[0].x0))
    out: list[tuple[fitz.Rect, str]] = []
    line = [ordered[0]]
    line_box = fitz.Rect(ordered[0][0])
    for word in ordered[1:]:
        box = word[0]
        if (
            abs(box.y0 - line_box.y0) <= LINE_TOLERANCE
            or abs(box.y1 - line_box.y1) <= LINE_TOLERANCE
        ):
            line.append(word)
            line_box |= box
        else:
            line.sort(key=lambda word: word[0].x0)
            out.extend(line)
            line = [word]
            line_box = fitz.Rect(box)
    line.sort(key=lambda word: word[0].x0)
    out.extend(line)
    return out


def _line_text(page_box: fitz.Rect, line: list[tuple[fitz.Rect, str]]) -> str:
    """One line of text, with the gaps between its words spelled in spaces."""
    line.sort(key=lambda word: word[0].x0)
    text = ""
    x1 = page_box.x0
    for box, word in line:
        gap = max(
            int(round((box.x0 - x1) / box.width * len(word))),
            0 if (x1 == page_box.x0 or box.x0 <= x1) else 1,
        )
        text += " " * gap + word
        x1 = box.x1
    return text


def _as_text(words: list[tuple[fitz.Rect, str]]) -> str:
    """Assemble ordered words into page text, the way PyMuPDF sorts text.

    Deliberately the algorithm of ``pymupdf.utils.get_sorted_text``, which the
    caller's existing position-sorted path already serves and which cannot be
    handed a filtered word list. ``test_pdf_occlusion`` pins the agreement: on
    any page that hides nothing, this has to return that function's output
    character for character.
    """
    if not words:
        return ""
    page_box = fitz.EMPTY_RECT()
    for box, _ in words:
        page_box |= box

    lines: list[tuple[fitz.Rect, str]] = []
    line = [words[0]]
    line_box = fitz.Rect(words[0][0])
    for box, word in words[1:]:
        if (
            abs(line_box.y0 - box.y0) <= LINE_TOLERANCE
            or abs(line_box.y1 - box.y1) <= LINE_TOLERANCE
        ):
            line.append((box, word))
            line_box |= box
        else:
            lines.append((line_box, _line_text(page_box, line)))
            line = [(box, word)]
            line_box = fitz.Rect(box)
    lines.append((line_box, _line_text(page_box, line)))
    lines.sort(key=lambda entry: entry[0].y1)

    text = lines[0][1]
    y1 = lines[0][0].y1
    for box, line_as_text in lines[1:]:
        breaks = min(int(round((box.y0 - y1) / box.height)), 5) + 1
        text += "\n" * breaks + line_as_text
        y1 = box.y1
    return text


def occluded_text(page: fitz.Page) -> UnoccludedPage | None:
    """The page's text without the glyphs later opaque paint covered up.

    Args:
        page: The PyMuPDF page about to be served.

    Returns:
        The remaining text and how many spans were dropped, or None when the
        page hides nothing -- in which case the caller must serve the page
        exactly as it does today. A page whose text is entirely covered (a
        redaction box over a whole paragraph) returns the empty string: the
        characters are in the file but the page does not show them, and the
        caller reports a page with no text rather than reading out what is
        under the box.
    """
    hidden, spans = _hidden_glyphs(page)
    if not hidden:
        return None
    words = _words(page, hidden)
    text = _as_text(_in_reading_sequence(words)) if words else ""
    return UnoccludedPage(text=text, hidden_spans=spans, hidden=frozenset(hidden))


@dataclass(frozen=True)
class OpaqueCover:
    """What a page's opaque fills hide from the ink painted before them.

    The glyph side of paint order is above; this is the same reading offered to
    anything else that walks the page's drawings. A page corrects a table the
    way it corrects a figure -- the stale rules are drawn, an opaque rectangle
    is filled over them, and the replacement is drawn on top -- so a reader
    that keeps every rule in the content stream reports lines the page does not
    show. The fills are the ones ``_opaque_fill_rects`` already finds, narrowed
    to the clip in force and with the translucent ones left out, so the two
    readings of a page agree about which paint is opaque.
    """

    fills: tuple[tuple[int, _Box], ...]

    def uncovered(
        self, seqno: int | None, x0: float, x1: float, y0: float, y1: float
    ) -> list[tuple[float, float]]:
        """The parts of the band ``[x0, x1] x [y0, y1]`` still on the page.

        Returns the x intervals of the band that no fill painted after `seqno`
        covers, so ink covered along part of its length keeps the rest. A fill
        counts only when it spans the band's whole height: a thin band is
        either shown or it is not, and a fill that merely meets its edge -- a
        shaded cell starting where a rule sits -- would otherwise take the
        whole rule with it.
        """
        if seqno is None:
            return [(x0, x1)]
        covering = sorted(
            (fill[0], fill[2])
            for fill_seqno, fill in self.fills
            if fill_seqno > seqno
            and fill[1] <= y0
            and fill[3] >= y1
            and fill[0] < x1
            and fill[2] > x0
        )
        spans: list[tuple[float, float]] = []
        start = x0
        for fill_x0, fill_x1 in covering:
            if fill_x0 > start:
                spans.append((start, min(fill_x0, x1)))
            start = max(start, fill_x1)
            if start >= x1:
                return spans
        spans.append((start, x1))
        return spans


def opaque_cover(page: fitz.Page) -> OpaqueCover:
    """The opaque fills `page` paints, ready to be asked what they cover."""
    return OpaqueCover(tuple(_opaque_fill_rects(page)))
