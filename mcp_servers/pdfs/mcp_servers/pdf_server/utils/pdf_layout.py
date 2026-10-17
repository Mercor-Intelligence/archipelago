"""Whether pypdf's layout extraction can place a page's text where the page does.

pypdf's layout extractor advances its text matrix only on positioning operators.
``TextStateManager.text_state_params`` snapshots the current transform, and
nothing feeds the width of the glyphs a ``Tj`` just drew back into it, so every
run after the first in one BT/ET block reports the x of the first. A line
assembled from several runs is therefore concatenated with the gaps between them
closed, and a run the page paints *into* one of those gaps carries a real x that
sorts past the whole line, so it is appended at the line's end.

Both halves land on the same documents: a page that writes ``4``, skips half an
em, writes ``6``, then paints the range dash into the skipped space reads back as
``46`` with a stray ``-`` at the end of the line. Nothing is lost, so the
completeness ratio in ``read_pdf_pages`` cannot see it -- the characters are all
there, in an order that changes what they say.

`has_interleaved_text` reports that painting pattern from PyMuPDF's glyph
geometry, so the caller can serve position-sorted text for those pages instead.

The extractor also discards outright every run the page sets at an angle, which
no reordering can recover: a chart's rotated axis label and a diagonal watermark
are simply not in its output. `unplaced_rotated_text` reports those runs, so a
page served from layout extraction can still name what that choice cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import fitz  # PyMuPDF

# How far a run's baseline may sit from the run it is painted into. Infill glyphs
# are positioned by hand rather than by the body text's own line layout, so they
# land a fraction of a line off it -- the range dashes in the report that
# prompted this check sit 1.5pt below a 12.7pt line. The bound is a fraction of
# the surrounding glyph height so it scales with the type size instead of pinning
# one document's numbers.
BASELINE_TOLERANCE = 0.4

# Slack on the horizontal comparison, in points. A glyph painted into a gap
# commonly starts exactly where its left neighbour's advance ends, and that
# equality does not survive the rounding each side went through.
EDGE_TOLERANCE = 0.6

# PyMuPDF reports a line's writing direction as a unit vector: ordinary
# left-to-right text is (1, 0). A direction that carries any vertical component,
# or that runs right to left, is a run the page set at an angle. The bound is
# the rounding the unit vector itself went through, not a real angle -- half a
# degree of skew is still text pypdf's layout extractor strips.
DIRECTION_TOLERANCE = 1e-3


@dataclass(frozen=True)
class _Run:
    """One contiguous span of visible glyphs, with the page's own paint order."""

    baseline: float
    height: float
    x0: float
    x1: float
    glyphs: list[dict[str, Any]]


def _runs_in_paint_order(page: fitz.Page) -> list[_Run]:
    """Every visible glyph run on the page, in the order the page paints them.

    ``get_text("rawdict")`` reports blocks in content-stream order unless sorting
    is asked for, which is exactly the order pypdf's layout extractor walks.
    """
    runs: list[_Run] = []
    raw: dict[str, Any] = page.get_text(
        "rawdict", flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES
    )
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                glyphs = [char for char in span.get("chars", []) if char["c"].strip()]
                if not glyphs:
                    continue
                first, last = glyphs[0], glyphs[-1]
                runs.append(
                    _Run(
                        baseline=first["origin"][1],
                        height=first["bbox"][3] - first["bbox"][1],
                        x0=first["bbox"][0],
                        x1=last["bbox"][2],
                        glyphs=glyphs,
                    )
                )
    return runs


def _advancing_spans(runs: list[_Run]) -> list[list[_Run]]:
    """Split `runs`, in paint order, wherever the page jumps back to the left.

    This reconstructs the unit pypdf's layout extractor actually emits. It pads
    runs that keep advancing to the right into a single span, breaks a new one
    where a run starts behind the last one's end, and then orders the spans it
    produced by their starting x alone. So the ground a later run can be buried
    inside belongs to a whole advancing span, not to any single run -- and,
    equally, ink the page laid down *before* turning back to the left is not
    part of the span the turn-back began.
    """
    spans: list[list[_Run]] = []
    for run in runs:
        if spans and run.x0 >= spans[-1][-1].x1 - EDGE_TOLERANCE:
            spans[-1].append(run)
        else:
            spans.append([run])
    return spans


def _is_painted_into(earlier: list[_Run], later: _Run) -> bool:
    """Does `later` land inside ground one earlier span already covered, on its line?

    Being merely to the left of an earlier run is not enough -- a table drawn
    column by column does that constantly and extracts fine, because each cell
    still occupies horizontal space of its own. What layout extraction cannot
    place is a run that falls *inside* ground already covered.

    The glyphs bracketing it need not come from one earlier *run*, but they must
    come from one earlier *span*: PyMuPDF reports a padded span as two runs
    whenever the gap is wide enough to break it, which is why asking each run on
    its own misses the case this guard exists for -- a line drawn as
    `Author: Treasurer (`, a jump past room reserved for a name, `dual-role)`,
    then the name back into the gap. Requiring one span keeps a page typeset
    back to front out of it: there the bracketing ink straddles a jump the page
    made leftwards, pypdf orders those spans correctly, and claiming the page
    would splice its columns together for nothing.
    """
    on_line = [
        run
        for run in earlier
        if abs(run.baseline - later.baseline) <= BASELINE_TOLERANCE * run.height
    ]
    return any(
        any(
            glyph["bbox"][2] <= later.x0 + EDGE_TOLERANCE
            for run in span
            for glyph in run.glyphs
        )
        and any(
            glyph["bbox"][0] >= later.x1 - EDGE_TOLERANCE
            for run in span
            for glyph in run.glyphs
        )
        for span in _advancing_spans(on_line)
    )


def has_interleaved_text(page: fitz.Page) -> bool:
    """Does the page paint a glyph run into a gap an earlier run left?

    Args:
        page: The PyMuPDF page about to be served from layout extraction.

    Returns:
        True when at least one run is painted inside the horizontal span the runs
        drawn earlier on its line already cover -- the pattern pypdf's layout
        extractor relocates to the end of the line while closing the gap it came
        from.
    """
    runs = _runs_in_paint_order(page)
    return any(
        _is_painted_into(runs[:index], later) for index, later in enumerate(runs)
    )


def _is_set_at_an_angle(direction: Any) -> bool:
    """Is `direction` -- a PyMuPDF line `dir` vector -- anything but left to right?"""
    try:
        dx, dy = float(direction[0]), float(direction[1])
    except (TypeError, ValueError, IndexError):
        return False
    return abs(dy) > DIRECTION_TOLERANCE or dx <= 0


def unplaced_rotated_text(page: fitz.Page, layout_text: str) -> list[str]:
    """Text the page sets at an angle that `layout_text` does not carry.

    pypdf's layout extractor strips rotated runs rather than placing them, so a
    chart's axis label or a diagonal watermark leaves its output entirely. The
    loss is a few dozen characters on a page of a couple of thousand, which the
    completeness ratio in ``read_pdf_pages`` is built to tolerate, so nothing
    downstream can infer it from the character counts.

    Runs whose text layout extraction did keep are not reported: pypdf's notion
    of rotated and PyMuPDF's writing direction do not have to agree, and only
    the text actually missing from what the caller gets is worth naming.

    Args:
        page: The PyMuPDF page the layout text was extracted from.
        layout_text: The layout-mode text about to be served for that page.

    Returns:
        Each distinct angled run absent from `layout_text`, whitespace
        collapsed, in the order the page paints them.
    """
    placed = " ".join(layout_text.split())
    omitted: list[str] = []
    raw = cast(
        dict[str, Any],
        page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES),
    )
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            if not _is_set_at_an_angle(line.get("dir", (1.0, 0.0))):
                continue
            text = " ".join(
                "".join(span.get("text", "") for span in line.get("spans", [])).split()
            )
            if text and text not in placed and text not in omitted:
                omitted.append(text)
    return omitted
