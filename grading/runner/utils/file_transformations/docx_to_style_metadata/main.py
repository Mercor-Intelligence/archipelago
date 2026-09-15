"""Extract paragraph, font, numbering, and tracked-change metadata from a DOCX.

The dict-returning extractor is a domain-specific utility used by the
docx_style_verifier_apex_v2 eval. docx_to_style_metadata_output wraps it as a
registered transformation for the generic multi-representation judge, emitting
a bounded run/table/chart style summary rather than the full dict.

The output is consumed by an LLM judge in `text` mode. Tracked changes are
preserved (not stripped) so style criteria can grade redline hygiene.

See ../README.md for the rules these extractors share: resolve references to
the value that renders, name the source, never invent one, and cap every
section with the cap disclosed.
"""

import asyncio
import io
import re
import zipfile
from typing import Any, NamedTuple

import lxml.etree as etree
from docx import Document
from docx.oxml.ns import qn
from loguru import logger
from pypdf import PdfReader

from ...file_extraction.utils.chart_extraction import find_libreoffice
from ..docx_to_pdf.main import docx_to_pdf
from ..models import TransformationOutput
from ..style_metadata_cache import cached_style_text, is_ooxml_package
from ..xml_utils import (
    DML_NS,
    OFFICE_RELS_NS,
    PKG_RELS_NS,
    WML_NS,
    bare,
    nearest_ancestor,
    xml_escape,
)

# lxml exposes its element type privately, so every annotation naming it
# needed its own reportPrivateUsage suppression. One alias covers them all.
type Element = etree._Element  # pyright: ignore[reportPrivateUsage]

_WNS = bare(WML_NS)
_SAFE_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)


def _on_off(el: Element) -> bool:
    """Whether a WordprocessingML on/off element is on.

    ST_OnOff admits 1/0/true/false/on/off, so this allow-lists the three truthy
    tokens rather than deny-listing the falsy ones: a deny-list of ("0","false")
    read w:val="off" as ON. Writers other than Word are the ones that serialize
    the attribute explicitly, and "off" is a token they emit, so the inversion
    landed exactly where it was least likely to be noticed. python-docx's own
    ST_OnOff.convert_from_xml uses the same truthy set. Case is normalised so a
    mis-cased "Off" cannot flip meaning either.

    Callers handle absence themselves, since for some properties absent means
    "inherit" rather than "off".
    """
    return str(el.get(qn("w:val"), "1")).strip().lower() in ("1", "true", "on")


def _half_pt_to_pt(half_pt: str | None) -> float | None:
    """Word stores font size in half-points (string). 22 -> 11.0pt."""
    if half_pt is None:
        return None
    try:
        return round(int(half_pt) / 2, 1)
    except (TypeError, ValueError):
        return None


def _twips_to_pt(twips: str | int | None) -> float | None:
    """Word uses twips (1/20 of a point) for spacing/indents."""
    if twips is None:
        return None
    try:
        return round(int(twips) / 20, 1)
    except (TypeError, ValueError):
        return None


def _extract_run_style(r: Element) -> dict[str, Any]:
    """Extract style + text from a single w:r element.

    Returns a dict with the inline text (possibly empty) and any style hints
    available on the run.
    """
    text_parts: list[str] = []
    for t in r:
        tag_local = etree.QName(t).localname
        if tag_local in ("t", "delText"):
            if t.text:
                text_parts.append(t.text)
        elif tag_local == "tab":
            text_parts.append("\t")
        elif tag_local == "br":
            text_parts.append("\n")
    text = "".join(text_parts)

    rpr = r.find(qn("w:rPr"))
    font_name: str | None = None
    font_size_pt: float | None = None
    bold: bool | None = None
    italic: bool | None = None
    underline: str | None = None
    color_hex: str | None = None
    color_auto = False
    run_shading: str | None = None
    # Stating "no shading" is not the same as saying nothing. Like any direct
    # formatting it overrides the character style's fill, so the style must not
    # be consulted afterwards.
    run_shading_cleared = False
    run_style: str | None = None
    if rpr is not None:
        rstyle = rpr.find(qn("w:rStyle"))
        if rstyle is not None:
            run_style = rstyle.get(qn("w:val"))
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is not None:
            font_name = rfonts.get(qn("w:ascii")) or rfonts.get(qn("w:hAnsi"))
        sz = rpr.find(qn("w:sz"))
        if sz is not None:
            font_size_pt = _half_pt_to_pt(sz.get(qn("w:val")))
        b = rpr.find(qn("w:b"))
        if b is not None:
            bold = _on_off(b)
        i = rpr.find(qn("w:i"))
        if i is not None:
            italic = _on_off(i)
        u = rpr.find(qn("w:u"))
        if u is not None:
            underline = u.get(qn("w:val"))
        shd = rpr.find(qn("w:shd"))
        if shd is not None:
            run_shading, run_shading_cleared = _shading_state(shd)
            # Named for what the caller does with it: skip the character style.
        color = rpr.find(qn("w:color"))
        if color is not None:
            val = color.get(qn("w:val"))
            # "auto" is not "unset": Word renders it black on a light page,
            # whereas an absent w:color inherits from the style. Collapsing
            # both to None left a criterion about black text unanswerable.
            if val == "auto":
                color_auto = True
            elif val:
                color_hex = f"#{val}" if not val.startswith("#") else val

    return {
        "text": text,
        "font_name": font_name,
        "font_size_pt": font_size_pt,
        "bold": bold,
        "italic": italic,
        "underline": underline,
        "color_hex": color_hex,
        "color_auto": color_auto,
        # Shading on the run itself sits closest to the text, so it beats the
        # paragraph's when automatic colour is resolved.
        "run_shading": run_shading,
        # The character style is a reference to a background, resolved against
        # the same shaded style ids a paragraph or table style is.
        "run_style": run_style,
        "run_shading_cleared": run_shading_cleared,
    }


# A background we can see exists but cannot resolve to a hex. Automatic text
# is left unresolved over it rather than guessed at.
_UNKNOWN_SHADING = "?"


def _shading_state(shd: "Element") -> tuple[str | None, bool]:
    """What a w:shd paints: (fill or _UNKNOWN_SHADING, overrides_its_style).

    ST_Shd is not a colour, it is a PATTERN plus two colours. Only ``clear``
    means "no pattern, just w:fill"; ``nil`` means nothing at all; every other
    value (pct25, horzStripe, diagCross, ...) paints w:color over w:fill, and
    the blend of the two is not something this resolves. So "no usable w:fill"
    does not mean "nothing is painted" -- reading it that way reported a 25%
    pattern of navy ink as an empty background.

    The second value says whether this w:shd overrides the fill its style would
    otherwise supply. Anything that paints does. ``nil`` does, because it is an
    explicit "no shading" -- an authoring act, not a default.

    ``clear`` with no usable fill deliberately does NOT. It is the default
    state written out longhand, which is exactly the shape a converter emits as
    boilerplate, and treating boilerplate as an override would suppress a real
    dark table style and report readable black text on a navy cell -- the
    false positive this module exists to prevent. No file to hand settles
    whether converters emit it, so the ambiguous spelling stays unresolved,
    which is what main already does. The unambiguous one is honoured.
    """
    val = (shd.get(qn("w:val")) or "clear").lower()
    if val == "nil":
        return None, True
    if val != "clear":
        # A pattern paints something; the effective colour is unresolvable.
        return _UNKNOWN_SHADING, True
    if shd.get(qn("w:themeFill")):
        return _UNKNOWN_SHADING, True
    fill = shd.get(qn("w:fill"))
    if fill and fill.lower() not in ("auto", "none"):
        return fill, True
    return None, False


def _enclosing_table_style(p: "Element") -> str | None:
    """The style id of the table this paragraph sits in, if any.

    A table style is a reference, so it is resolved against the same shaded
    style ids a paragraph style is: the fill lives in the style, not on the
    cell.
    """
    node = nearest_ancestor(p, "w:tbl")
    if node is None:
        return None
    tbl_pr = node.find(qn("w:tblPr"))
    style = tbl_pr.find(qn("w:tblStyle")) if tbl_pr is not None else None
    val = style.get(qn("w:val")) if style is not None else None
    return str(val) if val else None


def _effective_shading(
    p: "Element",
    ppr: "Element | None",
) -> tuple[list[tuple[str, str]], tuple[str, ...]]:
    """The fills painted behind a paragraph, and which holders declared one.

    All of them, not the nearest one. Automatic text is resolved only when
    every background in play is known light, so which one Word would pick
    never has to be decided: if they agree the answer is the same either way,
    and if they disagree the honest answer is "unresolved" either way. That
    removes the precedence question rather than answering it.

    The holder is reported because one comparison is NOT a precedence question:
    a style's fill and a direct fill on the SAME element are one property
    written twice, and direct formatting simply replaces the style's, which is
    never painted (ECMA-376 §17.7.2). Knowing a holder set its own fill is what
    lets the caller drop that holder's style rather than weigh the two.

    Which is why the second return value exists. A holder can declare a
    background and declare it EMPTY -- w:shd val="nil", or fill="auto" -- and
    that still overrides its style, because it is direct formatting like any
    other. Deriving "this holder declared something" from the fill list alone
    could not see those: they produce no fill, so a paragraph that explicitly
    clears a dark style's shading looked identical to one that said nothing,
    and automatic text came back unresolved over a page Word renders white.
    """
    holders: list[tuple[str, Any]] = [] if ppr is None else [("paragraph", ppr)]
    cell = nearest_ancestor(p, "w:tc")
    if cell is not None:
        tc_pr = cell.find(qn("w:tcPr"))
        if tc_pr is not None:
            holders.append(("cell", tc_pr))
        tbl = nearest_ancestor(cell, "w:tbl")
        tbl_pr = tbl.find(qn("w:tblPr")) if tbl is not None else None
        if tbl_pr is not None:
            holders.append(("table", tbl_pr))

    fills: list[tuple[str, str]] = []
    declared: set[str] = set()
    for kind, holder in holders:
        shd = holder.find(qn("w:shd"))
        if shd is None:
            continue
        fill, overrides = _shading_state(shd)
        if overrides:
            declared.add(kind)
        if fill:
            fills.append((kind, fill))
    # Sorted tuple, not a set: every other value in the paragraph dict is a
    # JSON-safe primitive, and a set both breaks json.dumps and stringifies in
    # hash order -- the same non-determinism the <font> ordering had.
    return fills, tuple(sorted(declared))


def _is_known_light(hex_rgb: str) -> bool:
    """Whether a background is READABLE and light enough that Word renders
    automatic text black. Rec. 601 luma, midpoint threshold.

    Not the negation of "dark": a fill this cannot parse is neither, and
    answering "light" for it resolved automatic text to black over a background
    that was never read — inventing the value this module exists to avoid.
    """
    value = hex_rgb.lstrip("#")
    if len(value) != 6:
        return False
    try:
        r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return False
    return (0.299 * r + 0.587 * g + 0.114 * b) >= 128


def _page_background_is_dark(
    zf: zipfile.ZipFile,
    doc_root: "Element",
) -> bool:
    """Whether the document paints a dark page background Word would honour.

    A w:background is inert unless settings.xml opts in via
    displayBackgroundShape, so an undisplayed one leaves the page white.
    """
    bg = doc_root.find(qn("w:background"))
    if bg is None:
        return False
    color = bg.get(qn("w:color")) or bg.get("color")
    themed = bg.get(qn("w:themeColor"))
    # A themed page colour is not resolved here, so it cannot be assumed light.
    if not themed and (
        not color or color.lower() in ("auto", "none") or _is_known_light(color)
    ):
        return False
    if "word/settings.xml" not in zf.namelist():
        return False
    try:
        st = etree.fromstring(zf.read("word/settings.xml"), parser=_SAFE_PARSER)
    except etree.XMLSyntaxError:
        return False
    dbs = st.find(qn("w:displayBackgroundShape"))
    return dbs is not None and _on_off(dbs)


# Elements that wrap runs without being runs. Word nests visible text inside
# any of these, and each one that is not descended into silently removes its
# text from the style summary. Enumerated rather than "recurse into anything
# unknown" so the tracked-change wrappers keep their own handling below and a
# non-content element cannot smuggle runs in.
_TRANSPARENT_RUN_CONTAINERS = frozenset(
    {
        "hyperlink",
        "fldSimple",
        "sdt",
        "sdtContent",
        "smartTag",
        "customXml",
        "dir",
        "bdo",
    }
)


def _extract_paragraph(p: Element, comments_by_id: dict[str, str]) -> dict[str, Any]:
    """Extract paragraph-level style + runs + tracked-change segments."""
    ppr = p.find(qn("w:pPr"))
    style_name: str | None = None
    alignment: str | None = None
    indent_left_pt: float | None = None
    indent_first_line_pt: float | None = None
    numbering_id: str | None = None
    numbering_level: str | None = None
    if ppr is not None:
        ps = ppr.find(qn("w:pStyle"))
        if ps is not None:
            style_name = ps.get(qn("w:val"))
        jc = ppr.find(qn("w:jc"))
        if jc is not None:
            alignment = jc.get(qn("w:val"))
        ind = ppr.find(qn("w:ind"))
        if ind is not None:
            indent_left_pt = _twips_to_pt(
                ind.get(qn("w:left")) or ind.get(qn("w:start"))
            )
            indent_first_line_pt = _twips_to_pt(ind.get(qn("w:firstLine")))
        numpr = ppr.find(qn("w:numPr"))
        if numpr is not None:
            numid_el = numpr.find(qn("w:numId"))
            ilvl_el = numpr.find(qn("w:ilvl"))
            if numid_el is not None:
                numbering_id = numid_el.get(qn("w:val"))
            if ilvl_el is not None:
                numbering_level = ilvl_el.get(qn("w:val"))

    # Walk children in order; capture runs + tracked-change segments.
    segments: list[dict[str, Any]] = []
    comment_refs: list[str] = []

    def walk(children: Any) -> None:
        for child in children:
            tag_local = etree.QName(child).localname
            if tag_local in _TRANSPARENT_RUN_CONTAINERS:
                # Transparent wrappers: their runs are ordinary runs.
                # Recursed rather than swept with iter(w:r) so a tracked
                # change inside one keeps its own classification instead of
                # becoming live text.
                walk(child)
                continue
            _segment(child, tag_local)

    def _segment(child: Any, tag_local: str) -> None:
        if tag_local == "r":
            run = _extract_run_style(child)
            if run["text"] or any(
                run[k] is not None for k in ("font_name", "font_size_pt", "bold")
            ):
                segments.append({"kind": "run", **run})
        elif tag_local in ("ins", "del", "moveFrom", "moveTo"):
            label = {
                "ins": "INSERTED",
                "del": "DELETED",
                "moveFrom": "MOVED_FROM",
                "moveTo": "MOVED_TO",
            }[tag_local]
            inner_runs: list[dict[str, Any]] = []
            for r in child.iter(qn("w:r")):
                run = _extract_run_style(r)
                inner_runs.append(run)
            text = "".join(rr["text"] for rr in inner_runs)
            segments.append(
                {
                    "kind": "tracked_change",
                    "type": label,
                    "author": child.get(qn("w:author")),
                    "date": child.get(qn("w:date")),
                    "text": text,
                    "runs": inner_runs,
                }
            )
        elif tag_local == "commentRangeStart":
            cid = child.get(qn("w:id"))
            if cid and cid in comments_by_id:
                comment_refs.append(cid)

    walk(p)

    plain_text = "".join(seg.get("text", "") for seg in segments if seg.get("text"))

    shading_fills, shading_declared_by = _effective_shading(p, ppr)

    return {
        "style_name": style_name,
        "alignment": alignment,
        # Everything painted behind this paragraph, so an automatic font
        # colour is resolved only when all of it is known light.
        "shading_fills": shading_fills,
        "shading_declared_by": shading_declared_by,
        "table_style": _enclosing_table_style(p),
        "indent_left_pt": indent_left_pt,
        "indent_first_line_pt": indent_first_line_pt,
        "numbering_id": numbering_id,
        "numbering_level": numbering_level,
        "plain_text": plain_text,
        "segments": segments,
        "comment_ids": comment_refs,
    }


def _load_comments(docx_zip: zipfile.ZipFile) -> dict[str, str]:
    """Load comments.xml if present. Returns {comment_id: comment_text}."""
    if "word/comments.xml" not in docx_zip.namelist():
        return {}
    try:
        tree = etree.fromstring(docx_zip.read("word/comments.xml"), parser=_SAFE_PARSER)
    except etree.XMLSyntaxError:
        return {}
    out: dict[str, str] = {}
    for c in tree.iter(qn("w:comment")):
        cid = c.get(qn("w:id"))
        if cid is None:
            continue
        text = "".join(t.text or "" for t in c.iter(qn("w:t")))
        out[cid] = text
    return out


def _approximate_page_index(
    paragraph_idx: int, total_paragraphs: int, page_count: int
) -> int:
    """Map a paragraph index to a 1-based page bucket.

    DOCX is a flowing format; "pages" only emerge after rendering. We bucket
    paragraphs evenly so page-scoped style criteria have a defined slice to
    grade. This is a coarse approximation — for criteria that need true
    per-page rendering, the image-mode judge sees actual PDF pages.
    """
    if page_count <= 1 or total_paragraphs == 0:
        return 1
    per_page = max(1, (total_paragraphs + page_count - 1) // page_count)
    return min(page_count, (paragraph_idx // per_page) + 1)


def docx_to_style_metadata(
    file_bytes: bytes,
    file_name: str,
    page_count: int | None = None,
    *,
    reuse_doc: Any = None,
    reuse_zip: zipfile.ZipFile | None = None,
) -> dict[str, Any]:
    """Extract style metadata from a DOCX file.

    Args:
        file_bytes: Raw .docx bytes.
        file_name: Filename, included in the output for context.
        page_count: Optional number of pages (from the image renderer). When
            provided, paragraphs are bucketed evenly across pages so a
            page-scoped judge can be given the relevant slice. If None, the
            whole document is treated as a single page.

    Returns:
        {
            "file_name": str,
            "page_count": int,
            "paragraph_count": int,
            "comments": {comment_id: text},
            "tracked_change_summary": {
                "insertions": int,
                "deletions": int,
                "move_from": int,
                "move_to": int,
            },
            "paragraphs": [
                {
                    "index": int,
                    "page": int,
                    "style_name": str | None,
                    "alignment": str | None,
                    "indent_left_pt": float | None,
                    "indent_first_line_pt": float | None,
                    "numbering_id": str | None,
                    "numbering_level": str | None,
                    "plain_text": str,
                    "segments": [...],
                    "comment_ids": [...],
                },
                ...
            ],
            "pages": [
                {"page": int, "paragraph_indices": [int, ...]},
                ...
            ],
        }
    """
    # reuse_doc/reuse_zip let a caller that has already parsed this document
    # hand them over instead of paying for a second parse. Both default to None
    # so every existing call site is unaffected; _docx_style_summary_xml is the
    # one caller that passes them, because it needs the same document and ZIP
    # for the chart, background and style-inheritance lookups it does itself.
    if reuse_zip is not None:
        comments = _load_comments(reuse_zip)
    else:
        with zipfile.ZipFile(io.BytesIO(file_bytes), "r") as zf:
            comments = _load_comments(zf)

    # python-docx for top-level access (sections, body) is convenient
    doc = reuse_doc if reuse_doc is not None else Document(io.BytesIO(file_bytes))
    body = doc.element.body  # pyright: ignore[reportAttributeAccessIssue]
    p_tag = qn("w:p")

    paragraphs_raw = [p for p in body.iter(p_tag)]
    total_paragraphs = len(paragraphs_raw)
    effective_page_count = page_count if (page_count and page_count > 0) else 1

    paragraphs_out: list[dict[str, Any]] = []
    counts = {"insertions": 0, "deletions": 0, "move_from": 0, "move_to": 0}
    for idx, p in enumerate(paragraphs_raw):
        meta = _extract_paragraph(p, comments)
        page = _approximate_page_index(idx, total_paragraphs, effective_page_count)
        for seg in meta["segments"]:
            if seg.get("kind") != "tracked_change":
                continue
            t = seg.get("type")
            if t == "INSERTED":
                counts["insertions"] += 1
            elif t == "DELETED":
                counts["deletions"] += 1
            elif t == "MOVED_FROM":
                counts["move_from"] += 1
            elif t == "MOVED_TO":
                counts["move_to"] += 1
        paragraphs_out.append({"index": idx, "page": page, **meta})

    pages_map: dict[int, list[int]] = {}
    for para in paragraphs_out:
        pages_map.setdefault(para["page"], []).append(para["index"])
    pages_out = [
        {"page": page, "paragraph_indices": pages_map[page]}
        for page in sorted(pages_map.keys())
    ]

    return {
        "file_name": file_name,
        "page_count": effective_page_count,
        "paragraph_count": total_paragraphs,
        "comments": comments,
        "tracked_change_summary": counts,
        "paragraphs": paragraphs_out,
        "pages": pages_out,
    }


# --- Registered transformation for the generic multi-representation judge ---
#
# The dict above is shaped for the docx_style_verifier_apex_v2 eval. What
# follows condenses the same document into a bounded XML block for the generic
# judge, plus two things the dict does not carry: embedded chart fills and the
# page background.
#
# Motivating case (DEP-846): a criterion asked that "all embedded tables and
# charts" have a transparent or #F4EBE8 background. The document had zero
# tables and two charts, both explicitly <a:noFill/> — i.e. compliant. But a
# transparent chart sitting on a tinted page renders as a tinted box, so the
# screenshot-reading judge failed it. The fill is only knowable from the XML.

_CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
# word/charts/ holds chartN.xml and chartExN.xml alongside companion
# colors1.xml / style1.xml parts; only the chart bodies are wanted.
_CHART_PART_RE = re.compile(r"word/charts/chart(?:Ex)?\d+\.xml")
# Theme part holding the major/minor font definitions styles.xml points at.
_THEME_PART_RE = re.compile(r"word/theme/theme\d+\.xml")

# Bound the emitted style groups; documents repeat a handful of combinations.
_MAX_RUN_STYLE_GROUPS = 20
_MAX_TABLE_ENTRIES = 12
# Cell fills listed per table; a sample, disclosed as one when truncated.
_MAX_CELL_FILLS = 8
_MAX_CHART_ENTRIES = 12


def _fill_of(sp_pr: "Element | None") -> str | None:
    """Describe a DrawingML <spPr> fill as 'none', '#RRGGBB', or a scheme name.

    Returns None when the element is absent, which in OOXML means "inherit"
    rather than "transparent" — the distinction matters for a criterion that
    accepts transparency, so the two are never collapsed.
    """
    if sp_pr is None:
        return None
    if sp_pr.find(f"{DML_NS}noFill") is not None:
        return "none"
    solid = sp_pr.find(f"{DML_NS}solidFill")
    if solid is None:
        return None
    srgb = solid.find(f"{DML_NS}srgbClr")
    if srgb is not None and srgb.get("val"):
        return f"#{str(srgb.get('val')).upper()}"
    scheme = solid.find(f"{DML_NS}schemeClr")
    if scheme is not None and scheme.get("val"):
        return f"scheme:{scheme.get('val')}"
    return "solid"


def _natural_key(name: str) -> str:
    """Sort key that orders chart10 after chart2.

    OOXML numbers parts sequentially, so the digits carry the ordering. Plain
    lexicographic sorting interleaves them (chart1, chart10, chart11, ...,
    chart2), which would both mislabel the emitted index and, once the entry
    cap truncates the list, keep the wrong charts. Zero-padding the digit runs
    in place keeps the key a plain string, so there is no int/str comparison to
    get wrong.
    """
    return re.sub(r"\d+", lambda m: m.group().zfill(12), name)


def _styles_root(zf: zipfile.ZipFile) -> "Element | None":
    """Parse word/styles.xml once, for the resolvers that both need it.

    _bold_style_ids and _style_run_fonts are called back to back and each used
    to read and parse the same part, so every docx paid two parses of it.
    Returns None when the part is absent or malformed, which both callers
    already treat as "nothing to resolve".
    """
    if "word/styles.xml" not in zf.namelist():
        return None
    try:
        return etree.fromstring(zf.read("word/styles.xml"), parser=_SAFE_PARSER)
    except etree.XMLSyntaxError:
        return None


def _bold_style_ids(
    zf: zipfile.ZipFile,
    root: "Element | None" = None,
) -> frozenset[str]:
    """Style ids whose definition turns bold on, following w:basedOn.

    A run with no w:b inherits from its paragraph style, so reporting such a
    run as not-bold is wrong: Heading1 in the motivating document sets bold at
    the style level and nowhere on the run. The criteria in play are precisely
    about which text is bold, so the distinction has to be resolved rather
    than flattened.
    """
    root = root if root is not None else _styles_root(zf)
    if root is None:
        return frozenset()

    # Tri-state per style: True = w:b turns bold on, False = w:b val="0"
    # turns it *off*, absent = says nothing and defers to w:basedOn. Collapsing
    # the last two into False made an explicit "off" keep walking up the chain,
    # so a style disabling bold while based on a bold parent was reported bold.
    own_bold: dict[str, bool | None] = {}
    based_on: dict[str, str] = {}
    for style in root.iter(qn("w:style")):
        sid = style.get(qn("w:styleId"))
        if not sid:
            continue
        rpr = style.find(qn("w:rPr"))
        b = rpr.find(qn("w:b")) if rpr is not None else None
        own_bold[sid] = None if b is None else _on_off(b)
        parent = style.find(qn("w:basedOn"))
        if parent is not None and parent.get(qn("w:val")):
            based_on[sid] = str(parent.get(qn("w:val")))

    def resolve(sid: str) -> bool:
        # Bounded walk: a malformed basedOn chain can be cyclic. The nearest
        # style that states a value wins, so an explicit off stops the walk
        # instead of letting an ancestor's bold leak through.
        seen: set[str] = set()
        cur = sid
        while cur and cur not in seen:
            seen.add(cur)
            stated = own_bold.get(cur)
            if stated is not None:
                return stated
            cur = based_on.get(cur, "")
        return False

    return frozenset(sid for sid in own_bold if resolve(sid))


def _theme_part_name(zf: zipfile.ZipFile) -> str | None:
    """The theme part the document references, else the lowest-numbered one.

    Editing a document can leave an orphaned theme1.xml behind while the
    document points at theme2.xml; the relationship is the authority.
    """
    parts = sorted(
        (n for n in zf.namelist() if _THEME_PART_RE.fullmatch(n)), key=_natural_key
    )
    if not parts:
        return None
    try:
        rels = etree.fromstring(
            zf.read("word/_rels/document.xml.rels"), parser=_SAFE_PARSER
        )
    except (KeyError, etree.XMLSyntaxError):
        return parts[0]
    for rel in rels.iter(f"{PKG_RELS_NS}Relationship"):
        if not (rel.get("Type") or "").endswith("/theme"):
            continue
        name = _rel_part_name(rel.get("Target") or "")
        if name in parts:
            return name
    return parts[0]


def _theme_fonts(zf: zipfile.ZipFile) -> dict[str, str]:
    """The major/minor latin typefaces the theme defines.

    Word does not write literal font names into styles.xml by default. It
    writes theme references — `w:asciiTheme="minorHAnsi"` — and the actual name
    lives in word/theme/theme1.xml as `<a:minorFont><a:latin typeface="Aptos"/>`.
    Reading only w:ascii therefore resolves nothing on a stock document, which
    is how "only fonts from the Aptos family" failed against a document whose
    theme is Aptos / Aptos Display.
    """
    part = _theme_part_name(zf)
    if part is None:
        return {}
    try:
        root = etree.fromstring(zf.read(part), parser=_SAFE_PARSER)
    except etree.XMLSyntaxError:
        return {}
    out: dict[str, str] = {}
    for slot in ("major", "minor"):
        el = root.find(f".//{DML_NS}{slot}Font/{DML_NS}latin")
        face = el.get("typeface") if el is not None else None
        if face:
            out[slot] = face
    return out


def _style_run_fonts(
    zf: zipfile.ZipFile,
    root: "Element | None" = None,
) -> dict[str, tuple[str | None, float | None]]:
    """Per style id, the font name and size it resolves to, following w:basedOn.

    A run with no w:rFonts or w:sz inherits both from its paragraph style, and
    that is how Word writes documents by default — the motivating document sets
    neither on a single run. The emitter used to omit the attribute entirely in
    that case, and a judge told to treat this block as ground truth read the
    missing attribute as a non-compliant one: "Formats all text using only
    fonts from the Aptos family" failed against a document whose styles do set
    Aptos, purely because no font_name was emitted. That is the same
    absence-as-evidence failure this module exists to remove, so the value has
    to be resolved rather than dropped.

    w:docDefaults is the base of the chain: a style stating nothing inherits
    the document default, which is where Word puts the theme font.
    """
    root = root if root is not None else _styles_root(zf)
    if root is None:
        return {}

    theme = _theme_fonts(zf)

    def _own(rpr: "Element | None") -> tuple[str | None, float | None]:
        if rpr is None:
            return None, None
        fonts = rpr.find(qn("w:rFonts"))
        name = None
        if fonts is not None:
            # Theme first, then the literal. ECMA-376 17.3.2.26 has the theme
            # attribute win when both are present, and Word ships styles
            # carrying both — w:ascii="Calibri" beside
            # w:asciiTheme="minorHAnsi" on a document whose theme is Aptos.
            # Preferring the literal there reports a stale face and fails the
            # very "only Aptos family" criterion this resolution exists to fix.
            # The token names the slot: "major" is the heading face,
            # "minor" the body face.
            token = fonts.get(qn("w:asciiTheme")) or fonts.get(qn("w:hAnsiTheme"))
            if token:
                slot = "major" if str(token).startswith("major") else "minor"
                name = theme.get(slot)
            if not name:
                name = fonts.get(qn("w:ascii")) or fonts.get(qn("w:hAnsi"))
        sz = rpr.find(qn("w:sz"))
        return name, _half_pt_to_pt(sz.get(qn("w:val")) if sz is not None else None)

    # Document defaults sit under w:docDefaults/w:rPrDefault/w:rPr.
    default_font: str | None = None
    default_size: float | None = None
    doc_defaults = root.find(qn("w:docDefaults"))
    if doc_defaults is not None:
        rpr_default = doc_defaults.find(qn("w:rPrDefault"))
        if rpr_default is not None:
            default_font, default_size = _own(rpr_default.find(qn("w:rPr")))

    own: dict[str, tuple[str | None, float | None]] = {}
    based_on: dict[str, str] = {}
    for style in root.iter(qn("w:style")):
        sid = style.get(qn("w:styleId"))
        if not sid:
            continue
        own[sid] = _own(style.find(qn("w:rPr")))
        parent = style.find(qn("w:basedOn"))
        if parent is not None and parent.get(qn("w:val")):
            based_on[sid] = str(parent.get(qn("w:val")))

    def resolve(sid: str) -> tuple[str | None, float | None]:
        # Font and size resolve independently: a style may state a size while
        # inheriting its font, so the walk cannot stop at the first style that
        # states either one. Bounded like the bold walk, since a malformed
        # basedOn chain can be cyclic.
        font: str | None = None
        size: float | None = None
        seen: set[str] = set()
        cur = sid
        while cur and cur not in seen:
            seen.add(cur)
            f, z = own.get(cur, (None, None))
            if font is None:
                font = f
            if size is None:
                size = z
            if font is not None and size is not None:
                break
            cur = based_on.get(cur, "")
        return (
            font if font is not None else default_font,
            size if size is not None else default_size,
        )

    return {sid: resolve(sid) for sid in own}


def _charts_xml(zf: zipfile.ZipFile) -> str:
    """Emit the chart-space and plot-area fill of every embedded chart.

    Matches chart parts exactly rather than by prefix: word/charts/ also holds
    companion colors1.xml / style1.xml parts, and a bare "chart" prefix would
    additionally sweep in chartEx parts. Those are matched deliberately —
    modern chart types (waterfall, treemap, funnel) are stored as chartEx and
    live in their own namespace, so dropping them would understate the count a
    criterion about "all embedded charts" is graded against.
    """
    names = sorted(
        (n for n in zf.namelist() if _CHART_PART_RE.fullmatch(n)),
        key=_natural_key,
    )
    if not names:
        return '  <charts count="0" />\n'

    rows: list[str] = []
    for i, name in enumerate(names[:_MAX_CHART_ENTRIES], start=1):
        try:
            root = etree.fromstring(zf.read(name), parser=_SAFE_PARSER)
        except Exception:
            # Emit a placeholder rather than skipping, so `index` stays dense:
            # a silent gap reads as a numbering error, and a criterion counting
            # charts would be graded against the wrong total.
            #
            # `index` is position in the natural-sorted part-name order, NOT
            # document order. OPC part names reflect creation order, so an
            # edited document can name charts in an order that does not match
            # where they appear on the page, and orphan parts an editing tool
            # left behind still count. For agent-authored documents the two
            # normally coincide, which is why this is left as is — but a
            # criterion about "the third chart" is only reliable to the extent
            # they do. Resolving through document.xml.rels would give true
            # document order if that ever stops holding.
            rows.append(f'    <chart index="{i}" unreadable="true" />')
            continue
        # chartEx uses its own namespace for the same element names, so take
        # the namespace from the root rather than assuming the classic one.
        ns = etree.QName(root).namespace or _CHART_NS
        space_fill = _fill_of(root.find(f"{{{ns}}}spPr"))
        plot = root.find(f".//{{{ns}}}plotArea")
        plot_fill = _fill_of(plot.find(f"{{{ns}}}spPr") if plot is not None else None)
        attrs = f'index="{i}"'
        if ns != _CHART_NS:
            attrs += ' kind="extended"'
        if space_fill is not None:
            attrs += f' chart_background="{xml_escape(space_fill)}"'
        if plot_fill is not None:
            attrs += f' plot_area_background="{xml_escape(plot_fill)}"'
        rows.append(f"    <chart {attrs} />")

    # Only the cap needs a note now — unreadable parts are marked in place
    # above, so the two can no longer be conflated into one "omitted" count.
    over_cap = max(0, len(names) - _MAX_CHART_ENTRIES)
    notes: list[str] = []
    if over_cap:
        notes.append(f"    <!-- {over_cap} more charts omitted -->")
    body = "\n".join([*rows, *notes])
    return f'  <charts count="{len(names)}">\n{body}\n  </charts>\n'


def _shd_fill(el: "Element | None") -> str | None:
    """The fill of a w:shd child of `el`, normalised, or None."""
    if el is None:
        return None
    shd = el.find(qn("w:shd"))
    if shd is None:
        return None
    fill = shd.get(qn("w:fill"))
    if not fill:
        return None
    return "none" if fill.lower() in ("auto", "none") else f"#{fill.upper()}"


def _owned_by(el: "Element", tbl: "Element") -> bool:
    """Whether `el`'s nearest enclosing table is `tbl`.

    Descendant search with an ownership test, rather than direct children:
    findall() misses rows wrapped in w:sdt (a content control, common in Word
    templates) and would silently under-report their shading, while a plain
    iter() hands a nested table's fills to its parent as well. Walking up to the
    closest w:tbl gets both right.
    """
    parent = el.getparent()
    while parent is not None:
        if parent.tag == qn("w:tbl"):
            return parent is tbl
        parent = parent.getparent()
    return False


def _tables_xml(body: "Element") -> str:
    """Emit table count, each table's cell shading, and its table-wide shading.

    Shading is read from specific parents rather than by walking descendants.
    w:shd is valid on w:rPr and w:pPr as well as w:tcPr and w:tblPr, so an
    iter() over the table reported highlighted *text* inside a cell as
    cell_backgrounds — asserting a cell fill that does not exist, which is worse
    than omitting one. Table-wide shading is reported separately rather than
    folded in, since "the table has a background" and "its cells do" are
    different claims and a criterion may ask either.

    Rows and cells are matched by ownership rather than by depth — see
    _owned_by — so a nested table keeps its own fills while a row wrapped in a
    content control is still found.
    """
    tables = list(body.iter(qn("w:tbl")))
    if not tables:
        return '  <tables count="0" />\n'

    rows_out: list[str] = []
    for i, tbl in enumerate(tables[:_MAX_TABLE_ENTRIES], start=1):
        table_rows = [r for r in tbl.iter(qn("w:tr")) if _owned_by(r, tbl)]
        fills: list[str] = []
        for cell in tbl.iter(qn("w:tc")):
            if not _owned_by(cell, tbl):
                continue
            fill = _shd_fill(cell.find(qn("w:tcPr")))
            if fill and fill not in fills:
                fills.append(fill)

        attrs = f'index="{i}" rows="{len(table_rows)}"'
        table_fill = _shd_fill(tbl.find(qn("w:tblPr")))
        if table_fill:
            attrs += f' table_background="{xml_escape(table_fill)}"'
        if fills:
            shown = fills[:_MAX_CELL_FILLS]
            attrs += f' cell_backgrounds="{xml_escape(",".join(shown))}"'
            # Disclose the cap. A criterion like "all tables have the right
            # colour" reads an 8-value list as the table's complete set of
            # fills, so a 9th differing cell would be invisible — absence
            # taken as evidence, which is the failure this block exists to
            # remove.
            if len(fills) > len(shown):
                attrs += f' cell_backgrounds_listed="{len(shown)} of {len(fills)}"'
        else:
            attrs += ' cell_backgrounds="unset"'
        rows_out.append(f"    <table {attrs} />")

    omitted = len(tables) - len(rows_out)
    tail = f"\n    <!-- {omitted} more tables omitted -->" if omitted > 0 else ""
    return (
        f'  <tables count="{len(tables)}">\n'
        + "\n".join(rows_out)
        + tail
        + "\n  </tables>\n"
    )


# Link entries listed before summarising the rest, and the characters of link
# text kept per entry. Same disclose-the-cap rule as the tables above.
# Per KIND, not per section: hyperlinks, HYPERLINK fields and cross-references
# are capped independently so a document with many of one does not hide the
# others entirely. A shared cap would let 50 hyperlinks crowd out every
# cross-reference, and "do the cross-references resolve" is its own criterion.
# The omitted count below sums the per-kind overflow, so the total stays
# disclosed either way.
_MAX_LINK_ENTRIES_PER_KIND = 12
_MAX_BOOKMARK_NAMES = 24
_LINK_TEXT_CHARS = 60

# Field types that constitute a cross-reference. HYPERLINK is deliberately
# absent: it is a link, not a reference to another part of the document, and
# the two are separate claims a criterion may ask about.
_XREF_INSTRUCTIONS = ("REF", "PAGEREF", "NOTEREF")


class _Field(NamedTuple):
    """One Word field instance: its instruction and its displayed result."""

    instr: str
    text: str
    in_hyperlink: bool


def _has_ancestor(el: "Element", tag: str) -> bool:
    parent = el.getparent()
    while parent is not None:
        if parent.tag == tag:
            return True
        parent = parent.getparent()
    return False


def _field_instances(body: "Element") -> list[_Field]:
    """Every Word field in document order, with its displayed result text.

    A field is not one element: Word splits it into a w:fldChar "begin", one or
    more w:instrText carrying the instruction, a "separate", the runs holding
    the result the reader actually sees, and an "end". Reading only instrText
    gives the instruction with no way to tell WHICH issue a cross-reference
    points at, which is what a criterion asks. Fields nest (a REF inside a
    hyperlink inside a TOC), so the open fields are kept on a stack and text is
    attributed to the innermost.

    The older w:fldSimple spelling carries its instruction as an attribute and
    its result as children, so it is collected separately.
    """
    fields: list[_Field] = []
    stack: list[dict[str, Any]] = []

    for el in body.iter():
        tag = el.tag
        if tag == qn("w:fldChar"):
            char_type = el.get(qn("w:fldCharType"))
            if char_type == "begin":
                stack.append({"instr": [], "text": [], "in_result": False, "el": el})
            elif char_type == "separate" and stack:
                stack[-1]["in_result"] = True
            elif char_type == "end" and stack:
                done = stack.pop()
                text = "".join(done["text"]).strip()
                fields.append(
                    _Field(
                        "".join(done["instr"]).strip(),
                        text,
                        _has_ancestor(done["el"], qn("w:hyperlink")),
                    )
                )
                # A field can nest inside another, and the inner field's
                # result IS part of what the outer one displays. Attributing
                # the text only to the innermost left a wrapping HYPERLINK
                # with no label, so a criterion naming the link text could
                # not identify it.
                if stack and text:
                    stack[-1]["text"].append(text)
        elif tag == qn("w:instrText") and stack:
            stack[-1]["instr"].append(el.text or "")
        elif tag == qn("w:t") and stack and stack[-1]["in_result"]:
            stack[-1]["text"].append(el.text or "")
        elif tag == qn("w:fldSimple"):
            # Collected in the same traversal as the complex form, so the
            # entry cap drops the last fields rather than one spelling.
            fields.append(
                _Field(
                    (el.get(qn("w:instr")) or "").strip(),
                    "".join(t.text or "" for t in el.iter(qn("w:t"))).strip(),
                    _has_ancestor(el, qn("w:hyperlink")),
                )
            )

    return fields


def _instruction_name(instr: str) -> str:
    """The field type — the first word of the instruction, upper-cased."""
    parts = instr.split()
    return parts[0].upper() if parts else ""


def _xref_target(instr: str) -> str | None:
    """The bookmark a REF/PAGEREF/NOTEREF instruction points at."""
    parts = instr.split()
    return parts[1] if len(parts) > 1 else None


def _hyperlink_targets(
    zf: zipfile.ZipFile, part: str = "word/document.xml"
) -> dict[str, str]:
    """rId -> external target, from one part's own relationships."""
    directory, _, name = part.rpartition("/")
    try:
        rels = etree.fromstring(
            zf.read(f"{directory}/_rels/{name}.rels"), parser=_SAFE_PARSER
        )
    except (KeyError, etree.XMLSyntaxError):
        return {}
    out: dict[str, str] = {}
    for rel in rels.iter(f"{PKG_RELS_NS}Relationship"):
        rid, target = rel.get("Id"), rel.get("Target")
        if rid and target and rel.get("TargetMode") == "External":
            out[rid] = target
    return out


def _hyperlink_field_target(instr: str) -> tuple[str, str]:
    r"""(url, anchor) for a HYPERLINK field instruction.

    Word writes the destination quoted or bare and marks an in-document target
    with the \l switch. Tokenised rather than split on the substring "\l",
    because a bare Windows or UNC path contains one — C:\links\report.docx
    would otherwise be cut into a mangled url and a bookmark that does not
    exist, and both are handed to the judge as ground truth.
    """
    tokens = re.findall(r'"[^"]*"|\S+', instr)
    url = anchor = ""
    # What the next operand belongs to. \l names a bookmark; \o (ScreenTip)
    # and \t (target frame) also take one, and treating those as switches
    # without operands let a tooltip become the link's destination — reporting
    # an internal link as external, which is a fabricated fact rather than a
    # missing one. \m and \n take no operand.
    pending: str | None = None
    for token in tokens:
        if token.upper() == "HYPERLINK":
            continue
        if re.fullmatch(r"\\[a-zA-Z]", token):
            switch = token.lower()
            pending = (
                "anchor"
                if switch == "\\l"
                else "discard"
                if switch in ("\\o", "\\t")
                else None
            )
            continue
        value = token[1:-1] if token.startswith('"') and token.endswith('"') else token
        if pending == "anchor":
            anchor = anchor or value
        elif pending != "discard":
            url = url or value
        pending = None
    return url, anchor


def _destination_attr(key: str, value: str) -> str:
    """A link destination, saying so when it had to be cut.

    An exact-URL criterion cannot tell a truncated destination from a short
    one, so a silent cut reads as a complete value that simply does not match.
    """
    if len(value) <= _LINK_TEXT_CHARS:
        return f' {key}="{xml_escape(value)}"'
    return (
        f' {key}="{xml_escape(value[:_LINK_TEXT_CHARS])}"'
        f' {key}_truncated="{len(value)} chars"'
    )


def _links_xml(sources: list[tuple[str, Any, dict[str, str]]]) -> str:
    """Emit hyperlinks, cross-reference fields, and the bookmarks they target.

    The navigation target and the field's source bookmark are reported
    SEPARATELY and never reconciled: a cross-reference inserted as a hyperlink
    can navigate to one bookmark while displaying text pulled from another, so
    collapsing them would state the link goes somewhere it does not.
    """
    # Collected across every part that can hold a link, not just the body:
    # a footer holding a company URL reported as zero hyperlinks while the
    # attribute name claimed document-wide coverage.
    hyperlinks: list[tuple[str, Any]] = []
    field_links: list[tuple[str, _Field]] = []
    xrefs: list[tuple[str, _Field]] = []
    bookmarks: list[str] = []
    targets_by_part: dict[str, dict[str, str]] = {}

    for part, root, rel_targets in sources:
        targets_by_part[part] = rel_targets
        hyperlinks.extend((part, h) for h in root.iter(qn("w:hyperlink")))
        for field in _field_instances(root):
            name = _instruction_name(field.instr)
            # A HYPERLINK field wrapped in a w:hyperlink is the same single
            # link, so only the unwrapped ones are counted here.
            if name == "HYPERLINK" and not field.in_hyperlink:
                field_links.append((part, field))
            # First WORD, not a prefix: "REFX" is not a cross-reference.
            elif name in _XREF_INSTRUCTIONS:
                xrefs.append((part, field))
        bookmarks.extend(
            n
            for b in root.iter(qn("w:bookmarkStart"))
            if (n := b.get(qn("w:name"))) and n != "_GoBack"
        )

    header = (
        f'  <links hyperlinks="{len(hyperlinks) + len(field_links)}" '
        f'cross_reference_fields="{len(xrefs)}" '
        f'bookmarks="{len(bookmarks)}">\n'
    )

    def part_attr(part: str) -> str:
        """Name the part unless it is the body, so a header link is not read
        as body content."""
        return (
            ""
            if part == "word/document.xml"
            else f' part="{xml_escape(part.rpartition("/")[2])}"'
        )

    rows: list[str] = []
    for part, link in hyperlinks[:_MAX_LINK_ENTRIES_PER_KIND]:
        anchor = link.get(qn("w:anchor"))
        rid = link.get(f"{OFFICE_RELS_NS}id")
        text = "".join(t.text or "" for t in link.iter(qn("w:t"))).strip()
        if anchor:
            attrs = f'kind="internal" navigates_to="{xml_escape(anchor)}"'
            attrs += f' target_exists="{str(anchor in bookmarks).lower()}"'
        elif rid:
            url = targets_by_part.get(part, {}).get(rid)
            attrs = 'kind="external"'
            if url:
                attrs += _destination_attr("url", url)
        else:
            attrs = 'kind="unknown"'
        inner = "".join(i.text or "" for i in link.iter(qn("w:instrText"))).strip()
        if inner:
            # The displayed text of such a link comes from THIS field's
            # bookmark, which need not be the one w:anchor navigates to.
            attrs += f' displays_field="{xml_escape(inner[:_LINK_TEXT_CHARS])}"'
        if text:
            attrs += f' text="{xml_escape(text[:_LINK_TEXT_CHARS])}"'
        rows.append(f"    <hyperlink {attrs}{part_attr(part)} />")

    for part, field in field_links[:_MAX_LINK_ENTRIES_PER_KIND]:
        url, anchor = _hyperlink_field_target(field.instr)
        attrs = f'kind="{"internal" if anchor and not url else "external"}"'
        attrs += ' stored_as="field"'
        if url:
            attrs += _destination_attr("url", url)
        if anchor:
            attrs += _destination_attr("navigates_to", anchor)
            attrs += f' target_exists="{str(anchor in bookmarks).lower()}"'
        if field.text:
            attrs += f' text="{xml_escape(field.text[:_LINK_TEXT_CHARS])}"'
        rows.append(f"    <hyperlink {attrs}{part_attr(part)} />")

    for part, field in xrefs[:_MAX_LINK_ENTRIES_PER_KIND]:
        target = _xref_target(field.instr)
        attrs = f'field="{xml_escape(_instruction_name(field.instr))}"'
        if target:
            attrs += f' target="{xml_escape(target)}"'
            attrs += f' target_exists="{str(target in bookmarks).lower()}"'
        attrs += f' inside_hyperlink="{str(field.in_hyperlink).lower()}"'
        if field.text:
            attrs += f' text="{xml_escape(field.text[:_LINK_TEXT_CHARS])}"'
        rows.append(f"    <cross_reference {attrs}{part_attr(part)} />")

    if bookmarks:
        shown = bookmarks[:_MAX_BOOKMARK_NAMES]
        attrs = f'names="{xml_escape(",".join(shown))}"'
        if len(bookmarks) > len(shown):
            attrs += f' names_listed="{len(shown)} of {len(bookmarks)}"'
        rows.append(f"    <bookmarks {attrs} />")

    omitted = (
        max(0, len(hyperlinks) - _MAX_LINK_ENTRIES_PER_KIND)
        + max(0, len(field_links) - _MAX_LINK_ENTRIES_PER_KIND)
        + max(0, len(xrefs) - _MAX_LINK_ENTRIES_PER_KIND)
    )
    tail = f"\n    <!-- {omitted} more link entries omitted -->" if omitted else ""
    return header + "\n".join(rows) + tail + "\n  </links>\n"


def _page_background_xml(zf: zipfile.ZipFile, doc_root: "Element") -> str:
    """Emit the page background colour and whether Word actually displays it.

    w:background is inert unless settings.xml opts in via
    displayBackgroundShape, so reporting the colour without that flag would
    overstate what a reader sees.
    """
    bg = doc_root.find(qn("w:background"))
    color = bg.get(qn("w:color")) if bg is not None else None
    if color is None and bg is not None:
        color = bg.get("color")
    displayed = False
    if "word/settings.xml" in zf.namelist():
        try:
            st = etree.fromstring(zf.read("word/settings.xml"), parser=_SAFE_PARSER)
            dbs = st.find(qn("w:displayBackgroundShape"))
            displayed = dbs is not None and _on_off(dbs)
        except etree.XMLSyntaxError:
            displayed = False
    # w:color legitimately carries the literal "auto" (no explicit colour), not
    # just a hex triplet — emitting it verbatim produced color="#AUTO", which is
    # not a colour and reads to the judge like a deliberate fill. The run-colour
    # extractor above already screens "auto"; this now matches it.
    if color is None or color.lower() in ("auto", "none"):
        return f'  <page_background color="unset" displayed="{str(displayed).lower()}" />\n'
    return (
        f'  <page_background color="#{xml_escape(color.upper())}" '
        f'displayed="{str(displayed).lower()}" />\n'
    )


# Header and footer parts, in the order Word numbers them.
_HDR_FTR_RE = re.compile(r"word/(header|footer)\d+\.xml$")

# Characters of header/footer text kept, enough to identify a slogan.
_HDR_FTR_TEXT_CHARS = 80

# Header/footer parts listed. Word allows three of each per section, so a
# many-section document has many parts and this was the one section that
# emitted all of them — nothing downstream caps the text, and the metadata
# cache bounds entry count rather than size.
_MAX_HDR_FTR_PARTS = 8


def _rel_part_name(target: str) -> str:
    """A relationship Target as a package part name.

    A Target may be relative to the document part ("header1.xml") or
    package-absolute ("/word/header1.xml"). Prefixing "word/" unconditionally
    turned the absolute form into "word/word/header1.xml", which matched no
    part — so a document using that spelling reported no headers at all and
    its running header vanished from the metadata.
    """
    if target.startswith("/"):
        return target.lstrip("/")
    return f"word/{target}"


def _referenced_header_parts(
    zf: zipfile.ZipFile,
    doc_root: "Element",
) -> list[str]:
    """Header/footer parts the document actually renders, in part order.

    Orphaned parts that no sectPr points at are excluded — Word never renders
    them. Falls back to every part only when the relationships are unreadable.

    A referenced part is not necessarily a rendered one. A "first" header is
    painted only where its section sets w:titlePg, and an "even" header only
    where settings.xml opts the document into w:evenAndOddHeaders. Word leaves
    both references behind when the options are turned off, so reading the
    reference alone put text no reader sees in front of the judge — and a
    criterion about the header then answers off the wrong one.
    """
    all_parts = sorted(
        (n for n in zf.namelist() if _HDR_FTR_RE.match(n)), key=_natural_key
    )
    try:
        rels = etree.fromstring(
            zf.read("word/_rels/document.xml.rels"), parser=_SAFE_PARSER
        )
    except (KeyError, etree.XMLSyntaxError):
        return all_parts

    target_of = {
        rel.get("Id"): _rel_part_name(rel.get("Target") or "")
        for rel in rels.iter(f"{PKG_RELS_NS}Relationship")
    }
    rid = f"{OFFICE_RELS_NS}id"

    # Document-wide, unlike titlePg — one setting governs every section.
    even_rendered = False
    if "word/settings.xml" in zf.namelist():
        try:
            settings = etree.fromstring(
                zf.read("word/settings.xml"), parser=_SAFE_PARSER
            )
        except etree.XMLSyntaxError:
            settings = None
        if settings is not None:
            flag = settings.find(qn("w:evenAndOddHeaders"))
            even_rendered = flag is not None and _on_off(flag)

    referenced: list[str] = []
    # Per section, because titlePg is a property of the section: the same
    # "first" part can render in one section and not in another.
    #
    # A section that declares no reference of a kind inherits the previous
    # section's — this is what "Link to Previous" writes, or rather what it
    # omits. So the reference in force is carried forward rather than read off
    # the section alone: a first-page header declared in one section and
    # switched on by titlePg in the NEXT renders there, and looking only at
    # the section's own children lost it.
    # A w:sectPrChange holds the section as it was BEFORE a tracked edit, and
    # it nests a whole w:sectPr inside the live one. Walking every w:sectPr
    # descendant treated that snapshot as a section of its own, so a header the
    # edit replaced was reported as one the document still paints.
    historical = {
        old
        for change in doc_root.iter(qn("w:sectPrChange"))
        for old in change.iter(qn("w:sectPr"))
    }

    in_force: dict[tuple[str, str], list[str]] = {}
    for sect_pr in doc_root.iter(qn("w:sectPr")):
        if sect_pr in historical:
            continue
        title_pg = sect_pr.find(qn("w:titlePg"))
        first_rendered = title_pg is not None and _on_off(title_pg)
        for tag in ("headerReference", "footerReference"):
            # Declaring a kind REPLACES what was inherited for it; declaring
            # none inherits. Kept as a list because what a section declares is
            # taken as declared — a well-formed section names one part per
            # kind, and silently keeping the last of several would drop parts
            # this is supposed to be enumerating.
            declared: dict[str, list[str]] = {}
            # findall, not iter: a reference is a direct child of the section,
            # and descending would collect the ones inside its sectPrChange as
            # though this section had declared them.
            for ref in sect_pr.findall(qn(f"w:{tag}")):
                kind = ref.get(qn("w:type")) or "default"
                declared.setdefault(kind, []).append(ref.get(rid) or "")
            for kind, rids in declared.items():
                in_force[(tag, kind)] = rids
            for kind in ("default", "first", "even"):
                if kind == "first" and not first_rendered:
                    continue
                if kind == "even" and not even_rendered:
                    continue
                for ref_id in in_force.get((tag, kind), []):
                    part = target_of.get(ref_id)
                    if part in all_parts and part not in referenced:
                        referenced.append(part)
    return sorted(referenced, key=_natural_key)


def _headers_footers_xml(
    zf: zipfile.ZipFile,
    doc_root: "Element",
    bold_style_ids: frozenset[str],
    style_fonts: dict[str, tuple[str | None, float | None]],
    page_background_is_dark: bool = False,
    shaded_style_ids: dict[str, frozenset[str]] | None = None,
) -> str:
    """Emit the text and run styles of every header and footer part.

    Its own section rather than folded into <run_styles>: a criterion about
    body text must not start matching header runs, and the existing run-style
    groups must not shift under criteria already written against them.
    """
    parts = _referenced_header_parts(zf, doc_root)
    if not parts:
        # Same load-bearing count as the tables and links above: without it,
        # a document with no header is indistinguishable from one whose
        # header went unread.
        return '  <headers_footers count="0" />\n'

    # Split the window between headers and footers instead of slicing the
    # sorted list: "footer" sorts before "header", so a plain slice dropped
    # every header once a document had enough footers to fill the cap.
    headers = [n for n in parts if "/header" in n]
    footers = [n for n in parts if "/header" not in n]
    half = _MAX_HDR_FTR_PARTS // 2
    shown = headers[: max(half, _MAX_HDR_FTR_PARTS - len(footers))]
    shown += footers[: _MAX_HDR_FTR_PARTS - len(shown)]
    shown = sorted(shown, key=_natural_key)

    blocks: list[str] = []
    for name in shown:
        try:
            root = etree.fromstring(zf.read(name), parser=_SAFE_PARSER)
        except (KeyError, etree.XMLSyntaxError) as e:
            blocks.append(
                f'    <header_footer part="{xml_escape(name.split("/")[-1])}" '
                f'unreadable="{xml_escape(type(e).__name__)}" />'
            )
            continue

        paragraphs = [_extract_paragraph(p, {}) for p in root.iter(qn("w:p"))]
        text = " ".join(
            t.strip()
            for para in paragraphs
            for seg in para.get("segments", [])
            for t in [str(seg.get("text") or "")]
            if t.strip()
        )
        kind = "header" if "/header" in name else "footer"
        attrs = f' kind="{kind}" part="{xml_escape(name.split("/")[-1])}"'
        if text:
            attrs += f' text="{xml_escape(text[:_HDR_FTR_TEXT_CHARS])}"'
        blocks.append(
            _run_styles_xml(
                paragraphs,
                bold_style_ids,
                style_fonts,
                tag="header_footer",
                extra_attrs=attrs,
                indent="    ",
                sample_text=True,
                page_background_is_dark=page_background_is_dark,
                shaded_style_ids=shaded_style_ids,
            ).rstrip("\n")
        )

    omitted = len(parts) - len(shown)
    tail = (
        f"\n    <!-- {omitted} more header/footer parts omitted -->" if omitted else ""
    )
    return (
        f'  <headers_footers count="{len(parts)}">\n'
        + "\n".join(blocks)
        + tail
        + "\n  </headers_footers>\n"
    )


_SHADING_LAYERS = ("paragraph", "cell", "table", "run")


def _style_paints_dark(
    style_id: str | None,
    shaded: dict[str, frozenset[str]],
    overridden: set[str],
) -> bool:
    """Whether `style_id` paints a not-known-light layer nothing overrode.

    A style can set w:pPr, w:rPr, w:tcPr and w:tblPr at once, so asking only
    about the layer its NAME suggests missed the rest: a paragraph style that
    shades through w:rPr, or a table style that shades through w:pPr, was
    recorded and then never consulted. Direct formatting silences the one layer
    it sits on, not the style.
    """
    if not style_id:
        return False
    return any(
        style_id in shaded[layer] and layer not in overridden
        for layer in _SHADING_LAYERS
    )


def _shaded_style_ids(
    zf: zipfile.ZipFile,
    root: "Element | None" = None,
) -> dict[str, frozenset[str]]:
    """Style ids that paint a dark or unresolvable background behind text.

    Shading can come from the paragraph STYLE rather than the paragraph, and
    reading only the direct w:shd meant a styled dark block looked like a
    plain white page — automatic text over it resolved to black although Word
    renders it light.

    Character styles count too — their fill lives in w:rPr, and a run
    wearing one sits on that background whatever the paragraph does.

    Table styles count too, and through every holder they can paint from:
    w:tcPr, w:tblPr and the conditional w:tblStylePr bands. A table style is
    how Word paints most shaded tables, and reading only direct formatting
    resolved automatic text to black inside a cell the style paints navy.

    Follows w:basedOn like _bold_style_ids and _style_run_fonts: a style that
    inherits its fill states nothing itself, so stopping at the style's own
    w:shd treated it as unshaded. Tri-state per style for the same reason bold
    is: an explicit light fill must STOP the walk rather than let an
    ancestor's dark fill leak through.
    """
    root = root if root is not None else _styles_root(zf)
    if root is None:
        return {layer: frozenset() for layer in _SHADING_LAYERS}

    own_dark: dict[str, dict[str, bool | None]] = {}
    based_on: dict[str, str] = {}
    for style in root.iter(qn("w:style")):
        sid = style.get(qn("w:styleId"))
        if not sid:
            continue
        # Every holder a style can paint from. A paragraph style uses w:pPr;
        # a table style paints cells through w:tcPr/w:tblPr and the
        # conditional w:tblStylePr bands, and any of them can be the one
        # behind the text.
        # Tagged with the LAYER each paints, because a holder's own w:shd
        # overrides only the layer it sits on. A cell that clears itself beats
        # the style's per-cell fill; the style's table-level fill is a
        # different layer and still renders behind it.
        holders = [
            ("paragraph", style.find(qn("w:pPr"))),
            ("cell", style.find(qn("w:tcPr"))),
        ]
        # A character style carries its shading in w:rPr, and it sits closest
        # of all to the text.
        holders.append(("run", style.find(qn("w:rPr"))))
        holders.append(("table", style.find(qn("w:tblPr"))))
        for cond in style.iter(qn("w:tblStylePr")):
            holders.append(("cell", cond.find(qn("w:tcPr"))))
            holders.append(("table", cond.find(qn("w:tblPr"))))
        # Through _shading_state like the run and holder paths. This is the
        # THIRD reader of w:shd, and it is the one that reads styles.xml. When
        # it kept its own copy of the parsing, a pct25 pattern read as a fill
        # here while the identical w:shd on a run read as unresolvable, and a
        # style that cleared its parent's dark fill inherited it anyway.
        stated: dict[str, bool | None] = {}
        for layer, holder in holders:
            shd = holder.find(qn("w:shd")) if holder is not None else None
            if shd is None:
                continue
            fill, overrides = _shading_state(shd)
            if fill == _UNKNOWN_SHADING:
                # A pattern or an unresolvable themeFill: not known light.
                stated[layer] = True
            elif fill:
                if not _is_known_light(fill):
                    stated[layer] = True
                elif stated.get(layer) is not True:
                    # An explicit light fill answers this layer unless a later
                    # holder on the same layer paints something darker.
                    stated[layer] = False
            elif overrides:
                # w:val="nil" -- this style states this LAYER paints nothing,
                # stopping the basedOn walk for that layer rather than letting
                # a parent's dark fill leak through.
                stated.setdefault(layer, False)
        own_dark[sid] = stated
        parent = style.find(qn("w:basedOn"))
        if parent is not None and parent.get(qn("w:val")):
            based_on[sid] = str(parent.get(qn("w:val")))

    def resolve(sid: str, layer: str) -> bool:
        """Whether `sid` paints a not-known-light background on `layer`.

        Per layer, because a style can paint dark at table level while saying
        nothing per cell. Collapsing the two let a cell that cleared its own
        shading suppress the table-level fill still rendering behind it.
        """
        seen: set[str] = set()
        cur = sid
        while cur and cur not in seen:
            seen.add(cur)
            said = own_dark.get(cur, {}).get(layer)
            if said is not None:
                return said
            cur = based_on.get(cur, "")
        return False

    return {
        layer: frozenset(sid for sid in own_dark if resolve(sid, layer))
        for layer in _SHADING_LAYERS
    }


def _run_styles_xml(
    paragraphs: list[dict[str, Any]],
    bold_style_ids: frozenset[str] = frozenset(),
    style_fonts: dict[str, tuple[str | None, float | None]] | None = None,
    tag: str = "run_styles",
    extra_attrs: str = "",
    indent: str = "  ",
    sample_text: bool = False,
    page_background_is_dark: bool = False,
    shaded_style_ids: dict[str, frozenset[str]] | None = None,
) -> str:
    """Collapse runs into (paragraph style, bold, colour, size, font) groups.

    Paragraph style is part of the key because the criteria that need this
    distinguish header text from body text — "all bolded non-header body text
    is #CD8D81" is unanswerable from run properties alone.
    """
    groups: dict[tuple[Any, ...], int] = {}
    # Only populated when a caller asks. A style group says nothing about
    # WHICH text carries it, and in a two-run header ("slogan at 8pt, tagline
    # at 10pt") that leaves the judge guessing which group a criterion about
    # the slogan refers to. Body run styles deliberately stay without it: they
    # are collapsed across hundreds of runs where one sample would mislead.
    samples: dict[tuple[Any, ...], str] = {}
    for para in paragraphs:
        pstyle = para.get("style_name") or "Normal"
        for seg in para.get("segments", []):
            runs = (
                [seg]
                if seg.get("kind") == "run"
                else list(seg.get("runs", []))
                if seg.get("kind") == "tracked_change"
                else []
            )
            for run in runs:
                if not str(run.get("text") or "").strip():
                    continue
                # Tri-state, not bool(): the extractor returns None when the
                # run carries no w:b at all, which means "inherit from the
                # paragraph style". Collapsing that to False reported Heading1
                # text as not bold even though its style sets bold.
                raw_bold = run.get("bold")
                if raw_bold is None:
                    bold = "true" if pstyle in bold_style_ids else "unset"
                    source = "paragraph_style" if bold == "true" else None
                else:
                    bold = "true" if raw_bold else "false"
                    source = None
                # Tri-state as bold is: a run with no w:rFonts/w:sz takes
                # them from its paragraph style, which is how Word writes
                # documents by default.
                inherited_font, inherited_size = (style_fonts or {}).get(
                    pstyle, (None, None)
                )
                size = run.get("font_size_pt")
                size_source = None
                if size is None and inherited_size is not None:
                    size, size_source = inherited_size, "paragraph_style"
                font = run.get("font_name")
                font_source = None
                if not font and inherited_font:
                    font, font_source = inherited_font, "paragraph_style"
                # w:color val="auto" renders black only over a light
                # background, so it is resolved only when the background is
                # known light and left as "auto" otherwise.
                color = run.get("color_hex")
                color_source = None
                if color is None and run.get("color_auto"):
                    # Every background that applies, with no precedence
                    # between them: run, paragraph, cell, table, character
                    # style, paragraph style, table style, page. Black is
                    # reported only if all of them are
                    # known light; anything dark or unreadable leaves the
                    # value "auto". Ordering these was the source of repeated
                    # bugs and is not needed — where they agree the answer is
                    # the same, and where they disagree it is "unresolved".
                    layers = para.get("shading_fills", [])
                    # Declared, not merely resolved: a holder that cleared its
                    # shading has still overridden its style.
                    declared = para.get("shading_declared_by") or ()
                    shaded = shaded_style_ids or {
                        layer: frozenset() for layer in _SHADING_LAYERS
                    }
                    backgrounds = [
                        b
                        for b in [
                            run.get("run_shading"),
                            *(fill for _, fill in layers),
                        ]
                        if b
                    ]
                    # A holder that spoke has overridden whatever its style
                    # said for THAT LAYER, so the style is not consulted there.
                    # It is still consulted on every other layer it paints:
                    # a style can set w:pPr, w:rPr, w:tcPr and w:tblPr at once,
                    # and a cell clearing itself silences only the per-cell one.
                    overridden = set(declared) | {kind for kind, _ in layers}
                    if run.get("run_shading") or run.get("run_shading_cleared"):
                        overridden.add("run")

                    on_dark = (
                        any(not _is_known_light(b) for b in backgrounds)
                        or _style_paints_dark(pstyle, shaded, overridden)
                        or _style_paints_dark(
                            para.get("table_style"), shaded, overridden
                        )
                        or _style_paints_dark(run.get("run_style"), shaded, overridden)
                        or page_background_is_dark
                    )
                    color, color_source = (
                        ("auto", "unresolved_over_this_background")
                        if on_dark
                        else ("#000000", "auto")
                    )
                key = (
                    pstyle,
                    bold,
                    source,
                    color,
                    size,
                    font,
                    size_source,
                    font_source,
                    color_source,
                )
                groups[key] = groups.get(key, 0) + 1
                if sample_text and key not in samples:
                    samples[key] = str(run.get("text") or "").strip()

    if not groups:
        return f"{indent}<{tag}{extra_attrs} />\n"

    ordered = sorted(groups.items(), key=lambda kv: (-kv[1], str(kv[0])))
    rows: list[str] = []
    for (
        pstyle,
        bold,
        bold_source,
        color,
        size,
        font,
        size_source,
        font_source,
        color_source,
    ), count in ordered[:_MAX_RUN_STYLE_GROUPS]:
        attrs = f'count="{count}" paragraph_style="{xml_escape(str(pstyle))}"'
        attrs += f' bold="{bold}"'
        if bold_source:
            attrs += f' bold_source="{bold_source}"'
        # An unset colour inherits from the style, so say so rather than
        # implying black — a criterion about an explicit hex needs the
        # difference between "set to X" and "never set".
        if color == "auto":
            attrs += ' color="auto"'
        elif color:
            attrs += f' color="{xml_escape(color.upper())}"'
        else:
            attrs += ' color="inherited"'
        if color_source:
            attrs += f' color_source="{color_source}"'
        # Always emitted, "inherited" included — see the README on absence.
        if size is not None:
            attrs += f' size_pt="{size}"'
            if size_source:
                attrs += f' size_pt_source="{size_source}"'
        else:
            attrs += ' size_pt="inherited"'
        if font:
            attrs += f' font_name="{xml_escape(str(font))}"'
            if font_source:
                attrs += f' font_name_source="{font_source}"'
        else:
            attrs += ' font_name="inherited"'
        sample = samples.get(
            (
                pstyle,
                bold,
                bold_source,
                color,
                size,
                font,
                size_source,
                font_source,
                color_source,
            )
        )
        if sample:
            attrs += f' text="{xml_escape(sample[:_HDR_FTR_TEXT_CHARS])}"'
        rows.append(f"{indent}  <style {attrs} />")

    omitted = len(ordered) - len(rows)
    tail = (
        f"\n{indent}  <!-- {omitted} rarer style combinations omitted -->"
        if omitted
        else ""
    )
    return (
        f"{indent}<{tag}{extra_attrs}>\n"
        + "\n".join(rows)
        + tail
        + f"\n{indent}</{tag}>\n"
    )


async def _page_count(file_bytes: bytes, file_name: str) -> tuple[int | None, bool]:
    """Real page count, plus whether the failure to get one may be transient.

    Returns (count, retryable). A missing LibreOffice binary is stable for the
    life of the process, so "unknown" is then the file's real answer and the
    whole summary — run styles, tables, chart fills, all extracted fine — can be
    cached like any other. A conversion that fails *with* the binary present may
    be a timeout or a non-zero exit, so that one is marked retryable and the
    cache does not persist it.

    A DOCX is a flowing format — pages only exist once something lays it out —
    so criteria like "includes exactly one page" are unanswerable from the
    document XML. Reuses the docx_to_pdf transformation (LibreOffice) and counts
    the result, which is the same renderer the image path already relies on, so
    the number agrees with what a reviewer would see.

    Costs a subprocess, so it is only paid for docx and only once per file per
    grading run (cached_style_text coalesces the whole extraction).
    """
    if not find_libreoffice():
        logger.info(
            f"[TRANSFORM] LibreOffice unavailable; page count for {file_name} is "
            f"permanently unknown in this process"
        )
        return None, False
    try:
        converted = await docx_to_pdf(file_bytes, file_name)
    except Exception as e:
        logger.warning(
            f"[TRANSFORM] page count unavailable for {file_name}: "
            f"{type(e).__name__}: {e}"
        )
        return None, True
    if not converted.pdf_bytes:
        logger.info(
            f"[TRANSFORM] no PDF produced for {file_name}; page count unavailable"
        )
        return None, True
    try:
        return len(PdfReader(io.BytesIO(converted.pdf_bytes)).pages), False
    except Exception as e:
        logger.warning(
            f"[TRANSFORM] could not read converted PDF for {file_name}: "
            f"{type(e).__name__}: {e}"
        )
        return None, True


def _docx_style_summary_xml(
    file_bytes: bytes,
    file_name: str,
    page_count: int | None = None,
    retryable: bool = False,
) -> str:
    # One Document build and one ZIP open for the whole summary. This used to
    # parse the document twice — once inside docx_to_style_metadata and again
    # here — and open the package twice on top of that, for every docx in every
    # criterion that touches one. Both are handed to docx_to_style_metadata so
    # it reuses them rather than repeating the work.
    doc = Document(io.BytesIO(file_bytes))
    doc_root = doc.element
    body = doc_root.body  # pyright: ignore[reportAttributeAccessIssue]

    with zipfile.ZipFile(io.BytesIO(file_bytes), "r") as zf:
        data = docx_to_style_metadata(
            file_bytes, file_name, reuse_doc=doc, reuse_zip=zf
        )
        charts = _charts_xml(zf)
        background = _page_background_xml(zf, doc_root)
        styles_root = _styles_root(zf)
        bold_styles = _bold_style_ids(zf, styles_root)
        shaded_styles = _shaded_style_ids(zf, styles_root)
        style_fonts = _style_run_fonts(zf, styles_root)
        link_sources: list[tuple[str, Any, dict[str, str]]] = [
            ("word/document.xml", body, _hyperlink_targets(zf))
        ]
        # Headers/footers plus footnotes/endnotes: all hold visible text that
        # can carry a link, and the count claims document-wide coverage.
        extra_parts = _referenced_header_parts(zf, doc_root) + [
            n for n in ("word/footnotes.xml", "word/endnotes.xml") if n in zf.namelist()
        ]
        for part in extra_parts:
            try:
                part_root = etree.fromstring(zf.read(part), parser=_SAFE_PARSER)
            except (KeyError, etree.XMLSyntaxError):
                continue
            link_sources.append((part, part_root, _hyperlink_targets(zf, part)))
        bg_dark = _page_background_is_dark(zf, doc_root)
        headers_footers = _headers_footers_xml(
            zf,
            doc_root,
            bold_styles,
            style_fonts,
            bg_dark,
            shaded_styles,
        )

    # Deliberately carries no filename. cached_style_text keys on content hash
    # alone, so two byte-identical documents at different paths share an entry
    # — embedding the name here would label the second one with the first's.
    # The eval already wraps this in <STYLE_METADATA file="..."> using the real
    # artifact path, so the name is never actually lost.
    if page_count:
        pages_attr = f'pages="{page_count}"'
    else:
        # Only a possibly-transient failure is flagged degraded. A renderer that
        # is simply absent gives the same answer every time, so the run styles,
        # tables and chart fills extracted alongside it must stay cacheable —
        # marking them degraded made every verifier re-parse the document.
        pages_attr = 'pages="unknown"' + (' data-degraded="true"' if retryable else "")
    return (
        f'<style_metadata {pages_attr} paragraphs="{data["paragraph_count"]}">\n'
        + background
        + _run_styles_xml(
            data["paragraphs"],
            bold_styles,
            style_fonts,
            page_background_is_dark=bg_dark,
            shaded_style_ids=shaded_styles,
        )
        + _tables_xml(body)  # pyright: ignore[reportUnknownArgumentType]
        + _links_xml(link_sources)
        + headers_footers
        + charts
        + "</style_metadata>\n"
    )


async def _extract_docx_style_text(file_bytes: bytes, file_name: str) -> str:
    # Conversion is a subprocess and awaits; the XML parse is CPU-bound and goes
    # to a thread so it doesn't stall other verifiers' LLM calls.
    pages, retryable = await _page_count(file_bytes, file_name)
    return await asyncio.to_thread(
        _docx_style_summary_xml, file_bytes, file_name, pages, retryable
    )


async def docx_to_style_metadata_output(
    file_bytes: bytes, file_name: str
) -> TransformationOutput:
    """Run/table/chart style facts for a DOCX as judge-readable XML.

    Answers criteria like "bolded non-header body text is #CD8D81" or "all
    embedded tables and charts have a transparent background" from the
    document XML, instead of having the judge guess colours off a render.

    The registry maps this to the whole DOCX family, so .doc (OLE2) and .odt
    (OpenDocument, a zip but not an OOXML package) reach it too — python-docx
    reads neither. They answer with an
    explanatory note rather than a bare BadZipFile, because the agentic judge
    surfaces the transformation's error text to the model and "File is not a zip
    file" invites a retry, while a stated limitation does not.
    """
    if not is_ooxml_package(file_bytes):
        logger.info(
            f"[TRANSFORM] {file_name} is not an OOXML package — no docx style "
            f"metadata available"
        )
        return TransformationOutput(
            text=(
                '<style_metadata unsupported="true" data-degraded="true">Style metadata requires a '
                ".docx (OOXML) file; legacy .doc and OpenDocument .odt are not "
                "supported by this extractor.</style_metadata>\n"
            )
        )

    text = await cached_style_text(
        file_bytes, file_name, "docx", _extract_docx_style_text
    )
    return TransformationOutput(text=text)
