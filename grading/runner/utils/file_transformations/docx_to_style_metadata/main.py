"""Extract paragraph, font, numbering, and tracked-change metadata from a DOCX.

The dict-returning extractor is a domain-specific utility used by the
docx_style_verifier_apex_v2 eval. docx_to_style_metadata_output wraps it as a
registered transformation for the generic multi-representation judge, emitting
a bounded run/table/chart style summary rather than the full dict.

The output is consumed by an LLM judge in `text` mode. Tracked changes are
preserved (not stripped) so style criteria can grade redline hygiene.
"""

import asyncio
import io
import re
import zipfile
from typing import Any

import lxml.etree as etree
from docx import Document
from docx.oxml.ns import qn  # pyright: ignore[reportUnknownVariableType]
from loguru import logger
from pypdf import PdfReader

from ...file_extraction.utils.chart_extraction import find_libreoffice
from ..docx_to_pdf.main import docx_to_pdf
from ..models import TransformationOutput
from ..style_metadata_cache import cached_style_text, is_ooxml_package
from ..xml_utils import xml_escape

_WNS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_SAFE_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)


def _on_off(el: etree._Element) -> bool:  # pyright: ignore[reportPrivateUsage]
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


def _extract_run_style(r: etree._Element) -> dict[str, Any]:  # pyright: ignore[reportPrivateUsage]
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
    if rpr is not None:
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
        color = rpr.find(qn("w:color"))
        if color is not None:
            val = color.get(qn("w:val"))
            if val and val != "auto":
                color_hex = f"#{val}" if not val.startswith("#") else val

    return {
        "text": text,
        "font_name": font_name,
        "font_size_pt": font_size_pt,
        "bold": bold,
        "italic": italic,
        "underline": underline,
        "color_hex": color_hex,
    }


def _extract_paragraph(
    p: etree._Element, comments_by_id: dict[str, str]
) -> dict[str, Any]:  # pyright: ignore[reportPrivateUsage]
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
    for child in p:
        tag_local = etree.QName(child).localname
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

    plain_text = "".join(seg.get("text", "") for seg in segments if seg.get("text"))

    return {
        "style_name": style_name,
        "alignment": alignment,
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

    paragraphs_raw = [p for p in body.iter(p_tag)]  # pyright: ignore[reportUnknownArgumentType,reportUnknownVariableType]
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

_DML_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
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


def _fill_of(sp_pr: "etree._Element | None") -> str | None:  # pyright: ignore[reportPrivateUsage]
    """Describe a DrawingML <spPr> fill as 'none', '#RRGGBB', or a scheme name.

    Returns None when the element is absent, which in OOXML means "inherit"
    rather than "transparent" — the distinction matters for a criterion that
    accepts transparency, so the two are never collapsed.
    """
    if sp_pr is None:
        return None
    if sp_pr.find(f"{{{_DML_NS}}}noFill") is not None:
        return "none"
    solid = sp_pr.find(f"{{{_DML_NS}}}solidFill")
    if solid is None:
        return None
    srgb = solid.find(f"{{{_DML_NS}}}srgbClr")
    if srgb is not None and srgb.get("val"):
        return f"#{str(srgb.get('val')).upper()}"
    scheme = solid.find(f"{{{_DML_NS}}}schemeClr")
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


def _styles_root(zf: zipfile.ZipFile) -> "etree._Element | None":  # pyright: ignore[reportPrivateUsage]
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
    root: "etree._Element | None" = None,  # pyright: ignore[reportPrivateUsage]
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


def _theme_fonts(zf: zipfile.ZipFile) -> dict[str, str]:
    """The major/minor latin typefaces the theme defines.

    Word does not write literal font names into styles.xml by default. It
    writes theme references — `w:asciiTheme="minorHAnsi"` — and the actual name
    lives in word/theme/theme1.xml as `<a:minorFont><a:latin typeface="Aptos"/>`.
    Reading only w:ascii therefore resolves nothing on a stock document, which
    is how "only fonts from the Aptos family" failed against a document whose
    theme is Aptos / Aptos Display.
    """
    part = next((n for n in sorted(zf.namelist()) if _THEME_PART_RE.fullmatch(n)), None)
    if part is None:
        return {}
    try:
        root = etree.fromstring(zf.read(part), parser=_SAFE_PARSER)
    except etree.XMLSyntaxError:
        return {}
    out: dict[str, str] = {}
    for slot in ("major", "minor"):
        el = root.find(f".//{{{_DML_NS}}}{slot}Font/{{{_DML_NS}}}latin")
        face = el.get("typeface") if el is not None else None
        if face:
            out[slot] = face
    return out


def _style_run_fonts(
    zf: zipfile.ZipFile,
    root: "etree._Element | None" = None,  # pyright: ignore[reportPrivateUsage]
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

    def _own(rpr: "etree._Element | None") -> tuple[str | None, float | None]:  # pyright: ignore[reportPrivateUsage]
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


def _shd_fill(el: "etree._Element | None") -> str | None:  # pyright: ignore[reportPrivateUsage]
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


def _owned_by(el: "etree._Element", tbl: "etree._Element") -> bool:  # pyright: ignore[reportPrivateUsage]
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


def _tables_xml(body: "etree._Element") -> str:  # pyright: ignore[reportPrivateUsage]
    """Emit table count, each table's cell shading, and its table-wide shading.

    The count alone is load-bearing: a criterion about "all embedded tables"
    is vacuously satisfied at zero, and without the count the judge cannot
    tell absence from unreported.

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


def _page_background_xml(zf: zipfile.ZipFile, doc_root: "etree._Element") -> str:  # pyright: ignore[reportPrivateUsage]
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


def _run_styles_xml(
    paragraphs: list[dict[str, Any]],
    bold_style_ids: frozenset[str] = frozenset(),
    style_fonts: dict[str, tuple[str | None, float | None]] | None = None,
) -> str:
    """Collapse runs into (paragraph style, bold, colour, size, font) groups.

    Paragraph style is part of the key because the criteria that need this
    distinguish header text from body text — "all bolded non-header body text
    is #CD8D81" is unanswerable from run properties alone.
    """
    groups: dict[tuple[Any, ...], int] = {}
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
                # Same tri-state treatment bold already gets. A run with no
                # w:rFonts / w:sz inherits from its paragraph style, which is
                # how Word writes documents by default, so resolving through
                # the style chain is what makes the attribute answerable at
                # all. Where even the style says nothing, the emitter below
                # writes "inherited" rather than dropping the attribute — a
                # missing font_name was being read as a non-compliant one.
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
                key = (
                    pstyle,
                    bold,
                    source,
                    run.get("color_hex"),
                    size,
                    font,
                    size_source,
                    font_source,
                )
                groups[key] = groups.get(key, 0) + 1

    if not groups:
        return "  <run_styles />\n"

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
    ), count in ordered[:_MAX_RUN_STYLE_GROUPS]:
        attrs = f'count="{count}" paragraph_style="{xml_escape(str(pstyle))}"'
        attrs += f' bold="{bold}"'
        if bold_source:
            attrs += f' bold_source="{bold_source}"'
        # An unset colour inherits from the style, so say so rather than
        # implying black — a criterion about an explicit hex needs the
        # difference between "set to X" and "never set".
        attrs += (
            f' color="{xml_escape(color.upper())}"' if color else ' color="inherited"'
        )
        # Never omit these two. An absent attribute reads as a violated one
        # to a judge told this block is ground truth, which is how a document
        # whose styles set Aptos failed an "only Aptos fonts" criterion.
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
        rows.append(f"    <style {attrs} />")

    omitted = len(ordered) - len(rows)
    tail = (
        f"\n    <!-- {omitted} rarer style combinations omitted -->" if omitted else ""
    )
    return "  <run_styles>\n" + "\n".join(rows) + tail + "\n  </run_styles>\n"


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
    doc_root = doc.element  # pyright: ignore[reportAttributeAccessIssue]
    body = doc_root.body  # pyright: ignore[reportAttributeAccessIssue]

    with zipfile.ZipFile(io.BytesIO(file_bytes), "r") as zf:
        data = docx_to_style_metadata(
            file_bytes, file_name, reuse_doc=doc, reuse_zip=zf
        )
        charts = _charts_xml(zf)
        background = _page_background_xml(zf, doc_root)  # pyright: ignore[reportUnknownArgumentType]
        styles_root = _styles_root(zf)
        bold_styles = _bold_style_ids(zf, styles_root)
        style_fonts = _style_run_fonts(zf, styles_root)

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
        + _run_styles_xml(data["paragraphs"], bold_styles, style_fonts)
        + _tables_xml(body)  # pyright: ignore[reportUnknownArgumentType]
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
