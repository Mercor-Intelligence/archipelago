"""XML helpers shared by the style-metadata extractors.

Split out of `spreadsheet_xml` because it is not spreadsheet-specific: the
docx, pptx and spreadsheet extractors all emit XML blocks, and the
multi-representation eval wraps them in one. Three of those four had reached
for `spreadsheet_xml._xml_escape` across a module boundary, and the fourth had
grown a byte-identical private copy — which is the usual outcome when a shared
helper lives behind a private name in a format-specific module.
"""


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
