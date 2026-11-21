"""The fonts, colours and print-standard claims of a PDF page."""

from __future__ import annotations

import contextlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
from numpy.typing import NDArray
from pypdf import PageObject, PdfReader
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

# pypdf parses a whole stream in pure Python before any command is seen, so a page reads this many bytes of it.
MAX_CONTENT_BYTES = 4_000_000
_DEADLINE_EVERY = 5_000
_MAX_FORM_DEPTH = 8
_MAX_COLOURS = 40
_MAX_FONT_COLOURS = 4
_MAX_RENDERED = 12
_MIN_RENDERED_SHARE = 0.01

_FILL_OPS = {b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"}
_STROKE_OPS = {b"S", b"s", b"B", b"B*", b"b", b"b*"}
_SHOW_OPS = {b"Tj", b"TJ", b"'", b'"'}
_DEVICE_SPACES = {"/DeviceGray": "gray", "/DeviceRGB": "rgb", "/DeviceCMYK": "cmyk"}
_NAMED_SPACES = {*_DEVICE_SPACES, "/Pattern"}
_ICC_SPACES = {1: "gray", 3: "rgb", 4: "cmyk"}
_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
_INLINE_IMAGE_START = re.compile(rb"(?:^|(?<=\s))BI(?=\s)")
# EI follows whitespace or a filter's end mark: > (ASCII85, ASCIIHex), 0x80 (RunLength), FF D9 (JPEG).
_INLINE_IMAGE_END = re.compile(rb"(?:(?<=[\s>\x80])|(?<=\xff\xd9))EI(?=\s|$)")
_XMP_CLAIMS = (
    ("pdfxid:GTS_PDFXVersion", "PDF/X version"),
    ("pdfx:GTS_PDFXVersion", "PDF/X version"),
    ("pdfx:GTS_PDFXConformance", "PDF/X conformance"),
    ("pdfaid:part", "PDF/A part"),
    ("pdfaid:conformance", "PDF/A conformance"),
)

Scale = tuple[float, float, float, float]
_IDENTITY: Scale = (1.0, 0.0, 0.0, 1.0)


def _scale(nums: list[float]) -> Scale:
    return (nums[0], nums[1], nums[2], nums[3])


def _mul(m: Scale, n: Scale) -> Scale:
    """The 2x2 part of PDF's row-vector product m x n; translation never changes a size."""
    a, b, c, d = m
    a2, b2, c2, d2 = n
    return (a * a2 + b * c2, a * b2 + b * d2, c * a2 + d * c2, c * b2 + d * d2)


@dataclass(frozen=True)
class _Space:
    kind: str
    names: tuple[str, ...] = ()
    icc: bool = False


@dataclass(frozen=True)
class _State:
    ctm: Scale = _IDENTITY
    tm: Scale = _IDENTITY
    fill_space: _Space = _Space("gray")
    stroke_space: _Space = _Space("gray")
    fill: str = "Gray 0.00 (#000000)"
    stroke: str = "Gray 0.00 (#000000)"
    font: str = ""
    size: float = 0.0
    render_mode: int = 0


@dataclass
class _FontUse:
    facts: str
    sizes: Counter[float] = field(default_factory=Counter)
    colours: Counter[str] = field(default_factory=Counter)


@dataclass
class _Walk:
    unit: float
    deadline: float
    colours: dict[str, Counter[str]] = field(default_factory=dict)
    fonts: dict[str, _FontUse] = field(default_factory=dict)
    content_bytes: int = 0
    operations: int = 0
    cut_short: str = ""
    # Set by the byte cap; unlike cut_short it does not stop commands already within budget.
    truncated: str = ""
    unreadable: list[str] = field(default_factory=list)

    def count(self, colour: str, use: str) -> None:
        self.colours.setdefault(colour, Counter())[use] += 1


def _numbers(operands: list[Any]) -> list[float]:
    out = []
    for value in operands:
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out


def _pct(values: list[float]) -> str:
    return "/".join(f"{round(min(max(v, 0.0), 1.0) * 100, 1):g}" for v in values)


def _hex(values: list[float]) -> str:
    return "".join(f"{round(min(max(v, 0.0), 1.0) * 255):02X}" for v in values)


def _colour(space: _Space, values: list[float]) -> str:
    """One colour as the file states it; RGB and gray also get a hex value."""
    icc = "ICC " if space.icc else ""
    if space.kind == "gray" and len(values) >= 1:
        return f"{icc}Gray {values[0]:.2f} (#{_hex(values[:1] * 3)})"
    if space.kind == "rgb" and len(values) >= 3:
        return f"{icc}RGB #{_hex(values[:3])}"
    if space.kind == "cmyk" and len(values) >= 4:
        return f"{icc}CMYK {_pct(values[:4])}"
    if space.kind == "spot" and values:
        return f"Spot colour '{space.names[0]}' at {_pct(values[:1])}%"
    if space.kind == "devicen":
        return f"DeviceN ({', '.join(space.names)}) {_pct(values)}"
    if space.kind == "pattern":
        return "Pattern or gradient"
    return f"{space.kind} {' '.join(f'{v:g}' for v in values)}".strip()


def _initial(space: _Space) -> str:
    """The colour that selecting ``space`` sets before any value is given."""
    start = {"gray": [0.0], "rgb": [0.0, 0.0, 0.0], "cmyk": [0.0, 0.0, 0.0, 1.0]}
    if space.kind in start:
        return _colour(space, start[space.kind])
    return _colour(space, [1.0] * max(len(space.names), 1))


def _name(value: Any) -> str:
    return str(value).lstrip("/")


def _space(value: Any, resources: DictionaryObject) -> _Space:
    if isinstance(value, NameObject) and value not in _NAMED_SPACES:
        spaces = resources.get("/ColorSpace")
        spaces = spaces.get_object() if spaces is not None else {}
        if value in spaces:
            value = spaces[value].get_object()
    if isinstance(value, NameObject):
        if value == "/Pattern":
            return _Space("pattern")
        return _Space(_DEVICE_SPACES.get(value, _name(value)))
    if not isinstance(value, ArrayObject) or not value:
        return _Space("unknown")
    family = value[0]
    if family == "/Separation" and len(value) > 1:
        return _Space("spot", (_name(value[1]),))
    if family == "/DeviceN" and len(value) > 1:
        return _Space("devicen", tuple(_name(n) for n in value[1].get_object()))
    if family == "/ICCBased" and len(value) > 1:
        channels = int(value[1].get_object().get("/N", 0))
        return _Space(_ICC_SPACES.get(channels, f"ICC {channels}-channel"), icc=True)
    if family == "/Pattern":
        return _Space("pattern")
    return _Space(_name(family))


def _font_facts(font: Any) -> str:
    font = font.get_object()
    base = _name(font.get("/BaseFont", "")) or "(unnamed)"
    subtype = _name(font.get("/Subtype", ""))
    if subtype == "Type3":
        return f"{base}, Type 3, glyphs drawn in the file"
    described = font
    if subtype == "Type0" and font.get("/DescendantFonts"):
        described = font["/DescendantFonts"].get_object()[0].get_object()
        subtype = f"Type0 ({_name(described.get('/Subtype', ''))})"
    descriptor = described.get("/FontDescriptor")
    descriptor = descriptor.get_object() if descriptor is not None else {}
    embedded = any(k in descriptor for k in ("/FontFile", "/FontFile2", "/FontFile3"))
    if not embedded:
        how = "not embedded"
    elif _SUBSET_PREFIX.match(base):
        how = "embedded as a subset"
    else:
        how = "embedded in full"
    return f"{_SUBSET_PREFIX.sub('', base)}, {subtype}, {how}"


def _walk(
    stream: Any,
    reader: PdfReader,
    resources: DictionaryObject,
    state: _State,
    walk: _Walk,
    depth: int,
) -> None:
    data = stream.get_data()
    room = MAX_CONTENT_BYTES - walk.content_bytes
    cut = ""
    if len(data) > room:
        cut = f"the page's drawing commands pass {MAX_CONTENT_BYTES:,} bytes"
        data = _whole_commands(data, room)
    walk.content_bytes += len(data)
    if time.monotonic() >= walk.deadline:
        walk.cut_short = "measuring ran out of time"
        return
    stack: list[_State] = []
    for operands, op in _operations(data, reader, cut=bool(cut)):
        walk.operations += 1
        if walk.operations % _DEADLINE_EVERY == 0 and time.monotonic() >= walk.deadline:
            walk.cut_short = "measuring ran out of time"
            return
        nums = _numbers(operands)
        if op == b"q":
            stack.append(state)
        elif op == b"Q" and stack:
            state = stack.pop()
        elif op == b"cm" and len(nums) == 6:
            state = replace(state, ctm=_mul(_scale(nums), state.ctm))
        elif op == b"BT":
            state = replace(state, tm=_IDENTITY)
        elif op == b"Tm" and len(nums) == 6:
            state = replace(state, tm=_scale(nums))
        elif op == b"Tr" and nums:
            state = replace(state, render_mode=int(nums[0]) % 4)
        elif op == b"Tf" and len(operands) == 2 and nums:
            fonts = resources.get("/Font")
            fonts = fonts.get_object() if fonts is not None else {}
            font = fonts.get(operands[0])
            facts = _font_facts(font) if font is not None else _name(operands[0])
            state = replace(state, font=facts, size=nums[-1])
        elif op in (b"g", b"G", b"rg", b"RG", b"k", b"K"):
            space = _Space({b"g": "gray", b"rg": "rgb", b"k": "cmyk"}[op.lower()])
            colour = _colour(space, nums)
            state = (
                replace(state, fill_space=space, fill=colour)
                if op.islower()
                else replace(state, stroke_space=space, stroke=colour)
            )
        elif op in (b"cs", b"CS") and operands:
            space = _space(operands[0], resources)
            state = (
                replace(state, fill_space=space, fill=_initial(space))
                if op == b"cs"
                else replace(state, stroke_space=space, stroke=_initial(space))
            )
        elif op in (b"sc", b"scn"):
            state = replace(state, fill=_colour(state.fill_space, nums))
        elif op in (b"SC", b"SCN"):
            state = replace(state, stroke=_colour(state.stroke_space, nums))
        elif op == b"sh":
            walk.count("Pattern or gradient", "gradient")
        elif op in _FILL_OPS or op in _STROKE_OPS:
            if op in _FILL_OPS:
                walk.count(state.fill, "fill")
            if op in _STROKE_OPS:
                walk.count(state.stroke, "stroke")
        elif op in _SHOW_OPS:
            _show(state, walk)
        elif op == b"Do" and operands and depth < _MAX_FORM_DEPTH:
            _form(operands[0], reader, resources, state, walk, depth)
        if walk.cut_short:
            return
    if cut:
        walk.truncated = cut


def _whole_commands(data: bytes, limit: int) -> bytes:
    """``data`` cut to at most ``limit`` bytes at a line break, else a space, never inside an inline image."""
    limit = max(limit, 0)
    done = 0
    # Images are skipped whole, since their bytes can hold line breaks, BI and EI.
    while start := _INLINE_IMAGE_START.search(data, done, limit):
        end = _INLINE_IMAGE_END.search(data, start.end(), limit + 1)
        if end is None or end.end() > limit:
            return data[: start.start()]
        done = end.end()
    head = data[:limit]
    cut = max(head.rfind(b"\n", done), head.rfind(b"\r", done))
    return head[: max(cut if cut >= 0 else head.rfind(b" ", done), done)]


def _operations(data: bytes, reader: PdfReader, *, cut: bool) -> list[Any]:
    """pypdf's operations for ``data``; a cut it cannot parse falls back to the commands before the first inline image, then to their whole lines."""
    first = _INLINE_IMAGE_START.search(data)
    head = data if first is None else data[: first.start()]
    line = head[: max(head.rfind(b"\n"), head.rfind(b"\r"), 0)]
    *fallbacks, last = dict.fromkeys((data, head, line) if cut else (data,))

    def parse(stream: bytes) -> list[Any]:
        part = DecodedStreamObject()
        part.set_data(stream)
        return ContentStream(part, reader).operations

    # pypdf decides where an image ends, so only it can say a cut split one.
    for attempt in fallbacks:
        with contextlib.suppress(Exception):
            return parse(attempt)
    return parse(last)


def _show(state: _State, walk: _Walk) -> None:
    if state.render_mode == 3:
        return
    _, _, c, d = _mul(state.tm, state.ctm)
    size = round(abs(state.size) * math.hypot(c, d) * walk.unit, 1)
    use = walk.fonts.setdefault(state.font, _FontUse(state.font))
    use.sizes[size] += 1
    if state.render_mode in (0, 2):
        use.colours[state.fill] += 1
        walk.count(state.fill, "text")
    if state.render_mode in (1, 2):
        use.colours[state.stroke] += 1
        walk.count(state.stroke, "text outline")


def _form(
    name: Any,
    reader: PdfReader,
    resources: DictionaryObject,
    state: _State,
    walk: _Walk,
    depth: int,
) -> None:
    xobjects = resources.get("/XObject")
    xobjects = xobjects.get_object() if xobjects is not None else {}
    form = xobjects.get(name)
    form = form.get_object() if form is not None else None
    if form is None or form.get("/Subtype") != "/Form":
        return
    matrix = _numbers(list(form.get("/Matrix", [1, 0, 0, 1, 0, 0])))
    if len(matrix) == 6:
        state = replace(state, ctm=_mul(_scale(matrix), state.ctm))
    inner = form.get("/Resources")
    inner = inner.get_object() if inner is not None else resources
    try:
        _walk(form, reader, inner, state, walk, depth + 1)
    except Exception as e:  # noqa: BLE001, one bad form must not hide the page
        walk.unreadable.append(f"{_name(name)}: {e}")


def drawing_lines(
    pdf_page: PageObject, reader: PdfReader, deadline: float
) -> list[str]:
    """The page's fonts with the sizes they are drawn at, and every colour its drawing commands use."""
    walk = _Walk(unit=float(pdf_page.user_unit), deadline=deadline)
    contents = pdf_page.get_contents()
    resources = pdf_page.get("/Resources")
    resources = resources.get_object() if resources is not None else DictionaryObject()
    if contents is not None:
        _walk(contents, reader, resources, _State(), walk, 0)
    lines = [
        "Fonts (each with the sizes, in points, and colours its text is drawn in):"
    ]
    for use in sorted(walk.fonts.values(), key=lambda u: -sum(u.sizes.values())):
        sizes = ", ".join(f"{s:g}" for s in sorted(use.sizes))
        colours = [c for c, _ in use.colours.most_common(_MAX_FONT_COLOURS)]
        more = len(use.colours) - len(colours)
        tail = f" and {more} more" if more > 0 else ""
        lines.append(
            f"  {use.facts or 'No font set'}. Sizes {sizes} pt. "
            f"Colours {'; '.join(colours) or 'none (outline only)'}{tail}."
        )
    if not walk.fonts:
        lines.append("  None. Text drawn as outlines or images has no font.")
    lines.append(
        "Colours the page's drawing commands use (exact values from the file, "
        "with how often each fills, strokes or colours text; colours inside "
        "images are not listed here):"
    )
    ranked = sorted(walk.colours.items(), key=lambda kv: -sum(kv[1].values()))
    for colour, uses in ranked[:_MAX_COLOURS]:
        counts = ", ".join(f"{use} x{n}" for use, n in uses.most_common())
        lines.append(f"  {colour}: {counts}")
    if len(ranked) > _MAX_COLOURS:
        lines.append(f"  and {len(ranked) - _MAX_COLOURS} more, each used less often.")
    if not ranked:
        lines.append("  None.")
    if walk.cut_short or walk.truncated:
        lines.append(
            f"  These lists stop early because {walk.cut_short or walk.truncated}, "
            "so they may be incomplete."
        )
    for problem in walk.unreadable:
        lines.append(f"  A placed object could not be read ({problem}).")
    return lines


def rendered_colour_lines(rgb: NDArray[np.uint8], dpi: int) -> list[str]:
    """The colours that cover most of the rendered page, with their share of its area."""
    pixels = rgb.reshape(-1, 3).astype(np.uint32)
    packed = (pixels[:, 0] << 16) | (pixels[:, 1] << 8) | pixels[:, 2]
    values, counts = np.unique(packed, return_counts=True)
    order = np.argsort(-counts)
    total = int(packed.size)
    lines = [
        f"Colours as rendered at {dpi} dpi in RGB, by share of the page's area "
        "(poppler converts CMYK and spot colours to RGB its own way, so the "
        "values above are the exact ones):"
    ]
    shown = 0
    covered = 0
    for i in order[:_MAX_RENDERED]:
        share = counts[i] / total
        if share < _MIN_RENDERED_SHARE:
            break
        lines.append(f"  #{int(values[i]):06X} {share * 100:.1f}%")
        shown += 1
        covered += int(counts[i])
    rest = 1 - covered / total if total else 0.0
    if not shown:
        lines.append("  No single colour covers 1% of the page.")
    if rest >= 0.005:
        lines.append(
            f"  The other {rest * 100:.1f}% is many colours that each cover less "
            "(photos, gradients, the soft edges of shapes and text)."
        )
    return lines


def standard_lines(reader: PdfReader) -> list[str]:
    """The print standard and output intent the document declares; a claim, not a validation."""
    lines = [
        "Print standard (what the document declares about itself; nothing here "
        "checks that the file meets it):",
        f"  PDF version {reader.pdf_header.removeprefix('%PDF-')}.",
    ]
    claimed = False
    info = reader.metadata or {}
    for key in ("/GTS_PDFXVersion", "/GTS_PDFXConformance", "/Trapped"):
        if key in info:
            lines.append(f"  Info {key[1:]}: {_name(info[key])}.")
            claimed = claimed or key != "/Trapped"
    catalog: Any = reader.trailer["/Root"].get_object()
    metadata = catalog.get("/Metadata")
    xmp = metadata.get_object().get_data().decode(errors="replace") if metadata else ""
    for tag, label in _XMP_CLAIMS:
        found = re.search(rf'{re.escape(tag)}\s*(?:=\s*"([^"]*)"|>([^<]*)<)', xmp)
        if found:
            lines.append(f"  XMP {label}: {found.group(1) or found.group(2)}.")
            claimed = True
    intents = catalog.get("/OutputIntents")
    for intent in intents.get_object() if intents is not None else []:
        intent = intent.get_object()
        parts = [
            f"{label} {intent[key]!s}"
            for key, label in (
                ("/OutputConditionIdentifier", "condition"),
                ("/OutputCondition", "described as"),
                ("/RegistryName", "registry"),
                ("/Info", "info"),
            )
            if key in intent
        ]
        profile = intent.get("/DestOutputProfile")
        if profile is not None:
            channels = int(profile.get_object().get("/N", 0))
            parts.append(f"{channels}-channel ICC profile embedded")
        else:
            parts.append("no ICC profile embedded")
        lines.append(
            f"  Output intent {_name(intent.get('/S', ''))}: {', '.join(parts)}."
        )
        claimed = True
    if not claimed:
        lines.append("  No PDF/X or PDF/A declaration and no output intent.")
    return lines
