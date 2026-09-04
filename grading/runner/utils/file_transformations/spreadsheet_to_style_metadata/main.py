"""Extract cell- and sheet-level style metadata from a spreadsheet.

Formatting criteria (bold headers, fill colors, borders, hidden gridlines,
frozen panes, print settings) are otherwise judged either from the plain-value
text extraction (`spreadsheet_to_xml`, which carries no style info at all) or
from a rendered screenshot (which forces the judge to eyeball colors/bold
from pixels, and loses print-only sheet settings and any sheet the renderer
didn't get to). This gives the judge the exact underlying style facts
instead, addressable by the same cell references used elsewhere.

Output is deliberately bounded so it survives the judge's prompt budget:
per-cell detail covers the head of each sheet (where title bands and headers
live), and a per-sheet style digest then summarises *every* styled cell —
including rows not listed individually — so criteria phrased "…throughout
every worksheet" stay answerable. Cells with no notable style are skipped
entirely.
"""

import asyncio
import io
import zipfile
from collections import Counter
from pathlib import Path

import openpyxl
from loguru import logger
from openpyxl.cell import Cell
from openpyxl.cell.cell import MergedCell
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet.worksheet import Worksheet

from runner.evals.spreadsheet_verifier.formatting_checker import (
    get_color_hex,
    get_fill_color,
)

from ...file_extraction.utils.chart_extraction import (
    evaluate_excel_formulas_with_libreoffice,
    find_libreoffice,
)
from ..models import TransformationOutput
from ..spreadsheet_xml import _col_letter
from ..style_metadata_cache import cached_style_text
from ..xml_utils import xml_escape

_BORDER_SIDES = ("top", "bottom", "left", "right")

# Per-cell detail is emitted only for the top of each sheet — title bands,
# headers and section headings live there (in the workbook that motivated
# this, every navy band was on row 3 and the deepest header was row 58).
# Emitting every styled cell instead made a 17MB workbook produce 45MB of
# metadata, 94% of it from one 470k-row data dump; the prompt budget then
# kept 0.045% of it and only 1 of 8 worksheets survived, so criteria asking
# about "every worksheet" still failed. Rows past the head are covered by
# the style digest instead.
_HEAD_ROWS_PER_SHEET = 50

# Guard against a pathological sheet with per-cell unique formats. Real
# workbooks are nowhere near this (14 was the worst single sheet observed).
_MAX_DIGEST_ENTRIES = 24

# Character budget for per-cell head detail, split evenly across worksheets.
# sheet_properties and the digest are always emitted on top of this — they're
# small, bounded, and carry most of the answers (gridlines/freeze/filters/print
# come from properties; "…throughout every worksheet" comes from the digest),
# so the head detail is what gets rationed.
#
# Sized so the whole payload clears the judge prompt's style-metadata budget
# intact. That matters more than raw detail: the budget truncates a head/tail
# slice of the *combined* text, which on a multi-sheet workbook silently drops
# the middle sheets and breaks any criterion asking about "every worksheet".
# Note this XML runs ~2.7 chars/token (not the usual ~4), and Gemini judges
# apply a 1.9x conservative token multiplier on top — so the char figure is
# far tighter in tokens than it looks.
_HEAD_DETAIL_CHAR_BUDGET = 12_000

# Theme 1 ("Text 1", default black) with no tint is the boilerplate default
# nearly every unstyled cell inherits — openpyxl/Excel write it explicitly
# even with zero custom styling, so treat *only* that exact case as noise.
# Theme 0 ("Background 1", white) is deliberately NOT suppressed: white
# text is only ever used intentionally (e.g. white-on-navy header bands —
# exactly the case this module exists to catch), so it's real signal even
# though it's just as common a theme reference as the black default. Any
# nonzero tint on either index means the color was deliberately adjusted
# (e.g. a tinted gray), so it's kept regardless of theme index.
_DEFAULT_TEXT_THEME_INDEX = 1

# Agent-produced workbooks are frequently written with openpyxl rather than
# Excel/LibreOffice, which tends to set an explicit RGB black (rather than a
# theme reference) as the ambient default. Treat that the same as the
# theme-1/no-tint case above, for the same reason.
_DEFAULT_TEXT_RGB_HEX = "#000000"


def _notable_font_color(cell: Cell | MergedCell) -> str | None:
    if not cell.font or not cell.font.color:
        return None
    color = cell.font.color
    if (
        color.type == "theme"
        and color.theme == _DEFAULT_TEXT_THEME_INDEX
        and not (color.tint or 0)
    ):
        return None
    hex_color = get_color_hex(color)
    if hex_color and hex_color.upper() == _DEFAULT_TEXT_RGB_HEX:
        return None
    return hex_color


def _border_info(cell: Cell | MergedCell) -> tuple[bool, str | None]:
    """Return (has_border, color_of_first_styled_side)."""
    border = cell.border
    if border is None:
        return False, None
    for side_name in _BORDER_SIDES:
        side = getattr(border, side_name, None)
        if side is not None and side.style:
            return True, get_color_hex(side.color)
    return False, None


def _cell_style_attrs(cell: Cell | MergedCell) -> str:
    attrs: list[str] = []

    if cell.font and cell.font.bold:
        attrs.append('bold="true"')
    if cell.font and cell.font.italic:
        attrs.append('italic="true"')

    font_color = _notable_font_color(cell)
    if font_color:
        attrs.append(f'font_color="{font_color}"')

    fill_color = get_fill_color(cell)
    if fill_color:
        attrs.append(f'fill_color="{fill_color}"')

    has_border, border_color = _border_info(cell)
    if has_border:
        attrs.append('border="true"')
        if border_color:
            attrs.append(f'border_color="{border_color}"')

    if cell.number_format and cell.number_format != "General":
        attrs.append(f'number_format="{xml_escape(cell.number_format)}"')

    return (" " + " ".join(attrs)) if attrs else ""


def _sheet_properties_xml(ws: Worksheet) -> str:
    attrs: list[str] = []

    # Visible is the default; some tools (unlike openpyxl, which leaves the
    # field unset) explicitly serialize showGridLines="1" for that default
    # state. Only surface the hidden case — the only one that's ever
    # actually notable — so those files don't get marked "has formatting"
    # purely from boilerplate.
    if ws.sheet_view.showGridLines is False:
        attrs.append('gridlines_visible="false"')

    if ws.freeze_panes:
        attrs.append(f'frozen_panes="{ws.freeze_panes}"')

    if ws.auto_filter and ws.auto_filter.ref:
        attrs.append(f'auto_filter_range="{xml_escape(str(ws.auto_filter.ref))}"')

    orientation = ws.page_setup.orientation
    if orientation:
        attrs.append(f'print_orientation="{xml_escape(str(orientation))}"')

    if ws.print_title_rows:
        # Embeds the sheet name itself (e.g. 'Sales & Ops'!$1:$1), unlike
        # frozen_panes (a bare cell ref), so it needs escaping too.
        attrs.append(f'print_title_rows="{xml_escape(str(ws.print_title_rows))}"')

    if ws.sheet_state and ws.sheet_state != "visible":
        attrs.append(f'sheet_state="{xml_escape(str(ws.sheet_state))}"')

    zoom = ws.sheet_view.zoomScale
    if zoom is not None and zoom != 100:
        attrs.append(f'zoom_scale="{zoom}"')

    if not attrs:
        return ""
    return f"  <sheet_properties {' '.join(attrs)} />\n"


# Bound the emitted list; a sheet with more tables than this is already well
# past the point where an individual range matters to a criterion.
_MAX_TABLES_PER_SHEET = 24


def _sheet_tables_xml(ws: Worksheet) -> str:
    """Emit the sheet's native Excel Table objects.

    This is the one workbook fact a screenshot can never establish: a native
    Table and a manually formatted range with a sheet-level AutoFilter look
    identical when rendered. DEP-846 cites a criterion asking whether 20
    ranges "use native Excel Table objects", and another asking for each
    table's AutoFilter and row banding — both unanswerable from images, and
    both read straight off the table definition here.

    Emitted even when empty: "this sheet has no native tables" is the evidence
    that distinguishes a genuine miss from an unreported one.
    """
    tables = list((ws.tables or {}).values())
    if not tables:
        return '  <tables count="0" />\n'

    rows: list[str] = []
    for tbl in tables[:_MAX_TABLES_PER_SHEET]:
        attrs = (
            f'name="{xml_escape(str(tbl.displayName))}" '
            f'range="{xml_escape(str(tbl.ref))}"'
        )
        style = tbl.tableStyleInfo
        if style is not None:
            if style.name:
                attrs += f' style="{xml_escape(str(style.name))}"'
            attrs += f' row_stripes="{str(bool(style.showRowStripes)).lower()}"'
            attrs += f' column_stripes="{str(bool(style.showColumnStripes)).lower()}"'
        # A native Table carries its own AutoFilter, independent of the
        # sheet-level one reported in <sheet_properties>.
        attrs += f' has_auto_filter="{str(tbl.autoFilter is not None).lower()}"'
        rows.append(f"    <table {attrs} />")

    omitted = len(tables) - len(rows)
    tail = f"\n    <!-- {omitted} more tables omitted -->" if omitted > 0 else ""
    return (
        f'  <tables count="{len(tables)}">\n'
        + "\n".join(rows)
        + tail
        + "\n  </tables>\n"
    )


def _style_digest_xml(
    signatures: Counter[str], row_span: dict[str, tuple[int, int]], listed_rows: int
) -> str:
    """Summarise every styled cell in a sheet, including unlisted rows.

    Per-cell detail is only emitted for the first _HEAD_ROWS_PER_SHEET rows
    (that's where title bands and headers live). But criteria are often
    phrased "…applied throughout every worksheet", which a head slice can't
    answer. Real workbooks reuse a tiny number of distinct styles — a
    470k-row data dump was 5 signatures — so a digest describes 100% of the
    cells in a handful of lines, which answers "throughout" better than a
    truncated per-cell dump ever could.
    """
    if not signatures:
        return ""

    entries: list[str] = []
    for attrs, count in signatures.most_common(_MAX_DIGEST_ENTRIES):
        lo, hi = row_span[attrs]
        span = f"{lo}" if lo == hi else f"{lo}-{hi}"
        entries.append(f'    <style count="{count}" rows="{span}"{attrs} />')

    listed_combinations = len(entries)
    omitted = len(signatures) - listed_combinations
    if omitted > 0:
        entries.append(f"    <!-- {omitted} rarer style combinations omitted -->")

    # The note has to describe what was actually written. It used to claim full
    # coverage unconditionally, including directly above an "N rarer style
    # combinations omitted" comment — and since the judge is told to treat this
    # block as ground truth, the attribute is what it keys on, not the comment.
    # Claiming completeness after dropping N invites exactly the inference this
    # feature exists to remove: an unlisted style read as an absent one.
    if omitted > 0:
        note = (
            f"the {listed_combinations} most common of {len(signatures)} style "
            f"combinations in this sheet; {omitted} rarer combination(s) are not "
            f"listed, so absence here is not evidence a style is missing"
        )
    else:
        note = "covers every styled cell in this sheet, including rows not listed above"

    total = sum(signatures.values())
    return (
        f'  <style_digest total_styled_cells="{total}" '
        f'rows_listed_individually="{listed_rows}" '
        f'note="{note}">\n' + "\n".join(entries) + "\n  </style_digest>"
    )


def _collapse_row_xml(row_idx: int, styled: list[tuple[int, str]]) -> str:
    """Render one row, collapsing runs of identically-styled adjacent cells.

    Header rows are typically wide and uniformly styled (a 55-column navy
    title band was 55 separate lines before this). A range carries exactly
    the same information as the individual cells, so this is lossless.
    """
    entries: list[str] = []
    run_start = run_end = None
    run_sig: str | None = None

    def flush() -> None:
        if run_sig is None or run_start is None or run_end is None:
            return
        start_ref = f"{_col_letter(run_start)}{row_idx}"
        ref = (
            start_ref
            if run_start == run_end
            else f"{start_ref}:{_col_letter(run_end)}{row_idx}"
        )
        entries.append(f'    <cell ref="{ref}"{run_sig} />')

    for col_idx, sig in styled:
        if sig == run_sig and run_end is not None and col_idx == run_end + 1:
            run_end = col_idx
            continue
        flush()
        run_sig, run_start, run_end = sig, col_idx, col_idx
    flush()

    if not entries:
        return ""
    return f'  <row number="{row_idx}">\n' + "\n".join(entries) + "\n  </row>"


def _spreadsheet_to_style_xml(file_bytes: bytes) -> str:
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    parts: list[str] = []
    try:
        # Chart sheets (openpyxl Chartsheet) have no cells, sheet_view or
        # freeze_panes — reading style off one raises, which would otherwise
        # take down extraction for every OTHER sheet in the workbook too.
        worksheets = [ws for ws in wb.worksheets if isinstance(ws, Worksheet)]
        per_sheet_budget = _HEAD_DETAIL_CHAR_BUDGET // max(1, len(worksheets))

        for ws in worksheets:
            sheet_props_xml = _sheet_properties_xml(ws)

            rows_xml: list[str] = []
            signatures: Counter[str] = Counter()
            row_span: dict[str, tuple[int, int]] = {}
            listed_rows = 0
            head_chars = 0
            budget_spent = False

            for row_idx, row in enumerate(ws.iter_rows(min_row=1), start=1):
                styled: list[tuple[int, str]] = []
                for cell in row:
                    # Non-anchor cells of a merged range are MergedCell, not
                    # Cell — but Excel/openpyxl can still store real
                    # border/fill on them (e.g. the outer edges of a bordered
                    # merged header block), so they're included too.
                    if not isinstance(cell, Cell | MergedCell) or cell.column is None:
                        continue
                    style_attrs = _cell_style_attrs(cell)
                    if not style_attrs:
                        continue

                    # Every styled cell feeds the digest, at any depth, so
                    # "throughout"-style criteria stay answerable even for
                    # rows that never get listed individually.
                    signatures[style_attrs] += 1
                    lo, hi = row_span.get(style_attrs, (row_idx, row_idx))
                    row_span[style_attrs] = (min(lo, row_idx), max(hi, row_idx))
                    styled.append((cell.column, style_attrs))

                if budget_spent or row_idx > _HEAD_ROWS_PER_SHEET or not styled:
                    continue

                row_xml = _collapse_row_xml(row_idx, styled)
                if head_chars + len(row_xml) > per_sheet_budget:
                    # Stop listing rows for this sheet, but keep scanning so
                    # the digest still covers the whole sheet.
                    budget_spent = True
                    continue
                rows_xml.append(row_xml)
                head_chars += len(row_xml)
                listed_rows += 1

            # Tables alone are enough to describe a sheet, but a sheet with
            # nothing notable at all still stays out — <tables count="0"/> on
            # an empty sheet is noise, whereas on a populated one it is the
            # evidence that no range was made a native Table.
            has_tables = bool(ws.tables)
            if (
                not sheet_props_xml
                and not rows_xml
                and not signatures
                and not has_tables
            ):
                continue

            digest_xml = _style_digest_xml(signatures, row_span, listed_rows)

            escaped_name = xml_escape(ws.title)
            body = (
                sheet_props_xml
                + _sheet_tables_xml(ws)
                + "\n".join([*rows_xml, digest_xml]).strip("\n")
            )
            parts.append(f'<template sheet="{escaped_name}">\n{body}\n</template>')
    finally:
        wb.close()
    return "\n\n".join(parts)


async def _extract_style_text(file_bytes: bytes, file_name: str) -> str:
    # Parsing is CPU-bound and can take tens of seconds on a large workbook.
    # Run it off the event loop so it doesn't stall the concurrent LLM calls
    # of every other verifier in the same grading run.
    try:
        text = await asyncio.to_thread(_spreadsheet_to_style_xml, file_bytes)
    except (zipfile.BadZipFile, InvalidFileException):
        # openpyxl's reader only understands OOXML zip-based files. Verified
        # against the pinned openpyxl (3.1.5): OLE2 input — the shape a real
        # legacy .xls starts with — surfaces as zipfile.BadZipFile from the zip
        # layer. InvalidFileException is caught alongside it because that is
        # openpyxl's own "unsupported format" error, raised when it rejects a
        # file by extension, so the fallback does not depend on which of the two
        # a future version happens to pick.
        #
        # Still narrowly scoped on purpose: a real bug while parsing a valid
        # .xlsx should propagate as an error, not silently fall back to a
        # LibreOffice re-save (up to 120s, and can itself alter formatting)
        # whose output would then be presented to the judge as ground truth.
        logger.info(
            f"[TRANSFORM] {file_name} is not OOXML (likely legacy .xls) — "
            f"converting via LibreOffice before style extraction"
        )
        converted = await evaluate_excel_formulas_with_libreoffice(
            file_bytes, suffix=Path(file_name).suffix
        )
        if not converted:
            # A conversion that comes back empty is deterministic for this file,
            # not a transient hiccup — re-raising means the cache (which never
            # stores failures, on purpose) lets every later verifier pay another
            # 120s LibreOffice timeout for the same unconvertible workbook. Say
            # so instead, which both caches and tells the judge why there is no
            # style evidence rather than leaving the absence unexplained.
            logger.warning(
                f"[TRANSFORM] LibreOffice could not convert {file_name}; no "
                f"spreadsheet style metadata available"
            )
            # Degraded only when the renderer exists and still failed — that
            # may be a timeout or a non-zero exit, so it should be retried
            # rather than remembered. A missing binary is stable for the life of
            # the process, so that answer is cacheable like any other.
            degraded = ' data-degraded="true"' if find_libreoffice() else ""
            return (
                f'<style_metadata unsupported="true"{degraded}>This workbook '
                "could not be converted from its legacy format, so no style "
                "facts were extracted; draw no conclusion from their absence."
                "</style_metadata>\n"
            )
        text = await asyncio.to_thread(_spreadsheet_to_style_xml, converted)

    return text


async def spreadsheet_to_style_metadata(
    file_bytes: bytes, file_name: str
) -> TransformationOutput:
    """Extract sheet- and cell-level style metadata as judge-readable XML.

    Covers: bold/italic, font color, fill color, borders, number formats
    (cell-level) and gridline visibility, frozen panes, autofilter range,
    print orientation, print title rows, sheet visibility, zoom (sheet-level).

    Extraction is coalesced per grading run via the shared style-metadata
    cache — see style_metadata_cache for why that matters.
    """
    text = await cached_style_text(
        file_bytes, file_name, "spreadsheet", _extract_style_text
    )
    return TransformationOutput(text=text)
