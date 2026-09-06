"""XML helpers shared by the style-metadata extractors.

Split out of `spreadsheet_xml` because it is not spreadsheet-specific: the
docx, pptx and spreadsheet extractors all emit XML blocks, and the
multi-representation eval wraps them in one. Three of those four had reached
for `spreadsheet_xml._xml_escape` across a module boundary, and the fourth had
grown a byte-identical private copy — which is the usual outcome when a shared
helper lives behind a private name in a format-specific module.
"""

from docx.oxml.ns import qn
from lxml import etree

# lxml exports its element class privately, so every annotation naming it
# needs its own reportPrivateUsage suppression. One alias covers them all --
# the same convention the docx extractor uses.
type Element = etree._Element  # pyright: ignore[reportPrivateUsage]


def xml_escape(value: str) -> str:
    """Escape a value for XML text or a double-quoted attribute.

    Covers `&`, `<`, `>` and `"`. Single quotes are deliberately not escaped:
    every emitter here quotes attributes with double quotes, so `'` needs no
    encoding and escaping it would only make filenames harder to read in a
    prompt. `&` must be replaced first or the entities inserted afterwards get
    double-escaped.
    """
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# OOXML namespaces, in ONE convention: wrapped in braces, because that is the
# form ElementTree and lxml take in `find`/`iter`. Mixing the two spellings is
# not cosmetic. `_DML_NS` used to be defined twice — bare in the docx
# extractor, braced in the spreadsheet one — and the vendored bundle
# concatenates all three extractors into a single module, so one definition
# silently shadowed the other. Because the values differed, lookups quietly
# returned nothing instead of raising, and it took a real customer workbook to
# notice. One constant, one spelling, one definition.
PKG_RELS_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
OFFICE_RELS_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
WML_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DML_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
PML_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def bare(namespace: str) -> str:
    """A namespace without its braces, for the APIs that want it that way.

    python-docx's `qn` and the `nsmap` arguments take the bare URI, while
    `find`/`iter` take it braced. Deriving one from the other keeps a single
    source for the URI rather than two spellings to drift apart.
    """
    return namespace.strip("{}")


def nearest_ancestor(element: "Element", tag: str) -> "Element | None":
    """The closest ancestor of `element` whose tag is `tag`, or None.

    Starts at the parent, so an element is never its own ancestor. Written out
    three times in forty lines of the docx extractor before this existed —
    once for `w:tc`, twice for `w:tbl` — and each copy had to be mirrored into
    the vendored bundle separately.

    `tag` is the prefixed name (`"w:tbl"`), resolved through `qn` here so
    callers do not each import it.
    """
    node = element.getparent()
    qualified = qn(tag)
    while node is not None and node.tag != qualified:
        node = node.getparent()
    return node
