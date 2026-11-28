"""Page text for a scan whose searchable layer does not sit on its picture.

A scanned page is a picture of its text plus an invisible transcription so the
picture can be searched. When the transcription's positions are the scanner's
reading of the picture, the rest of this server can treat it like any other
text. Some documents write it somewhere else entirely -- a small-font dump into
one corner of the sheet -- and keep only its *order*: the lines are the
picture's lines, top to bottom, but their positions say nothing about where the
page shows them (`pdf_occlusion.layer_on_page` is the test).

That is harmless until the page paints over its own picture. The reported page
patches a paragraph: an opaque white rectangle over the picture's superseded
sentences, the replacement typeset on top. A reader sees the replacement in the
section it patches and none of the superseded text. The layer still carries the
superseded sentences, at its own corner positions, and the replacement carries
real positions that line up with nothing in the layer -- so every extractor
either deletes the wrong layer text, splices the replacement into another
section, or appends it to the page and keeps the stale sentences as well.

The rendered page is the only thing that says where the layer's lines are. It
is read by OCR here, but only to *place* the layer: each layer line is matched
to the line of the render that reads the same, the lines the render does not
show at all and that sit where the page painted an opaque fill are dropped, and
the page's painted text goes in after the layer line above it. Every character
served is the PDF's own -- the OCR transcription is never served -- so the
misreads the OCR fallback warns about cannot reach the caller.
"""

from __future__ import annotations

import difflib
import re
import statistics
from dataclasses import dataclass, field
from typing import Any

import fitz  # PyMuPDF
from utils.ocr import ocr_page_lines
from utils.pdf_occlusion import (
    INK_LEVEL,
    GlyphKey,
    glyph_key,
    layer_on_page,
    opaque_fills,
    paints_ink,
)

# Resolution the page is rendered at for OCR. Tesseract reads body text cleanly
# from about 200 dpi; the render is only used to locate lines, never to read
# characters, so more costs time and buys nothing.
OCR_DPI = 200

# How alike a layer line and an OCR line must read to be the same line of the
# page, as difflib's ratio over their letters and digits. OCR misreads a
# character or two per line, and the layer may carry an artefact the render
# does not (a bullet glyph spelled out); unrelated lines of one document score
# well under this.
LINE_MATCH_RATIO = 0.6

# An OCR line is the page's painted text, not the picture's, when at least this
# much of it lies over the boxes of the painted lines.
PAINTED_OVERLAP = 0.5

# Layer lines the render shows nowhere are dropped only where the page could
# not be showing them: the stretch of page between the matched lines around them
# has to be blank paper once the opaque fills painted over the picture, and the
# page's own painted text, are taken out of it. The stretch counts as blank
# while its remaining ink is under this many squared line pitches -- a couple of
# glyphs' worth, so a stray descender poking in from the line above or a rule
# does not count, but a line OCR failed to read does.
STRAY_INK_PITCHES = 0.2
# Line pitch assumed when the render gives too few lines to measure one.
DEFAULT_PITCH = 12.0

# A jump of the pen this many font sizes wide inside a layer line, with nothing
# painted across it, is a hole in the transcription rather than a space: the
# layer's own font sets words a fraction of its size apart. The reported
# document's layer has such holes, where text the picture shows was cut out of
# the layer, and so does a layer line whose start sits that far right of the
# layer's left margin.
LAYER_GAP = 2.0

# The shortest run of characters a layer line and its rendered line must share
# to pin a hole's edges. A single shared letter matches somewhere in almost any
# stretch of text, and would put the transcription of the hole off by a word.
MIN_ANCHOR = 3

type _Box = tuple[float, float, float, float]


@dataclass
class _Line:
    """A line of glyphs assembled from ``get_texttrace``, with its extent."""

    text: str = ""
    box: _Box = (0.0, 0.0, 0.0, 0.0)
    baseline: float = 0.0
    size: float = 0.0
    last_x: float = 0.0
    start_x: float = 0.0
    chars: list[str] = field(default_factory=list)
    gaps: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class RealignedLayer:
    """The page's text with the layer placed where the rendered page shows it."""

    text: str
    dropped_lines: int
    ocr_runs: tuple[str, ...] = ()


def _union(a: _Box, b: _Box) -> _Box:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _lines(spans: list[dict[str, Any]], hidden: frozenset[GlyphKey]) -> list[_Line]:
    """Assemble glyphs into lines in the order the content stream paints them.

    A new line starts where the baseline moves by more than half the font size
    or the pen jumps back to the left; a gap wider than a quarter of the font
    size with no space glyph in it is spelled as a space. Gaps wider than
    `LAYER_GAP` font sizes are also recorded, as offsets into the line's text,
    and so is a line starting that far right of the leftmost line.
    """
    lines: list[_Line] = []
    current: _Line | None = None
    for span in spans:
        size = float(span.get("size") or 0.0) or 1.0
        for char in span.get("chars", []):
            letter = chr(char[0])
            origin = char[2]
            box = tuple(char[3])
            if glyph_key(letter, origin) in hidden:
                continue
            if (
                current is None
                or abs(origin[1] - current.baseline) > 0.5 * size
                or origin[0] < current.last_x - size
            ):
                current = _Line(
                    box=box,
                    baseline=origin[1],
                    size=size,
                    last_x=origin[0],
                    start_x=origin[0],
                )
                lines.append(current)
            elif not letter.isspace() and current.chars:
                jump = origin[0] - current.box[2]
                if not current.chars[-1].isspace() and jump > 0.25 * size:
                    current.chars.append(" ")
                if jump > LAYER_GAP * size:
                    current.gaps.append(len(current.chars))
            current.chars.append(letter)
            if not letter.isspace():
                current.box = _union(current.box, box)
            current.last_x = origin[0]
    margin = min((line.start_x for line in lines), default=0.0)
    for line in lines:
        raw = "".join(line.chars)
        lead = len(raw) - len(raw.lstrip())
        line.text = raw.strip()
        line.gaps = [
            gap - lead for gap in line.gaps if lead < gap < lead + len(line.text)
        ]
        if line.start_x - margin > LAYER_GAP * line.size:
            line.gaps.insert(0, 0)
    return [line for line in lines if line.text]


def _fill_gaps(text: str, gaps: list[int], rendered: str) -> tuple[str, list[str]]:
    """Fill the holes in a layer line from the rendered line that reads the same.

    Each hole is bounded by the nearest runs of at least `MIN_ANCHOR`
    characters the two lines share on either side, and what lies between those
    runs in the rendered line replaces what lies between them in the layer
    line. Only that stretch comes from OCR; the rest of the line is the layer's.

    Returns:
        The filled line, and the stretches taken from the rendered line.
    """
    matcher = difflib.SequenceMatcher(None, text, rendered, autojunk=False)
    blocks = [
        block for block in matcher.get_matching_blocks() if block.size >= MIN_ANCHOR
    ]
    filled: list[str] = []
    limit = len(text)
    for gap in sorted(set(gaps), reverse=True):
        left = [block for block in blocks if block.a + block.size <= gap]
        right = [block for block in blocks if block.a >= gap]
        if not right:
            continue
        before = max(left, key=lambda block: block.a) if left else None
        after = min(right, key=lambda block: block.a)
        start = before.a + before.size if before else 0
        start_rendered = before.b + before.size if before else 0
        end, end_rendered = after.a, after.b
        if end > limit or end_rendered <= start_rendered:
            continue
        run = rendered[start_rendered:end_rendered]
        if not run.strip() or _normal(run) == _normal(text[start:end]):
            continue
        text = text[:start] + run + text[end:]
        filled.append(run.strip())
        limit = start
    return text, filled[::-1]


def _normal(text: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.casefold()).split())


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if matcher.real_quick_ratio() < LINE_MATCH_RATIO:
        return 0.0
    if matcher.quick_ratio() < LINE_MATCH_RATIO:
        return 0.0
    return matcher.ratio()


def _align(layer: list[str], rendered: list[str]) -> dict[int, int]:
    """Match layer lines to rendered lines, keeping both in order.

    A weighted longest-common-subsequence: the pairing that keeps both
    sequences in order and maximises the summed similarity of matched lines,
    counting only pairs at or above `LINE_MATCH_RATIO`.
    """
    n, m = len(layer), len(rendered)
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    score: dict[tuple[int, int], float] = {}
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            value = max(best[i - 1][j], best[i][j - 1])
            similarity = _similarity(layer[i - 1], rendered[j - 1])
            if similarity >= LINE_MATCH_RATIO:
                score[(i - 1, j - 1)] = similarity
                value = max(value, best[i - 1][j - 1] + similarity)
            best[i][j] = value
    matches: dict[int, int] = {}
    i, j = n, m
    while i and j:
        pair = score.get((i - 1, j - 1))
        if pair is not None and best[i][j] == best[i - 1][j - 1] + pair:
            matches[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif best[i][j] == best[i - 1][j]:
            i -= 1
        else:
            j -= 1
    return matches


def _ink_outside(
    pixmap: fitz.Pixmap,
    scale: tuple[float, float],
    band: _Box,
    masks: list[_Box],
) -> int:
    """Count the dark pixels of `band` that no box in `masks` covers.

    Coordinates are page points; `scale` maps them to the pixmap's pixels.
    """
    scale_x, scale_y = scale
    x0 = max(0, int(band[0] * scale_x))
    x1 = min(pixmap.width, int(band[2] * scale_x) + 1)
    y0 = max(0, int(band[1] * scale_y))
    y1 = min(pixmap.height, int(band[3] * scale_y) + 1)
    pixel_masks = [
        (
            int(mask[0] * scale_x),
            int(mask[1] * scale_y),
            int(mask[2] * scale_x) + 1,
            int(mask[3] * scale_y) + 1,
        )
        for mask in masks
        if mask[1] < band[3] and mask[3] > band[1]
    ]
    samples, stride = pixmap.samples, pixmap.stride
    dark = 0
    for row in range(y0, y1):
        covered = [(m[0], m[2]) for m in pixel_masks if m[1] <= row < m[3]]
        line = samples[row * stride + x0 : row * stride + x1]
        for offset, value in enumerate(line):
            if value >= INK_LEVEL:
                continue
            column = x0 + offset
            if any(left <= column < right for left, right in covered):
                continue
            dark += 1
    return dark


def layer_off_page_under_paint(page: fitz.Page) -> bool:
    """Does the page paint over a picture whose searchable layer is elsewhere?

    True when the page carries an invisible layer that `layer_on_page` finds
    does not sit on the picture, and the page also paints something of its own
    over it -- visible text, or an opaque fill after the layer. A scan with
    nothing painted over it is served from the layer as it stands: its order
    is the picture's order, and nothing on the page contradicts it.
    """
    layer_seqnos: list[int] = []
    painted = False
    for span in page.get_texttrace():
        if not span.get("chars"):
            continue
        if paints_ink(span):
            painted = True
        else:
            seqno = span.get("seqno")
            layer_seqnos.append(seqno if seqno is not None else 0)
    if not layer_seqnos:
        return False
    if not painted:
        first = min(layer_seqnos)
        if not any(seqno > first for seqno, _ in opaque_fills(page)):
            return False
    return not layer_on_page(page)


def unplaced_layer_text(
    page: fitz.Page, hidden: frozenset[GlyphKey] = frozenset()
) -> str:
    """The layer's lines in their own order, then the page's painted text.

    What a page `realigned_layer_text` could not place is served as: the two
    texts kept apart rather than assembled onto one grid, painted glyphs the
    page covered over left out.
    """
    spans = page.get_texttrace()
    layer = _lines([span for span in spans if not paints_ink(span)], frozenset())
    painted = _lines([span for span in spans if paints_ink(span)], hidden)
    return "\n".join(line.text for line in [*layer, *painted])


def _estimated_anchor(
    layer: list[_Line],
    at: int,
    run_start: int,
    run_end: int,
    above: _Box | None,
    below: _Box | None,
    pitch: float,
) -> _Box | None:
    """Where on the page a layer line OCR did not match most likely sits.

    The line has no rendered box of its own, so it is placed between the
    matched lines around its run in proportion to where the layer itself puts
    it between them. The layer is misregistered by a scale and an offset, not
    reordered, so that proportion carries over to the page. Borrowing the box
    of the line above instead would slot painted text that lies between that
    line and this one after this one.

    At either end of the page, with only one matched neighbour, the line is
    stepped a rendered line pitch per line away from it.

    Args:
        layer: The layer's lines, in their own order.
        at: The index of the unmatched line to place.
        run_start: The first index of the unmatched run `at` belongs to.
        run_end: One past the last index of that run.
        above: The rendered box of the matched line before the run, if any.
        below: The rendered box of the matched line after the run, if any.
        pitch: The rendered line pitch, in points.

    Returns:
        An estimated box on the page, or None when the run has no matched
        neighbour at all.
    """
    if above is None and below is None:
        return None
    if above is not None and below is not None:
        first = layer[run_start - 1].box[1]
        last = layer[run_end].box[1]
        if last > first:
            fraction = (layer[at].box[1] - first) / (last - first)
        else:
            fraction = (at - run_start + 1) / (run_end - run_start + 1)
        fraction = min(max(fraction, 0.0), 1.0)
        top = above[1] + fraction * (below[1] - above[1])
        neighbour = above
    elif above is not None:
        top = above[1] + pitch * (at - run_start + 1)
        neighbour = above
    else:
        assert below is not None
        top = below[1] - pitch * (run_end - at)
        neighbour = below
    height = neighbour[3] - neighbour[1]
    return (neighbour[0], top, neighbour[2], top + height)


def realigned_layer_text(
    page: fitz.Page, hidden: frozenset[GlyphKey] = frozenset()
) -> RealignedLayer | None:
    """The page's text as the rendered page shows it, from the PDF's own glyphs.

    Args:
        page: A page `layer_off_page_under_paint` holds true for.
        hidden: Painted glyphs `pdf_occlusion` found under later opaque paint,
            which stay out of the painted text here too.

    Returns:
        The layer's lines in their own order, less those the page covers over,
        with the painted text placed after the layer line above it -- or None
        when the page is rotated, OCR is unavailable or fails, or no line of
        the render can be matched to the layer, in which case the caller keeps
        its own reading.
    """
    if page.rotation:
        return None
    spans = page.get_texttrace()
    layer_spans = [span for span in spans if not paints_ink(span)]
    layer = _lines(layer_spans, frozenset())
    painted = _lines([span for span in spans if paints_ink(span)], hidden)
    if not layer:
        return None

    pixmap = page.get_pixmap(dpi=OCR_DPI, colorspace=fitz.csGRAY, alpha=False)
    ocr = ocr_page_lines(pixmap.tobytes("png"))
    if not ocr:
        return None
    scale_x = page.rect.width / pixmap.width
    scale_y = page.rect.height / pixmap.height
    origin = page.rect.tl
    rendered: list[tuple[str, _Box]] = []
    for text, (x0, y0, x1, y1) in ocr:
        box = (
            origin.x + x0 * scale_x,
            origin.y + y0 * scale_y,
            origin.x + x1 * scale_x,
            origin.y + y1 * scale_y,
        )
        area = (box[2] - box[0]) * (box[3] - box[1])
        over_painted = sum(
            max(0.0, min(box[2], line.box[2]) - max(box[0], line.box[0]))
            * max(0.0, min(box[3], line.box[3]) - max(box[1], line.box[1]))
            for line in painted
        )
        if area > 0 and over_painted >= PAINTED_OVERLAP * area:
            continue
        rendered.append((text, box))

    matches = _align(
        [_normal(line.text) for line in layer],
        [_normal(text) for text, _ in rendered],
    )
    if not matches:
        return None

    tops = sorted(box[1] for _, box in rendered)
    pitches = [b - a for a, b in zip(tops, tops[1:], strict=False) if b - a > 1.0]
    pitch = statistics.median(pitches) if pitches else DEFAULT_PITCH

    first_layer_seqno = min((span.get("seqno") or 0 for span in layer_spans), default=0)
    later_fills = [
        fill for seqno, fill in opaque_fills(page) if seqno > first_layer_seqno
    ]
    masks = later_fills + [line.box for line in painted]
    pixel_scale = (1 / scale_x, 1 / scale_y)
    stray_ink = STRAY_INK_PITCHES * (pitch / scale_x) * (pitch / scale_y)

    anchors: list[_Box | None] = [None] * len(layer)
    for index, at in matches.items():
        anchors[index] = rendered[at][1]
    keep = [True] * len(layer)
    dropped = 0
    index = 0
    while index < len(layer):
        if index in matches:
            index += 1
            continue
        run_end = index
        while run_end < len(layer) and run_end not in matches:
            run_end += 1
        above = anchors[index - 1] if index > 0 else None
        below = anchors[run_end] if run_end < len(layer) else None
        # The stretch between the lines around the run, a point in from each
        # so their own descenders and ascenders stay out of it.
        band = (
            page.rect.x0 - origin.x,
            (above[3] if above is not None else page.rect.y0) + 1.0 - origin.y,
            page.rect.x1 - origin.x,
            (below[1] if below is not None else page.rect.y1) - 1.0 - origin.y,
        )
        covered = any(
            fill[1] < band[3] + origin.y and fill[3] > band[1] + origin.y
            for fill in later_fills
        )
        if (
            covered
            and band[3] > band[1]
            and _ink_outside(
                pixmap,
                pixel_scale,
                band,
                [
                    (m[0] - origin.x, m[1] - origin.y, m[2] - origin.x, m[3] - origin.y)
                    for m in masks
                ],
            )
            < stray_ink
        ):
            for hidden_index in range(index, run_end):
                keep[hidden_index] = False
            dropped += run_end - index
        for kept_index in range(index, run_end):
            anchors[kept_index] = _estimated_anchor(
                layer, kept_index, index, run_end, above, below, pitch
            )
        index = run_end

    blocks: list[tuple[_Box, list[str]]] = []
    for line in painted:
        if blocks:
            box, texts = blocks[-1]
            height = line.box[3] - line.box[1]
            if (
                line.box[1] - box[3] <= height
                and line.box[0] < box[2]
                and line.box[2] > box[0]
            ):
                blocks[-1] = (_union(box, line.box), [*texts, line.text])
                continue
        blocks.append((line.box, [line.text]))

    after: dict[int, list[tuple[_Box, list[str]]]] = {}
    for box, texts in blocks:
        # The layer line above the block, preferring one in the same column:
        # the layer is in reading order, so on a page set in columns the last
        # line above the block in sequence can be at the foot of another one.
        above_any = above_column = -1
        for index, anchor in enumerate(anchors):
            if not keep[index] or anchor is None or anchor[1] >= box[1]:
                continue
            above_any = index
            if anchor[0] < box[2] and anchor[2] > box[0]:
                above_column = index
        slot = above_column if above_column >= 0 else above_any
        after.setdefault(slot, []).append((box, texts))

    out: list[str] = []
    ocr_runs: list[str] = []
    for _box, texts in sorted(after.get(-1, []), key=lambda block: block[0][1]):
        out.extend(texts)
    for index, line in enumerate(layer):
        if keep[index]:
            text = line.text
            if line.gaps and index in matches:
                text, runs = _fill_gaps(text, line.gaps, rendered[matches[index]][0])
                ocr_runs.extend(runs)
            out.append(text)
        for _box, texts in sorted(after.get(index, []), key=lambda block: block[0][1]):
            out.extend(texts)
    return RealignedLayer(
        text="\n".join(out), dropped_lines=dropped, ocr_runs=tuple(ocr_runs)
    )
