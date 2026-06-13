"""
Utilities for handling OOXML tracked changes in DOCX files.

Two complementary operations:
1. accept_tracked_changes_in_docx — strips tracked-change markup so extractors
   see only the accepted document content.
2. extract_tracked_changes_as_text — converts tracked-change markup into a
   human-readable text summary for LLM judges that need to see redlines.
"""

import io
import zipfile

import lxml.etree as etree
from loguru import logger

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DOCUMENT_XML_PATH = "word/document.xml"
_SAFE_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)


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
    elements.  For each, extracts the text content with paragraph context.

    Returns a structured summary like:
        === DOCUMENT REDLINES ===
        [Para 2] DELETED: "old company name"
        [Para 2] INSERTED: "new company name"
        ...

    Returns None if the DOCX has no tracked changes or on any error.
    """
    try:
        tree = _parse_document_xml(docx_bytes)
        if tree is None:
            return None

        changes: list[str] = []
        para_tag = f"{{{_WORD_NS}}}p"
        tag_map = {
            f"{{{_WORD_NS}}}ins": "INSERTED",
            f"{{{_WORD_NS}}}del": "DELETED",
            f"{{{_WORD_NS}}}moveFrom": "MOVED FROM",
            f"{{{_WORD_NS}}}moveTo": "MOVED TO",
        }

        para_idx = 0
        for p in tree.iter(para_tag):
            para_idx += 1
            for tc_tag, label in tag_map.items():
                for el in p.iter(tc_tag):
                    text = _collect_text_from_element(
                        el, is_deletion=(label == "DELETED")
                    )
                    if text:
                        changes.append(f'[Para {para_idx}] {label}: "{text}"')

        if not changes:
            return None

        return "=== DOCUMENT REDLINES ===\n\n" + "\n".join(changes)

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
