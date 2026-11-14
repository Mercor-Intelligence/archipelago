"""Font-encoding normalisation applied before pypdf text extraction.

pypdf resolves ``/WinAnsiEncoding`` from a table that leaves a handful of codes
undefined and returns the raw code point for them, so a bullet written at code
0x7F comes back as U+007F instead of U+2022. `apply_winansi_bullet_mapping`
spells the missing part of the encoding out explicitly so pypdf resolves it
natively.
"""

from __future__ import annotations

from pypdf import PageObject
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    NumberObject,
    PdfObject,
)

# PDF 32000-1:2008 Annex D.2, note 3 to the Latin-character-set table:
#
#   "In WinAnsiEncoding, all unused codes greater than 40 map to the bullet
#    character. However, only code 225 shall be specifically assigned to the
#    bullet character; other codes are subject to future reassignment."
#
# (Codes there are octal: 40 is 0x20, 225 is 0x95.) These six are the codes
# WinAnsiEncoding leaves unused above 0x20. pypdf maps 0x95 but returns the raw
# code point for these; MuPDF, poppler and reportlab all read them as a bullet,
# and reportlab -- which this server uses to *write* PDFs -- emits bullets at
# 0x7F, so create_pdf output is affected as much as third-party documents.
WINANSI_UNASSIGNED_BULLET_CODES = (0x7F, 0x81, 0x8D, 0x8F, 0x90, 0x9D)


def _resolve(value: PdfObject | None) -> PdfObject | None:
    return value.get_object() if isinstance(value, IndirectObject) else value


def _mapped_codes(differences: ArrayObject) -> set[int]:
    """Every code a /Differences array assigns, including implied ones.

    Per PDF 32000-1:2008 9.6.6.1 a number starts a run and each glyph name after
    it takes the next consecutive code, so ``[126 /asciitilde /dagger]`` assigns
    126 *and* 127 while only 126 appears as a number. Collecting the numbers
    alone would miss 127 and let us append a later, overriding entry for it.
    """
    codes: set[int] = set()
    code: int | None = None
    for entry in differences:
        if isinstance(entry, NameObject):
            if code is not None:
                codes.add(code)
                code += 1
        elif isinstance(entry, (int, float)) and not isinstance(entry, bool):
            code = int(entry)
    return codes


def _normalise_font(font: DictionaryObject) -> None:
    encoding = _resolve(font.get("/Encoding"))

    if isinstance(encoding, DictionaryObject):
        if encoding.get("/BaseEncoding") != "/WinAnsiEncoding":
            return
        differences = ArrayObject(list(encoding.get("/Differences") or []))
        # A code the document maps itself keeps that mapping.
        already_mapped = _mapped_codes(differences)
    elif encoding == "/WinAnsiEncoding":
        differences = ArrayObject()
        already_mapped = set()
        encoding = DictionaryObject()
        encoding[NameObject("/Type")] = NameObject("/Encoding")
        encoding[NameObject("/BaseEncoding")] = NameObject("/WinAnsiEncoding")
        font[NameObject("/Encoding")] = encoding
    else:
        return

    missing = [
        code for code in WINANSI_UNASSIGNED_BULLET_CODES if code not in already_mapped
    ]
    if not missing:
        return
    for code in missing:
        differences.append(NumberObject(code))
        differences.append(NameObject("/bullet"))
    encoding[NameObject("/Differences")] = differences


def apply_winansi_bullet_mapping(page: PageObject) -> None:
    """Make a page's WinAnsiEncoding fonts resolve unassigned codes to a bullet.

    Mutates the in-memory font dictionaries reached from the page's resources;
    nothing is written back to the document. Safe to call more than once on the
    same page, and on pages whose fonts are shared with other pages.

    Only fonts that declare WinAnsiEncoding are touched, and only for codes the
    font does not already map through its own /Differences, so a document that
    states a different meaning for those codes keeps it.

    Args:
        page: A page whose text is about to be extracted with pypdf.
    """
    resources = _resolve(page.get_inherited("/Resources"))
    if not isinstance(resources, DictionaryObject):
        return

    fonts = _resolve(resources.get("/Font"))
    if not isinstance(fonts, DictionaryObject):
        return

    for font_ref in fonts.values():
        font = _resolve(font_ref)
        if isinstance(font, DictionaryObject):
            _normalise_font(font)
