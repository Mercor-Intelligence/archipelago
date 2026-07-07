"""Extract paragraph, font, numbering, and tracked-change metadata from a DOCX.

This is a domain-specific utility called only by the docx_style_verifier_apex_v2
eval, NOT registered in the TRANSFORMATION_REGISTRY.

The output is consumed by an LLM judge in `text` mode. Tracked changes are
preserved (not stripped) so style criteria can grade redline hygiene.
"""

import io
import zipfile
from typing import Any

import lxml.etree as etree
from docx import Document
from docx.oxml.ns import qn  # pyright: ignore[reportUnknownVariableType]

_WNS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_SAFE_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)


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
            bold = b.get(qn("w:val"), "1") not in ("0", "false")
        i = rpr.find(qn("w:i"))
        if i is not None:
            italic = i.get(qn("w:val"), "1") not in ("0", "false")
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
    src = io.BytesIO(file_bytes)
    with zipfile.ZipFile(src, "r") as zf:
        comments = _load_comments(zf)

    # python-docx for top-level access (sections, body) is convenient
    doc = Document(io.BytesIO(file_bytes))
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
