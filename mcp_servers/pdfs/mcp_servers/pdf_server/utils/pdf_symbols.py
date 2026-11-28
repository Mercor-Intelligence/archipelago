"""The character a symbol font draws, recovered from the glyph the page paints.

ZapfDingbats names its glyphs ``a1``..``a191``: Adobe's encoding puts the check
mark ``a19`` at code 0x33 and the ballot X ``a23`` at 0x37, which is what a
compliance certificate ticks its boxes with. MuPDF draws both correctly but has
no Unicode for those names -- its glyph list is the standard AGL, which does
not carry them -- so ``get_text`` falls back to reading the digits of ``a19`` as
a character code and returns U+0013. Twenty-one of ZapfDingbats' codes come
back as control characters that way, the checks, crosses and X's among them,
and U+0013 reads the same whether the box held a check or a cross.

Nothing is missing from the page: it drew the tick, so MuPDF did resolve the
code to a glyph. ``Page.get_texttrace`` reports that glyph's id, and the font it
was rendered with maps the id back to the character a reader sees (U+2713).
That makes this a recovery rather than a guess, but only while the id is looked
up in the font MuPDF actually drew with -- the document's own embedded program,
or the standard-14 font it names. A page that draws one code two different
ways, or that names a font we cannot load, keeps its control characters so the
caller can report them instead of inventing a symbol for them.
"""

from __future__ import annotations

import re
from typing import Any, cast

import fitz  # PyMuPDF
from loguru import logger

# Unicode's Cc category -- U+0000..U+001F and U+007F..U+009F -- less the five
# the extractor produces as its own line and column layout rather than from any
# glyph on the page. Matched by a compiled scan because every page's text is
# tested against it before anything else happens.
UNNAMED = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")

# PDF 32000-1:2008 9.6.2.2: the 14 fonts a viewer supplies for itself. MuPDF
# draws these from its own built-in copies, which are the ones ``fitz.Font``
# loads under the same names, so a glyph id resolved against them is the id
# that was drawn. Any other unembedded font is substituted by a fallback we
# cannot identify, and its ids would mean nothing here.
STANDARD_14 = frozenset(
    {
        "Courier",
        "Courier-Bold",
        "Courier-Oblique",
        "Courier-BoldOblique",
        "Helvetica",
        "Helvetica-Bold",
        "Helvetica-Oblique",
        "Helvetica-BoldOblique",
        "Times-Roman",
        "Times-Bold",
        "Times-Italic",
        "Times-BoldItalic",
        "Symbol",
        "ZapfDingbats",
    }
)


def is_unnamed(char: str) -> bool:
    """Is `char` a character no page could be showing?

    A control character is never a glyph a document means to show, so one that
    survives extraction stands for a glyph whose name the extractor could not
    turn into a character.

    This is also what keeps a document's own claim about a code: a /ToUnicode
    or /Differences entry that names a real character leaves nothing here to
    recover, and only a mapping asserting a control character -- which asserts
    nothing a reader can see -- is overridden.
    """
    return UNNAMED.fullmatch(char) is not None


def unnamed_count(text: str) -> int:
    """How many characters in `text` stand for a glyph no font could name."""
    return len(UNNAMED.findall(text))


def _base_name(font_name: str) -> str:
    """A font's name without the subset prefix of PDF 32000-1:2008 9.6.4.

    ``get_texttrace`` reports the name a font was loaded under and
    ``get_fonts`` reports its ``/BaseFont``; for a subset the second carries a
    ``ABCDEF+`` tag the first does not, and the two have to be joined by name.
    """
    if len(font_name) > 7 and font_name[6] == "+":
        return font_name[7:]
    return font_name


def _font_program(page: fitz.Page, name: str) -> fitz.Font | None:
    """The font MuPDF drew `name` with, or None if it cannot be identified.

    Only two cases are identifiable, and both are exact: a font the document
    embeds, where the program itself is in the file, and one of the standard 14,
    where the viewer supplies a copy PyMuPDF loads by the same name.
    """
    entries = [
        entry
        for entry in page.get_fonts(full=True)
        if _base_name(str(entry[3])) == name
    ]
    if not entries or page.parent is None:
        return None
    # Two fonts answering to one name leaves no way to say which drew a glyph
    # -- unless neither carries a program of its own. A standard-14 name every
    # entry leaves unembedded is drawn from MuPDF's one built-in copy whatever
    # encoding each entry declares, so a page font and the AcroForm's own
    # ``/ZaDb`` (which every form with a check box carries) are one program.
    if len(entries) != 1:
        if name in STANDARD_14 and not any(
            page.parent.extract_font(int(cast(Any, entry)[0]))[3] for entry in entries
        ):
            return fitz.Font(fontname=name)
        return None
    xref = int(cast(Any, entries[0])[0])
    try:
        buffer = page.parent.extract_font(xref)[3]
        if buffer:
            return fitz.Font(fontbuffer=buffer)
        if name in STANDARD_14:
            return fitz.Font(fontname=name)
    except Exception as exc:
        logger.debug(f"pdf_symbols: font {name!r} (xref {xref}) not loadable: {exc!r}")
    return None


def _characters_by_glyph(font: fitz.Font) -> dict[int, str]:
    """Every character the font can draw, keyed by the glyph id it draws it as.

    ``valid_codepoints`` is ascending, so where several characters share a
    glyph the lowest wins -- the encoded character rather than the private-use
    alias fonts tend to add above it.
    """
    characters: dict[int, str] = {}
    for code in font.valid_codepoints():
        char = chr(code)
        if is_unnamed(char):
            continue
        glyph = font.has_glyph(code)
        if glyph and glyph not in characters:
            characters[glyph] = char
    return characters


def _page_mapping(page: fitz.Page) -> dict[int, str]:
    """``{character code: the character its glyph stands for}`` for this page.

    A code is mapped only when every glyph the page draws it as resolves, and
    resolves to the same character. One drawn two ways is left alone: the text
    carries no record of which occurrence was which, so either choice would be
    wrong for the other.
    """
    # Nothing marks a glyph no font could name, so it is carried as the empty
    # string: a code that collects one keeps its control character.
    drawn: dict[int, set[str]] = {}
    tables: dict[str, dict[int, str] | None] = {}

    for span in page.get_texttrace():
        name = _base_name(str(cast(Any, span)["font"]))
        for char in cast(Any, span)["chars"]:
            code, glyph = int(char[0]), int(char[1])
            if not 0 <= code <= 0x10FFFF or not is_unnamed(chr(code)):
                continue
            if name not in tables:
                font = _font_program(page, name)
                tables[name] = _characters_by_glyph(font) if font is not None else None
            table = tables[name]
            drawn.setdefault(code, set()).add((table.get(glyph) or "") if table else "")

    return {
        code: characters.pop()
        for code, characters in drawn.items()
        if len(characters) == 1 and "" not in characters
    }


def drawn_characters(page: fitz.Page) -> dict[tuple[str, int, str], str]:
    """The character each drawn glyph stands for, where extraction disagrees.

    Keyed on ``(font, glyph id, the character extraction returned)``, which is
    what separates this from the page mapping above. That one is keyed on the
    character code alone and so has to leave a code the page draws two ways
    untouched -- and a *printable* substitution is always drawn two ways,
    because the character the AGL fallback invents is one the page also uses as
    itself. ZapfDingbats' black square ``a73`` is the case this was reported
    from: its digits read as code 73, so MuPDF returns ``I`` for every selected
    box on the page, and the same pages spell ``CONFIDENTIAL`` and ``INC.``
    with a real ``I``. The glyph tells the two apart, so the square is
    recoverable where the bare code is not.

    The font has to be part of that key rather than merely the thing consulted,
    because a glyph id means nothing outside the font it indexes. A subset font
    numbers its glyphs by what the document happened to use, so two fonts on
    one page share ids freely -- and then one page's genuine ``(79, 'I')``
    would settle the pair for the other's square, in whichever order the page
    drew them: the square left as ``I`` if the letter came first, the letter
    rewritten as the square if it did not.

    A pair is reported only when the font MuPDF drew with does not draw the
    extracted character with that glyph *and* does draw some other character
    with it -- the same "recovery, not a guess" bound the module header states.
    A font that cannot be identified, a glyph that names no character, and an
    extracted character the font confirms are all left exactly as extracted.

    Args:
        page: The PyMuPDF page whose glyphs are in question.

    Returns:
        ``{(font, glyph id, extracted character): the character it draws}``,
        the font named exactly as ``get_texttrace`` reports it so a caller
        walking the same trace can look a glyph up without renormalising.
        Empty when the page draws nothing extraction got wrong.
    """
    programs: dict[str, fitz.Font | None] = {}
    tables: dict[str, dict[int, str]] = {}
    drawn: dict[tuple[str, int, str], str] = {}
    settled: set[tuple[str, int, str]] = set()

    for span in page.get_texttrace():
        reported = str(cast(Any, span)["font"])
        name = _base_name(reported)
        if name not in programs:
            programs[name] = _font_program(page, name)
        program = programs[name]
        if program is None:
            continue
        for char in cast(Any, span)["chars"]:
            code, glyph = int(char[0]), int(char[1])
            if not 0 <= code <= 0x10FFFF:
                continue
            extracted = chr(code)
            key = (reported, glyph, extracted)
            if key in settled or extracted.isspace():
                continue
            settled.add(key)
            # The font drawing this glyph for this character is extraction
            # agreeing with the page, which is the ordinary case and the one
            # that must never be rewritten.
            if program.has_glyph(code) == glyph:
                continue
            if name not in tables:
                tables[name] = _characters_by_glyph(program)
            real = tables[name].get(glyph)
            if real is not None and real != extracted:
                drawn[key] = real
    return drawn


def recover_symbol_characters(page: fitz.Page, text: str) -> tuple[str, int]:
    """`text` with each character code replaced by the character its glyph draws.

    Returns the recovered text together with how many characters are left in it
    that the page draws but no font on it could name, so a caller can report
    them rather than pass them off as text. Each replacement is one character
    for one, so positions already measured against `text` still hold.
    """
    if not UNNAMED.search(text):
        return text, 0
    mapping = _page_mapping(page)
    recovered = text.translate(mapping) if mapping else text
    return recovered, unnamed_count(recovered)


def recover_drawn_characters(page: fitz.Page, text: str) -> str:
    """`text` with each glyph ``drawn_characters`` recovers put back in place.

    `text` must be ``page.get_text("text")``: its characters are tied to their
    glyphs through the same extraction in ``rawdict`` form, which lists them in
    the same order with their origins, and the origin is what ties one back to
    the glyph id ``get_texttrace`` reports. That is what lets a printable
    substitution be undone where the page also uses the character as itself --
    ZapfDingbats' ``a73`` read as ``I`` beside a real ``I`` -- which a mapping
    keyed on the character code alone cannot do.

    Each replacement is one character for one, so lengths and offsets measured
    against `text` still hold. If the two extractions ever disagree about the
    characters in between, nothing is replaced: a position that cannot be tied
    to its glyph is left as extracted rather than guessed at.
    """
    drawn = drawn_characters(page)
    if not drawn:
        return text

    glyph_at: dict[tuple[str, int, int, str], tuple[str, int]] = {}
    for span in page.get_texttrace():
        reported = str(cast(Any, span)["font"])
        for char in cast(Any, span)["chars"]:
            code = int(char[0])
            if not 0 <= code <= 0x10FFFF:
                continue
            x, y = char[2]
            key = (_base_name(reported), round(x * 100), round(y * 100), chr(code))
            glyph_at[key] = (reported, int(char[1]))

    sequence: list[tuple[str, str | None]] = []
    for block in cast(Any, page.get_text("rawdict")).get("blocks", ()):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", ()):
            for span in line.get("spans", ()):
                name = _base_name(str(span.get("font", "")))
                for char in span.get("chars", ()):
                    extracted = str(char.get("c", ""))
                    if not extracted or extracted.isspace():
                        continue
                    x, y = char["origin"]
                    found = glyph_at.get(
                        (name, round(x * 100), round(y * 100), extracted)
                    )
                    sequence.append(
                        (extracted, drawn.get((*found, extracted)) if found else None)
                    )
    if not any(real for _, real in sequence):
        return text

    out = list(text)
    at = 0
    for extracted, real in sequence:
        while at < len(out) and out[at].isspace():
            at += 1
        if at >= len(out) or out[at] != extracted:
            return text
        if real is not None:
            out[at] = real
        at += 1
    if any(not rest.isspace() for rest in out[at:]):
        return text
    return "".join(out)
