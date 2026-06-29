"""
Utilities for handling OOXML tracked changes in DOCX files.

Two complementary operations:
1. accept_tracked_changes_in_docx — strips tracked-change markup so extractors
   see only the accepted document content.
2. extract_tracked_changes_as_text — converts tracked-change markup into a
   human-readable text summary for LLM judges that need to see redlines,
   grouped by revision author and annotated with the nearest section heading.
"""

import io
import re
import zipfile

import lxml.etree as etree
from loguru import logger

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DOCUMENT_XML_PATH = "word/document.xml"
_SAFE_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)

# A paragraph is treated as a section heading if it begins with an explicit
# "SECTION"/"ARTICLE" marker or a DOTTED clause number (6.9, 11.2.1). Bare
# integers are intentionally excluded so street addresses and dollar amounts
# ("301 South College St", "$1,700,000") are not mistaken for headings.
_SECTION_HEADING_RE = re.compile(r"^(SECTION|ARTICLE)\s+[0-9IVXLC]+\b|^[0-9]+\.[0-9]+")


def accept_tracked_changes_in_docx(docx_bytes: bytes) -> bytes:
    """Accept all tracked changes in a DOCX file, returning modified bytes.

    - Removes <w:del> elements (deleted text)
    - Removes <w:moveFrom> elements (text moved away)
    - Unwraps <w:ins> elements (keeps inserted content)
    - Unwraps <w:moveTo> elements (keeps moved-to content)

    Returns the original bytes unchanged if any error occurs.
    """
    try:
        src = io.BytesIO(docx_bytes)
        if not zipfile.is_zipfile(src):
            return docx_bytes
        src.seek(0)

        with zipfile.ZipFile(src, "r") as zin:
            if _DOCUMENT_XML_PATH not in zin.namelist():
                return docx_bytes

            tree = etree.fromstring(zin.read(_DOCUMENT_XML_PATH), parser=_SAFE_PARSER)

            _remove_elements(tree, f"{{{_WORD_NS}}}del")
            _remove_elements(tree, f"{{{_WORD_NS}}}moveFrom")
            _unwrap_elements(tree, f"{{{_WORD_NS}}}ins")
            _unwrap_elements(tree, f"{{{_WORD_NS}}}moveTo")

            modified_xml = etree.tostring(
                tree, xml_declaration=True, encoding="UTF-8", standalone=True
            )

            dst = io.BytesIO()
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    if item.filename == _DOCUMENT_XML_PATH:
                        zout.writestr(item, modified_xml)
                    else:
                        zout.writestr(item, zin.read(item.filename))

            return dst.getvalue()

    except Exception:
        logger.warning(
            "Failed to strip tracked changes from DOCX; using original bytes"
        )
        return docx_bytes


def extract_tracked_changes_as_text(docx_bytes: bytes) -> str | None:
    """Extract tracked changes from a DOCX file as human-readable text.

    Walks document.xml looking for w:ins, w:del, w:moveFrom, and w:moveTo
    elements. Changes are grouped by their revision author and annotated with
    the nearest section heading, so a judge can attribute redlines (e.g.
    distinguish a model's new edits from pre-existing redline) and locate each
    change by section.

    Returns a structured summary like:
        === DOCUMENT REDLINES ===

        Author: Jane Smith
          [3.1 Contract Price] DELETED: "[$ ____________]"
          [3.1 Contract Price] INSERTED: "$1,700,000"

    Changes with no detectable section fall back to a [Para N] index. Returns
    None if the DOCX has no tracked changes or on any error.
    """
    try:
        tree = _parse_document_xml(docx_bytes)
        if tree is None:
            return None

        para_tag = f"{{{_WORD_NS}}}p"
        author_attr = f"{{{_WORD_NS}}}author"
        tag_map = {
            f"{{{_WORD_NS}}}ins": "INSERTED",
            f"{{{_WORD_NS}}}del": "DELETED",
            f"{{{_WORD_NS}}}moveFrom": "MOVED FROM",
            f"{{{_WORD_NS}}}moveTo": "MOVED TO",
        }

        lines_by_author: dict[str, list[str]] = {}
        author_order: list[str] = []
        current_section = ""
        para_idx = 0
        for p in tree.iter(para_tag):
            para_idx += 1
            heading = _detect_section_heading(p)
            if heading:
                current_section = heading
            location = current_section or f"Para {para_idx}"
            for tc_tag, label in tag_map.items():
                for el in p.iter(tc_tag):
                    text = _collect_text_from_element(
                        el, is_deletion=(label == "DELETED")
                    )
                    if not text:
                        continue
                    author = el.get(author_attr) or "Unknown author"
                    if author not in lines_by_author:
                        lines_by_author[author] = []
                        author_order.append(author)
                    lines_by_author[author].append(f'  [{location}] {label}: "{text}"')

        if not author_order:
            return None

        out: list[str] = ["=== DOCUMENT REDLINES ==="]
        for author in author_order:
            out.append(f"\nAuthor: {author}")
            out.extend(lines_by_author[author])
        return "\n".join(out)

    except Exception:
        logger.warning("Failed to extract tracked changes from DOCX")
        return None


def _parse_document_xml(docx_bytes: bytes) -> etree._Element | None:  # pyright: ignore[reportPrivateUsage]
    """Open a DOCX zip and parse word/document.xml. Returns None on failure."""
    src = io.BytesIO(docx_bytes)
    if not zipfile.is_zipfile(src):
        return None
    src.seek(0)
    with zipfile.ZipFile(src, "r") as zin:
        if _DOCUMENT_XML_PATH not in zin.namelist():
            return None
        return etree.fromstring(zin.read(_DOCUMENT_XML_PATH), parser=_SAFE_PARSER)


def _paragraph_text(p: etree._Element) -> str:  # pyright: ignore[reportPrivateUsage]
    """Accepted text of a paragraph (insertions kept, deletions excluded)."""
    text_tag = f"{{{_WORD_NS}}}t"
    return "".join(t.text or "" for t in p.iter(text_tag))


def _detect_section_heading(
    p: etree._Element,  # pyright: ignore[reportPrivateUsage]
) -> str | None:
    """Return a compact section label if the paragraph begins like a heading.

    Returns None when the paragraph is not heading-like, so callers fall back to
    a paragraph index. The label is truncated to the first sentence (or 60
    chars) to keep the redline summary compact.
    """
    text = _paragraph_text(p).strip()
    if not text or not _SECTION_HEADING_RE.match(text):
        return None
    cut = text.find(". ")
    if cut == -1 or cut > 60:
        cut = 60
    return text[:cut].strip()


def _collect_text_from_element(
    el: etree._Element,  # pyright: ignore[reportPrivateUsage]
    is_deletion: bool = False,
) -> str:
    """Collect and coalesce text from w:t or w:delText children of a tracked change element."""
    text_tag = f"{{{_WORD_NS}}}delText" if is_deletion else f"{{{_WORD_NS}}}t"
    parts: list[str] = []
    for t in el.iter(text_tag):
        if t.text:
            parts.append(t.text)
    return "".join(parts)


def _remove_elements(tree: etree._Element, tag: str) -> None:  # pyright: ignore[reportPrivateUsage]
    """Remove all elements matching *tag* from the tree."""
    for el in list(tree.iter(tag)):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)


def _unwrap_elements(tree: etree._Element, tag: str) -> None:  # pyright: ignore[reportPrivateUsage]
    """Replace elements matching *tag* with their children (unwrap)."""
    for el in list(tree.iter(tag)):
        parent = el.getparent()
        if parent is None:
            continue
        idx = list(parent).index(el)
        for child in reversed(list(el)):
            parent.insert(idx, child)
        parent.remove(el)
