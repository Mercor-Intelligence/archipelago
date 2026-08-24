"""Extract font, color, layout, and shape metadata from a PPTX file.

The dict-returning extractor is a domain-specific utility used by the
pptx_style_verifier eval. pptx_to_style_metadata_output wraps it as a
registered transformation for the generic multi-representation judge, emitting
a bounded per-slide style summary rather than the full dict.
"""

import asyncio
from collections import Counter
from io import BytesIO
from typing import Any

from loguru import logger
from pptx import Presentation
from pptx.util import Emu

from ..models import TransformationOutput
from ..style_metadata_cache import cached_style_text, is_ooxml_package
from ..xml_utils import xml_escape


def _emu_to_pt(emu_val: int | Emu | None) -> float | None:
    """Convert EMU to points. Returns None if input is None."""
    if emu_val is None:
        return None
    return round(int(emu_val) / 12700, 1)


def _rgb_to_hex(rgb: Any) -> str | None:
    """Convert an RGBColor to a hex string like '#RRGGBB'. Returns None if not set."""
    if rgb is None:
        return None
    try:
        return f"#{rgb}"
    except Exception:
        return None


def _theme_color_name(color: Any) -> str | None:
    """A theme colour as "accent1" / "dark1", plus any brightness tweak.

    python-pptx raises "no .rgb property on color type '_SchemeColor'" for a run
    coloured from the theme, so catching AttributeError and moving on left no
    colour information at all — and colouring by theme is what PowerPoint
    templates do by default. The theme slot is the honest answer: the concrete
    RGB lives in the theme part and can be overridden per layout, so naming the
    slot is both correct and stable.

    Brightness carries PowerPoint's "Lighter 40% / Darker 25%" variants, without
    which two visibly different shades collapse to the same slot.
    """
    try:
        theme = color.theme_color
    except Exception:
        return None
    # Falsy, not None: python-pptx returns the MSO_THEME_COLOR.NOT_THEME_COLOR
    # sentinel — an int enum whose value is 0 — for every colour type except
    # scheme colours, so a system, preset, HSL or scRGB colour reached the name
    # formatting below and came out as the invented slot "notthemecolor". Every
    # real member is nonzero (DARK_1=1 … ACCENT_1=5), so `not theme` screens the
    # sentinel and None alike.
    if not theme:
        return None
    # MSO_THEME_COLOR_INDEX.ACCENT_1 -> "accent1"
    name = str(theme).split()[0].lower().replace("_", "")
    try:
        brightness = color.brightness
    except Exception:
        brightness = 0
    if brightness:
        return f"{name}{brightness:+.0%}".replace("%", "pct")
    return name


def _extract_run_style(run: Any) -> dict[str, Any]:
    """Extract style properties from a single text run."""
    font = run.font
    color = font.color
    color_rgb = None
    color_theme = None
    if color is not None:
        try:
            color_rgb = _rgb_to_hex(color.rgb)
        except AttributeError:
            # Not an explicit RGB colour. A theme reference is still real colour
            # information, so resolve the slot rather than dropping it.
            color_theme = _theme_color_name(color)
    return {
        "text": run.text,
        "font_name": font.name,  # None means inherited from slide master
        "font_size_pt": _emu_to_pt(font.size),
        "font_color_rgb": color_rgb,
        # Additive: font_color_rgb keeps its meaning (explicit hex, or None), so
        # existing consumers see a new key rather than a changed value.
        "font_color_theme": color_theme,
        "bold": font.bold,  # None means inherited
        "italic": font.italic,  # None means inherited
    }


def _shape_type_name(shape: Any) -> tuple[str | None, str | None]:
    """(type name, why it could not be read).

    The reason is returned rather than swallowed for two reasons. python-pptx
    also returns None legitimately — for a graphic frame holding SmartArt — so
    collapsing both into None leaves a consumer unable to tell "no type" from
    "could not read the type". And it travels under the same `unreadable` key
    the per-shape guard below uses, so there is one marker for this class of gap
    rather than two for a consumer to reconcile.

    BaseShape.shape_type raises NotImplementedError for any subclass that
    doesn't override it, and Shape.shape_type raises for an sp that is neither
    placeholder, freeform, autoshape nor textbox. Letting that propagate cost
    the *whole* deck's style metadata over one odd shape — the caller catches
    per-file, not per-shape — so a criterion about branded text elsewhere on the
    slide would be graded with no evidence at all.
    """
    try:
        shape_type = shape.shape_type
    except Exception as e:
        return None, f"shape_type: {type(e).__name__}: {e}"
    return (str(shape_type) if shape_type else None), None


def _extract_shape_metadata(shape: Any) -> dict[str, Any]:
    """Extract metadata from a single shape."""
    shape_type, type_unreadable = _shape_type_name(shape)
    result: dict[str, Any] = {
        "shape_name": shape.name,
        "shape_type": shape_type,
        "left": _emu_to_pt(shape.left),
        "top": _emu_to_pt(shape.top),
        "width": _emu_to_pt(shape.width),
        "height": _emu_to_pt(shape.height),
    }
    if type_unreadable:
        # Same key the per-shape guard below uses, so a consumer has one marker
        # for this class of gap rather than two to reconcile.
        result["unreadable"] = type_unreadable

    if shape.has_text_frame:
        paragraphs = []
        for para in shape.text_frame.paragraphs:
            runs = [_extract_run_style(run) for run in para.runs]
            paragraphs.append(
                {
                    "alignment": str(para.alignment) if para.alignment else None,
                    "level": para.level,
                    "text_runs": runs,
                }
            )
        result["paragraphs"] = paragraphs

    return result


# Guard against a malformed deck with a cyclic or absurdly deep group nesting.
_MAX_GROUP_DEPTH = 8


def _flatten_shapes(shapes: Any, depth: int = 0) -> list[Any]:
    """Yield shapes, descending into group shapes.

    slide.shapes lists a GroupShape but not its children, so text nested in a
    group is invisible to a plain iteration. That was tolerable when this
    extractor only fed the dedicated pptx style verifiers, but its output is now
    given to the generic judge as ground truth for formatting criteria — a deck
    that groups its title and body would have had "the deck uses Arial Black"
    failed on absent evidence.

    Used only when a caller opts in, so the older verifiers keep the shape list
    they were written against. See pptx_to_style_metadata.
    """
    flat: list[Any] = []
    for shape in shapes:
        flat.append(shape)
        if depth >= _MAX_GROUP_DEPTH:
            continue
        # shape_type can raise on exotic shapes, so key off the child
        # collection that GroupShape actually exposes.
        children = getattr(shape, "shapes", None)
        if children is not None:
            flat.extend(_flatten_shapes(children, depth + 1))
    return flat


def pptx_to_style_metadata(
    file_bytes: bytes, file_name: str, include_grouped_shapes: bool = False
) -> dict[str, Any]:
    """Extract style metadata from a PPTX file.

    Args:
        file_bytes: Raw .pptx bytes.
        file_name: Filename, included in the output for context.
        include_grouped_shapes: Descend into group shapes. Defaults off because
            two pre-existing evals (pptx_style_verifier and
            pptx_style_verifier_apex_v2) json.dumps this dict wholesale, and
            adding a slide's group children plus the group shapes themselves
            changes any criterion that counts shapes — a 3-shape slide becomes
            5. Only the generic multi-representation judge opts in, where the
            output is a collapsed style summary that no criterion counts.

    Returns:
        {
            "file_name": str,
            "slide_width_pt": float,
            "slide_height_pt": float,
            "slide_count": int,
            "slides": [
                {
                    "index": int,
                    "title": str | None,
                    "layout_name": str,
                    "shapes": [...]
                }
            ]
        }
    """
    prs = Presentation(BytesIO(file_bytes))

    slide_width_pt = _emu_to_pt(prs.slide_width)
    slide_height_pt = _emu_to_pt(prs.slide_height)

    slides_meta: list[dict[str, Any]] = []
    for idx, slide in enumerate(prs.slides):
        # Extract title from the title placeholder if present
        title = None
        if slide.shapes.title and slide.shapes.title.has_text_frame:
            title = slide.shapes.title.text_frame.text

        layout_name = slide.slide_layout.name if slide.slide_layout else None

        # Degrade per shape, not per file. python-pptx raises on several
        # properties of shapes it cannot model, and the caller only catches at
        # file granularity — so one odd shape would otherwise discard every
        # other shape's fonts and colours along with it. The unreadable shape is
        # recorded rather than dropped so a consumer can see the gap.
        raw_shapes = (
            _flatten_shapes(slide.shapes)
            if include_grouped_shapes
            else list(slide.shapes)
        )
        shapes: list[dict[str, Any]] = []
        for shape in raw_shapes:
            try:
                shapes.append(_extract_shape_metadata(shape))
            except Exception as e:
                logger.warning(
                    f"[TRANSFORM] unreadable shape in {file_name}: "
                    f"{type(e).__name__}: {e}"
                )
                shapes.append(
                    {
                        "shape_name": None,
                        "shape_type": None,
                        "unreadable": f"{type(e).__name__}: {e}",
                    }
                )

        slides_meta.append(
            {
                "index": idx,
                "title": title,
                "layout_name": layout_name,
                "shapes": shapes,
            }
        )

    return {
        "file_name": file_name,
        "slide_width_pt": slide_width_pt,
        "slide_height_pt": slide_height_pt,
        "slide_count": len(slides_meta),
        "slides": slides_meta,
    }


# ==========================================================================
# TRANSFORMATION WRAPPER (for the generic multi-representation judge)
# ==========================================================================
# The dict above is consumed directly by pptx_style_verifier, which json.dumps
# it wholesale. That verifier grades one deck per call, so size doesn't matter
# there. The generic judge is different: its style metadata shares a capped
# token budget with everything else in the prompt, and on a spreadsheet an
# unbounded dump cost 45MB and lost 7 of 8 sheets to truncation. So this emits
# a compact per-slide style summary instead — runs collapsed by identical
# style, which is what font/size/colour criteria actually need.

# Distinct style combinations listed per slide before summarising the rest.
_MAX_STYLES_PER_SLIDE = 12

# Characters of sample text kept per style, purely to help the judge tie a
# style to what it applies to.
_SAMPLE_TEXT_CHARS = 60

# Shape names listed per style group. A sample, and said to be one when it is
# truncated — see the shapes_listed attribute.
_MAX_SHAPE_NAMES = 3


def _run_style_key(run: dict[str, Any]) -> tuple[Any, ...]:
    return (
        run.get("font_name"),
        run.get("font_size_pt"),
        run.get("font_color_rgb") or run.get("font_color_theme"),
        run.get("bold"),
        run.get("italic"),
    )


def _style_attrs(key: tuple[Any, ...]) -> str:
    font_name, size, color, bold, italic = key
    attrs: list[str] = []
    # None means "inherited from the slide layout/master" rather than unset,
    # so say that explicitly instead of silently omitting it — otherwise the
    # judge can't tell "not bold" from "not specified here".
    attrs.append(
        f'font_name="{xml_escape(str(font_name))}"'
        if font_name
        else 'font_name="inherited"'
    )
    attrs.append(
        f'font_size_pt="{size}"' if size is not None else 'font_size_pt="inherited"'
    )
    if color:
        # A theme slot is prefixed so the judge cannot mistake it for a hex.
        value = str(color)
        if not value.startswith("#"):
            value = f"theme:{value}"
        attrs.append(f'font_color="{xml_escape(value)}"')
    # Same reasoning as the fonts above, which this previously failed to
    # follow: omitting bold/italic when None left the judge unable to tell
    # inherited-bold from not-bold, on criteria whose subject is exactly that.
    attrs.append(
        f'bold="{str(bool(bold)).lower()}"' if bold is not None else 'bold="inherited"'
    )
    attrs.append(
        f'italic="{str(bool(italic)).lower()}"'
        if italic is not None
        else 'italic="inherited"'
    )
    return " ".join(attrs)


def _pptx_style_summary_xml(data: dict[str, Any]) -> str:
    lines: list[str] = [
        f'<presentation slides="{data.get("slide_count", 0)}" '
        f'slide_width_pt="{data.get("slide_width_pt")}" '
        f'slide_height_pt="{data.get("slide_height_pt")}">'
    ]

    for slide in data.get("slides", []):
        title = slide.get("title")
        attrs = f'index="{slide.get("index", 0) + 1}"'
        if slide.get("layout_name"):
            attrs += f' layout="{xml_escape(str(slide["layout_name"]))}"'
        if title:
            attrs += f' title="{xml_escape(title[:_SAMPLE_TEXT_CHARS])}"'
        lines.append(f"  <slide {attrs}>")

        # Collapse every run on the slide by identical style signature. A
        # deck reuses a handful of styles, so this stays tiny regardless of
        # how much text the slide holds.
        counts: Counter[tuple[Any, ...]] = Counter()
        samples: dict[tuple[Any, ...], str] = {}
        shapes_for: dict[tuple[Any, ...], set[str]] = {}
        for shape in slide.get("shapes", []):
            for para in shape.get("paragraphs", []):
                for run in para.get("text_runs", []):
                    key = _run_style_key(run)
                    counts[key] += 1
                    text = (run.get("text") or "").strip()
                    if text and key not in samples:
                        samples[key] = text[:_SAMPLE_TEXT_CHARS]
                    if shape.get("shape_name"):
                        shapes_for.setdefault(key, set()).add(shape["shape_name"])

        # Surface shapes whose extraction failed. The dict records them, but
        # this summary only walks shapes -> paragraphs -> text_runs and an
        # unreadable shape has neither, so without this the slide reads as
        # complete: a criterion like "all text uses Arial" passes because the
        # one shape that would have failed it was dropped silently.
        # Two different gaps, reported separately, because one element covering
        # both was wrong whichever way it fired. A shape whose shape_type read
        # failed still has its paragraphs extracted, so it carries `unreadable`
        # while its runs are listed below in full. Lumping it in told the judge
        # those styles were "NOT represented" when they were right there, and
        # this block is presented as ground truth — so the judge was invited to
        # discount real evidence, the inverse of the gap the element exists to
        # close. Dropping it from the warning instead went too far the other
        # way: nothing then disclosed that the shape's TYPE is unknown, so a
        # criterion about pictures or tables lost its evidence silently.
        def _represented(shape: dict[str, Any]) -> bool:
            return any(para.get("text_runs") for para in shape.get("paragraphs", []))

        flagged = [s for s in slide.get("shapes", []) if s.get("unreadable")]
        unreadable = [str(s.get("unreadable")) for s in flagged if not _represented(s)]
        partial = [str(s.get("unreadable")) for s in flagged if _represented(s)]

        if unreadable:
            reason = xml_escape(unreadable[0][:_SAMPLE_TEXT_CHARS])
            lines.append(
                f'    <unreadable_shapes count="{len(unreadable)}" '
                f'first_error="{reason}">Text and styles of these shapes are '
                f"NOT represented below; draw no conclusion about them from "
                f"their absence.</unreadable_shapes>"
            )
        if partial:
            reason = xml_escape(partial[0][:_SAMPLE_TEXT_CHARS])
            lines.append(
                f'    <partial_shapes count="{len(partial)}" '
                f'first_error="{reason}">The SHAPE TYPE of these shapes could '
                f"not be read, so draw no conclusion about what kind of object "
                f"they are. Their text and text styles ARE listed below and can "
                f"be relied on.</partial_shapes>"
            )

        for key, count in counts.most_common(_MAX_STYLES_PER_SLIDE):
            attrs = _style_attrs(key) + f' runs="{count}"'
            all_shapes = sorted(shapes_for.get(key, ()))
            shapes = all_shapes[:_MAX_SHAPE_NAMES]
            if shapes:
                attrs += f' shapes="{xml_escape(", ".join(shapes))}"'
                # Say the list is a sample. Unqualified, a criterion about
                # "the title and the footer" could read a 3-name list as the
                # complete set of shapes carrying this style.
                if len(all_shapes) > len(shapes):
                    attrs += f' shapes_listed="{len(shapes)} of {len(all_shapes)}"'
            sample = samples.get(key)
            if sample:
                lines.append(
                    f"    <text_style {attrs}>{xml_escape(sample)}</text_style>"
                )
            else:
                lines.append(f"    <text_style {attrs} />")

        omitted = len(counts) - min(len(counts), _MAX_STYLES_PER_SLIDE)
        if omitted > 0:
            lines.append(f"    <!-- {omitted} rarer style combinations omitted -->")

        lines.append("  </slide>")

    lines.append("</presentation>")
    return "\n".join(lines)


async def _extract_pptx_style_text(file_bytes: bytes, file_name: str) -> str:
    # Parsing is CPU-bound; keep it off the shared grading event loop.
    data = await asyncio.to_thread(pptx_to_style_metadata, file_bytes, file_name, True)
    return _pptx_style_summary_xml(data)


async def pptx_to_style_metadata_output(
    file_bytes: bytes, file_name: str
) -> TransformationOutput:
    """Per-slide font/size/colour summary as judge-readable XML.

    Answers criteria like "titles are 30pt on slides 1 and 7" or "the deck
    uses Arial Black" without the judge having to read font sizes off a
    rendered slide image.
    """
    if not is_ooxml_package(file_bytes):
        logger.info(
            f"[TRANSFORM] {file_name} is not an OOXML package — no pptx style "
            f"metadata available"
        )
        return TransformationOutput(
            text=(
                '<presentation unsupported="true" data-degraded="true">Style metadata requires a '
                ".pptx (OOXML) file; legacy .ppt and OpenDocument .odp are not "
                "supported by this extractor.</presentation>\n"
            )
        )

    text = await cached_style_text(
        file_bytes, file_name, "pptx", _extract_pptx_style_text
    )
    return TransformationOutput(text=text)
