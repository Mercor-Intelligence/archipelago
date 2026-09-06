"""Extract cell- and sheet-level style metadata from a spreadsheet.

Gives the judge the underlying style facts — bold, fills, borders, gridlines,
frozen panes, print settings — addressable by the same cell references used
elsewhere, rather than leaving it to eyeball them from a screenshot.

See ../README.md for the rules these extractors share: resolve references to
the value that renders, name the source, never invent one, and cap every
section with the cap disclosed.

Output is deliberately bounded so it survives the judge's prompt budget:
per-cell detail covers the head of each sheet (where title bands and headers
live), and a per-sheet style digest then summarises *every* styled cell —
including rows not listed individually — so criteria phrased "…throughout
every worksheet" stay answerable. Cells with no notable style are skipped
entirely.
"""

import asyncio
import colorsys
import io
import re
import zipfile
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from xml.etree import ElementTree

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
from ..xml_utils import DML_NS, PKG_RELS_NS, xml_escape

_BORDER_SIDES = ("top", "bottom", "left", "right")

# Per-cell detail covers only the top of each sheet, where title bands and
# headers live; rows past it are covered by the style digest. Emitting every
# styled cell turned a 17MB workbook into 45MB of metadata, most of it one
# 470k-row data dump.
_HEAD_ROWS_PER_SHEET = 50

# Guard against a pathological sheet with per-cell unique formats. Real
# workbooks are nowhere near this (14 was the worst single sheet observed).
_MAX_DIGEST_ENTRIES = 24

# Character budget for per-cell head detail, split evenly across worksheets.
# Only the head detail is rationed; sheet_properties and the digest are small,
# bounded and always emitted. Kept low because the prompt budget truncates a
# head/tail slice of the COMBINED text, which would silently drop the middle
# sheets of a multi-sheet workbook — and this XML runs ~2.7 chars/token rather
# than the usual ~4.
_HEAD_DETAIL_CHAR_BUDGET = 12_000

# Theme 1 (default black) with no tint is boilerplate Excel writes into
# unstyled cells, so only that exact case is suppressed as noise. Theme 0
# (white) is kept — white text is always deliberate — and any nonzero tint
# means the colour was adjusted, so it is kept on either index.
_DEFAULT_TEXT_THEME_INDEX = 1

# Agent-produced workbooks are frequently written with openpyxl rather than
# Excel/LibreOffice, which tends to set an explicit RGB black (rather than a
# theme reference) as the ambient default. Treat that the same as the
# theme-1/no-tint case above, for the same reason.
_DEFAULT_TEXT_RGB_HEX = "#000000"


# Theme colour indices in openpyxl order, which is NOT the order clrScheme
# lists them in: index 0 is lt1 and 1 is dk1, likewise 2/3. Used to turn a
# "theme:4" reference into the hex it denotes.
_THEME_SLOT_ORDER = (
    "lt1",
    "dk1",
    "lt2",
    "dk2",
    "accent1",
    "accent2",
    "accent3",
    "accent4",
    "accent5",
    "accent6",
    "hlink",
    "folHlink",
)
_THEME_REF_RE = re.compile(r"^theme:(?P<index>\d+):(?P<tint>-?[\d.]+)$")


# Matches the theme part itself. A "_rels" entry and a bare directory entry
# both sort before "theme1.xml", so the first name under xl/theme/ is not
# necessarily the theme.
_THEME_PART_RE = re.compile(r"^xl/theme/theme(\d+)\.xml$")


def _theme_part_name(zf: zipfile.ZipFile) -> str | None:
    """The theme part the WORKBOOK references, else the lowest-numbered one.

    Editing a workbook can leave an orphaned theme1.xml behind while the
    workbook actually points at theme2.xml, and picking by number then reads a
    palette and font scheme the file does not use. The relationship is the
    authority; the numeric pick stays only as a fallback for packages whose
    rels cannot be read.
    """
    parts = [(m, n) for n in zf.namelist() if (m := _THEME_PART_RE.match(n))]
    if not parts:
        return None
    try:
        rels = ElementTree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        for rel in rels.iter(f"{PKG_RELS_NS}Relationship"):
            if not (rel.get("Type") or "").endswith("/theme"):
                continue
            target = rel.get("Target") or ""
            name = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            if name in zf.namelist():
                return name
    except (KeyError, ElementTree.ParseError):
        pass
    return min(parts, key=lambda p: int(p[0].group(1)))[1]


def _theme_face_map(zf: zipfile.ZipFile) -> dict[str, str]:
    """{"major": face, "minor": face} from the workbook theme's font scheme."""
    part = _theme_part_name(zf)
    if part is None:
        return {}
    try:
        root = ElementTree.fromstring(zf.read(part))
    except (KeyError, ElementTree.ParseError):
        return {}
    scheme = root.find(f".//{DML_NS}fontScheme")
    out: dict[str, str] = {}
    for slot in ("major", "minor"):
        latin = (
            scheme.find(f"{DML_NS}{slot}Font/{DML_NS}latin")
            if scheme is not None
            else None
        )
        typeface = latin.get("typeface") if latin is not None else None
        if typeface:
            out[slot] = typeface.strip()
    return out


def _rich_text_runs(
    cell: Cell | MergedCell,
) -> list[tuple[str | None, float | None, str | None]]:
    """(raw face, size, scheme slot) per run, None where the run states none.

    A cell can carry several fonts across its runs and the cell-level font is
    only the default among them, so a flattened cell reported one face and
    "all body text is Verdana" read a mixed cell as compliant. Each run is
    returned raw — a run naming no face renders in the cell's, and a run naming
    no size renders at the cell's — so the caller can fill both from the cell
    and resolve the face the same way it resolves a cell-level one. The scheme
    slot rides along for that resolution: a run can name a theme slot and a
    stale literal at once, and the slot is the one that renders. Returns []
    for a cell that is not rich text.
    """
    value = getattr(cell, "value", None)
    if value is None or isinstance(value, str) or not isinstance(value, Iterable):
        return []
    runs: list[tuple[str | None, float | None, str | None]] = []
    for block in value:
        font = getattr(block, "font", None)
        name = getattr(font, "rFont", None) if font is not None else None
        size = getattr(font, "sz", None) if font is not None else None
        slot = getattr(font, "scheme", None) if font is not None else None
        runs.append(
            (
                str(name).strip() if name else None,
                float(size) if size else None,
                str(slot) if slot else None,
            )
        )
    return runs


def _has_face(cell: Cell | MergedCell, theme_map: dict[str, str] | None = None) -> bool:
    """Whether this cell's font names a face, directly or via the theme.

    A font can carry scheme="major"/"minor" with no name at all, so the slot
    is consulted too — but it has to resolve, or there is no face to report.
    """
    font = cell.font
    if not font:
        return False
    if font.name:
        return True
    slot = getattr(font, "scheme", None)
    return slot in ("major", "minor") and bool((theme_map or {}).get(slot))


def _face_from(
    name: str | None,
    slot: str | None,
    theme_map: dict[str, str],
    theme_faces: frozenset[str],
) -> tuple[str, bool]:
    """The face a (name, scheme) pair renders in, and whether it was indirect.

    A font carrying scheme="major"/"minor" renders the theme face whatever
    name is stored beside it; Excel leaves that name stale when the theme
    changes, so it is a reference rather than the answer.

    Cell fonts and rich-text run fonts both carry the pair, so both resolve
    here. A run resolving on its own name alone reported the stale spelling
    the theme slot exists to correct -- the very confusion ("Verdana" vs
    "Verdana (Body)") this module was changed to end.
    """
    stored = str(name or "").strip()
    if slot in ("major", "minor") and theme_map.get(slot):
        face = theme_map[slot]
        return face, face.lower() != stored.lower()
    return _normalized_face(stored, theme_faces)


def _resolved_face(
    cell: Cell | MergedCell, theme_map: dict[str, str], theme_faces: frozenset[str]
) -> tuple[str, bool]:
    """The face a cell renders in, and whether it was recorded indirectly."""
    return _face_from(
        cell.font.name, getattr(cell.font, "scheme", None), theme_map, theme_faces
    )


def _theme_faces(zf: zipfile.ZipFile) -> frozenset[str]:
    """The workbook theme's major/minor Latin typefaces, lower-cased.

    Used to prove a name like "Verdana (Body)" really denotes a theme face
    before folding it, rather than stripping the suffix off anything that
    happens to end that way.
    """
    part = _theme_part_name(zf)
    if part is None:
        return frozenset()
    try:
        root = ElementTree.fromstring(zf.read(part))
    except (KeyError, ElementTree.ParseError):
        return frozenset()
    faces: set[str] = set()
    scheme = root.find(f".//{DML_NS}fontScheme")
    for slot in ("major", "minor"):
        latin = (
            scheme.find(f"{DML_NS}{slot}Font/{DML_NS}latin")
            if scheme is not None
            else None
        )
        typeface = latin.get("typeface") if latin is not None else None
        if typeface:
            faces.add(typeface.strip().lower())
    return frozenset(faces)


def _theme_palette(file_bytes: bytes) -> list[str]:
    """The workbook's colour scheme as hex, in Excel's theme-index order."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes), "r") as zf:
            part = _theme_part_name(zf)
            if part is None:
                return []
            root = ElementTree.fromstring(zf.read(part))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        return []

    by_slot: dict[str, str] = {}
    scheme = root.find(f".//{DML_NS}clrScheme")
    for child in scheme if scheme is not None else []:
        slot = child.tag.rsplit("}", 1)[-1]
        srgb = child.find(f"{DML_NS}srgbClr")
        if srgb is not None and srgb.get("val"):
            by_slot[slot] = str(srgb.get("val"))
            continue
        # dk1/lt1 are usually system colours; lastClr carries the concrete value.
        sys_clr = child.find(f"{DML_NS}sysClr")
        if sys_clr is not None and sys_clr.get("lastClr"):
            by_slot[slot] = str(sys_clr.get("lastClr"))
    return [by_slot.get(slot, "") for slot in _THEME_SLOT_ORDER]


# Excel works in HLS on a 0..240 integer scale, not on floats (MS-OI29500
# §2.1.750). Applying the tint to a float luminance and rounding only at the
# end lands one step off on many colours, and these values are graded against
# criteria that name an exact hex — so an off-by-one here IS a false flag.
_HLS_MAX = 240


def _apply_tint(hex_rgb: str, tint: float) -> str:
    """Excel's tint, applied to luminance on Excel's own 240-step HLS scale."""
    if not tint:
        return hex_rgb
    r, g, b = (int(hex_rgb[i : i + 2], 16) / 255 for i in (0, 2, 4))
    hue, lum, sat = colorsys.rgb_to_hls(r, g, b)
    lum = round(lum * _HLS_MAX)
    if tint < 0:
        lum = lum * (1 + tint)
    else:
        lum = lum * (1 - tint) + (_HLS_MAX - _HLS_MAX * (1 - tint))
    lum = max(0, min(_HLS_MAX, round(lum)))
    r, g, b = colorsys.hls_to_rgb(hue, lum / _HLS_MAX, sat)
    return f"{round(r * 255):02X}{round(g * 255):02X}{round(b * 255):02X}"


def _resolved_color(
    value: str | None, palette: list[str], exact_tint: float | None = None
) -> str | None:
    """Turn a "theme:N:tint" reference into the hex it denotes.

    exact_tint comes from the cell's own Color when the caller has it. The
    reference string carries the tint rounded to two decimals, and Excel writes
    tints like 0.499984740745262 for "50%" — rounding that shifts the result by
    a step or two per channel, which an exact-hex criterion counts as a miss.
    """
    if not value:
        return value
    match = _THEME_REF_RE.match(value)
    if not match:
        return value
    index = int(match.group("index"))
    if not palette or index >= len(palette) or not palette[index]:
        # Unresolvable: keep the reference rather than inventing a colour, so
        # the judge sees an unmatched slot instead of a confident wrong hex.
        return value
    tint = float(match.group("tint")) if exact_tint is None else exact_tint
    return f"#{_apply_tint(palette[index], tint)}"


def _notable_font_color(
    cell: Cell | MergedCell, palette: list[str] | None = None
) -> str | None:
    if not cell.font or not cell.font.color:
        return None
    color = cell.font.color
    hex_color = _resolved_color(
        get_color_hex(color), palette or [], getattr(color, "tint", None)
    )
    if hex_color and hex_color.startswith("#"):
        # Resolved, so judge it on the colour itself. Keying on the slot index
        # instead suppressed a workbook that customised slot 1 to something
        # other than black, losing the colour it actually renders.
        return None if hex_color.upper() == _DEFAULT_TEXT_RGB_HEX else hex_color
    # Unresolved (no readable theme): fall back to treating an untinted
    # slot-1 reference as the boilerplate default Excel writes everywhere.
    if (
        color.type == "theme"
        and color.theme == _DEFAULT_TEXT_THEME_INDEX
        and not (color.tint or 0)
    ):
        return None
    return hex_color


def _border_info(
    cell: Cell | MergedCell, palette: list[str] | None = None
) -> tuple[bool, str | None]:
    """Return (has_border, color_of_first_styled_side)."""
    border = cell.border
    if border is None:
        return False, None
    for side_name in _BORDER_SIDES:
        side = getattr(border, side_name, None)
        if side is not None and side.style:
            return True, _resolved_color(
                get_color_hex(side.color),
                palette or [],
                getattr(side.color, "tint", None),
            )
    return False, None


def _cell_style_attrs(
    cell: Cell | MergedCell,
    palette: list[str] | None = None,
    theme_faces: frozenset[str] = frozenset(),
    theme_map: dict[str, str] | None = None,
) -> str:
    """Style attributes for one cell, or "" when the cell is not worth emitting.

    Two distinct questions, deliberately kept apart: whether a cell is notable
    enough to list at all, and what to say about one that is. Only the first
    controls inclusion — the caller skips a cell whose attrs are empty, so
    anything that is present on nearly every cell (a font name, a size) must
    not participate in that test or every cell in the sheet becomes "styled".
    An earlier draft of this got it wrong and would have reproduced the 45MB
    blowup the head/digest split exists to prevent.
    """
    notable: list[str] = []

    if cell.font and cell.font.bold:
        notable.append('bold="true"')
    if cell.font and cell.font.italic:
        notable.append('italic="true"')

    font_color = _notable_font_color(cell, palette)
    if font_color:
        notable.append(f'font_color="{font_color}"')

    fill = getattr(cell, "fill", None)
    fg = getattr(fill, "fgColor", None) if fill else None
    fill_color = _resolved_color(
        get_fill_color(cell), palette or [], getattr(fg, "tint", None)
    )
    if fill_color:
        notable.append(f'fill_color="{fill_color}"')

    has_border, border_color = _border_info(cell, palette)
    if has_border:
        notable.append('border="true"')
        if border_color:
            notable.append(f'border_color="{border_color}"')

    if cell.number_format and cell.number_format != "General":
        notable.append(f'number_format="{xml_escape(cell.number_format)}"')

    if not notable:
        return ""

    # The cell is already being emitted, so naming its face and size is close
    # to free and answers the criteria this module previously could not: a
    # "title in worksheet 2 is 18pt" check had no size to read anywhere.
    font: list[str] = []
    if cell.font:
        if _has_face(cell, theme_map):
            # Same resolution the sheet summary uses, or the two views
            # disagree about the font one cell renders in. The raw spelling is
            # still reported once per sheet in theme_label_spellings.
            face, _ = _resolved_face(cell, theme_map or {}, theme_faces)
            font.append(f'font_name="{xml_escape(face)}"')
        if cell.font.sz:
            font.append(f'font_size_pt="{cell.font.sz}"')

    return " " + " ".join(font + notable)


# Distinct font faces/sizes listed per sheet. Real workbooks use a handful;
# this only guards against a pathological sheet with per-cell fonts.
_MAX_FONT_ENTRIES = 12


# Excel labels the theme faces "Verdana (Body)" / "Verdana (Headings)" in its
# font dropdown, and those labels reach styles.xml as literal font names even
# though no such font exists. Matches the label so it can be folded into the
# face it denotes.
_THEME_FACE_LABEL_RE = re.compile(r"^(?P<face>.+?)\s*\((?:Body|Headings)\)$", re.I)


def _normalized_face(
    name: str, theme_faces: frozenset[str] = frozenset()
) -> tuple[str, bool]:
    """(face, was_theme_label) for a raw font name from styles.xml.

    Gated on the workbook's own theme, so a literal family that merely ends in
    "(Body)" is left verbatim rather than rewritten to a font it never names.
    """
    stripped = name.strip()
    match = _THEME_FACE_LABEL_RE.match(stripped)
    if not match:
        return stripped, False
    face = match.group("face").strip()
    if face.lower() not in theme_faces:
        return stripped, False
    return face, True


# Face names and theme-label spellings listed in the summary attributes.
_MAX_FACE_NAMES = 12


def _sheet_fonts_xml(
    combos: "Counter[tuple[str, float | None]]",
    theme_labels: set[str] | None = None,
) -> str:
    """Render every distinct font face and size seen in the sheet.

    Exists because per-cell attrs are only emitted for cells that are already
    notable, while "all body text is Verdana 11" concerns cells with no bold,
    fill or border. Counts come from the caller's existing row walk rather than
    a second pass, which would double iteration on a 470k-row sheet.
    """
    if not combos:
        return ""

    entries: list[str] = []
    for (name, size), count in combos.most_common(_MAX_FONT_ENTRIES):
        size_attr = f' size_pt="{size}"' if size is not None else ""
        entries.append(
            f'    <font name="{xml_escape(name)}"{size_attr} cells="{count}" />'
        )

    # Faces and face/size combinations are counted separately, so one face at
    # two sizes does not read as two fonts. The name lists are capped like the
    # entries below, with the counts carrying the true totals.
    faces = sorted({name for name, _ in combos})
    shown_faces = faces[:_MAX_FACE_NAMES]
    faces_listed = (
        f' face_names_listed="{len(shown_faces)} of {len(faces)}"'
        if len(faces) > len(shown_faces)
        else ""
    )
    labels = ""
    if theme_labels:
        ordered_labels = sorted(theme_labels)
        shown_labels = ordered_labels[:_MAX_FACE_NAMES]
        labels = f' theme_label_spellings="{xml_escape(", ".join(shown_labels))}"'
        if len(ordered_labels) > len(shown_labels):
            labels += (
                f' theme_label_spellings_listed="{len(shown_labels)} of'
                f' {len(ordered_labels)}"'
            )
    omitted = len(combos) - len(entries)
    note = (
        f' note="the {len(entries)} most common of {len(combos)} face/size'
        f' combinations; {omitted} rarer one(s) not listed"'
        if omitted > 0
        else ' note="every face and size used by a non-empty cell in this sheet"'
    )
    return (
        f'  <fonts faces="{len(faces)}" '
        f'face_names="{xml_escape(", ".join(shown_faces))}"{faces_listed} '
        f'face_size_combinations="{len(combos)}"{labels}{note}>\n'
        + "\n".join(entries)
        + "\n  </fonts>\n"
    )


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

    # The note describes what was actually written; the judge keys on this
    # attribute rather than on an XML comment, so it must not claim full
    # coverage after dropping combinations.
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
    palette = _theme_palette(file_bytes)
    with zipfile.ZipFile(io.BytesIO(file_bytes), "r") as _zf:
        theme_faces = _theme_faces(_zf)
        theme_map = _theme_face_map(_zf)
    # data_only=False so an uncalculated formula reports its formula rather
    # than None, which is indistinguishable from an empty cell. cell.value has
    # no other consumer here, so styles are unaffected.
    # rich_text=True so a cell whose runs carry different fonts reports all of
    # them. Flattened, such a cell reports only its cell-level font, so a
    # criterion like "all body text is Verdana" reads a mixed cell as compliant.
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=False, rich_text=True)
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
            font_combos: Counter[tuple[str, float | None]] = Counter()
            theme_labels: set[str] = set()

            for row_idx, row in enumerate(ws.iter_rows(min_row=1), start=1):
                styled: list[tuple[int, str]] = []
                for cell in row:
                    # Non-anchor cells of a merged range are MergedCell, not
                    # Cell — but Excel/openpyxl can still store real
                    # border/fill on them (e.g. the outer edges of a bordered
                    # merged header block), so they're included too.
                    if not isinstance(cell, Cell | MergedCell) or cell.column is None:
                        continue
                    # Collected before the notability test: a plain body
                    # cell is skipped below, and its font is exactly what the
                    # "all body text is X" criteria need.
                    runs = _rich_text_runs(cell) if cell.value is not None else []
                    cell_has_face = cell.value is not None and _has_face(
                        cell, theme_map
                    )
                    # Runs are walked even when the CELL names no face: a run
                    # carrying its own renders regardless, and gating the whole
                    # walk on the cell dropped every one of them.
                    if cell_has_face or runs:
                        face, was_label = (
                            _resolved_face(cell, theme_map, theme_faces)
                            if cell_has_face
                            else ("", False)
                        )
                        if was_label:
                            theme_labels.add(
                                str(cell.font.name or "(theme font)").strip()
                            )
                        # Counted per CELL, not per run: the attribute is
                        # cells=, and three same-styled runs are still one cell
                        # to a criterion asking how many carry a face.
                        if not runs:
                            if face:
                                font_combos[(face, cell.font.sz)] += 1
                        else:
                            # Only rich text needs the de-duplicating set, and
                            # it is the rare cell — allocating one per plain
                            # cell cost measurable time on a 320k-cell sheet.
                            cell_combos: set[tuple[str, float | None]] = set()
                            for raw_face, raw_size, raw_slot in runs:
                                # A run inherits whichever of the two it omits,
                                # and its own name resolves exactly as a
                                # cell-level one does — theme slot first, then
                                # label folding.
                                run_face = face
                                if raw_face or raw_slot:
                                    run_face, run_was_label = _face_from(
                                        raw_face, raw_slot, theme_map, theme_faces
                                    )
                                    if run_was_label and raw_face:
                                        theme_labels.add(raw_face)
                                if not run_face:
                                    # Neither the run nor the cell names a face.
                                    continue
                                cell_combos.add((run_face, raw_size or cell.font.sz))
                            # Sorted, not set order: a set of (face, size)
                            # tuples iterates in hash order, which Python
                            # randomises per process, so the <font> lines came
                            # out in a different order run to run. Harmless to
                            # a judge reading them, but it makes the output
                            # non-reproducible and any byte-comparison of it
                            # unsound. None sorts before a real size.
                            for combo in sorted(
                                cell_combos,
                                key=lambda c: (c[0], c[1] is None, c[1] or 0.0),
                            ):
                                font_combos[combo] += 1

                    style_attrs = _cell_style_attrs(
                        cell, palette, theme_faces, theme_map
                    )
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
                # A sheet of plain text has no notable cell and so produced
                # nothing at all, which is exactly the case "all body text is
                # Verdana 11" asks about. Its <fonts> element is small and
                # bounded, so keep the sheet for that alone.
                and not font_combos
            ):
                continue

            digest_xml = _style_digest_xml(signatures, row_span, listed_rows)

            escaped_name = xml_escape(ws.title)
            body = (
                sheet_props_xml
                + _sheet_fonts_xml(font_combos, theme_labels)
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
        # A legacy .xls is OLE2, which openpyxl surfaces as BadZipFile from
        # the zip layer; InvalidFileException is its own unsupported-format
        # error, caught alongside so the fallback works either way.
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
            # Deterministic for this file, so it is reported rather than
            # raised: the cache never stores failures, and re-raising would
            # make every later verifier pay another 120s LibreOffice timeout.
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
