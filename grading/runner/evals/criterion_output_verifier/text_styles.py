"""Authored font evidence, not a measurement of rendered glyphs."""

import asyncio
import io
import json
import sys
from collections.abc import Iterable
from typing import Any, cast
from xml.etree import ElementTree as ET

import openpyxl
from docx import Document
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from loguru import logger
from openpyxl.utils import get_column_letter
from pptx import Presentation
from pptx.enum.shapes import PP_PLACEHOLDER

_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}
_W = "{" + _NS["w"] + "}"


def _find(element: Any, path: str) -> Any:
    return element.find(path, _NS) if element is not None else None


def _theme(part: Any) -> Any:
    for relation in part.rels.values():
        if relation.reltype.endswith("/theme"):
            return ET.fromstring(relation.target_part.blob)
    return None


def _theme_font(theme: Any, name: str) -> str | None:
    if name not in {
        "+mj-lt",
        "+mn-lt",
        "+mj-ea",
        "+mn-ea",
        "+mj-cs",
        "+mn-cs",
        "major",
        "minor",
        "majorAscii",
        "minorAscii",
        "majorHAnsi",
        "minorHAnsi",
        "majorEastAsia",
        "minorEastAsia",
        "majorBidi",
        "minorBidi",
    }:
        return None
    if name.startswith(("+mj", "major")):
        family = "majorFont"
    elif name.startswith(("+mn", "minor")):
        family = "minorFont"
    else:
        return None
    script = (
        "ea"
        if name.endswith(("-ea", "EastAsia"))
        else "cs"
        if name.endswith(("-cs", "Bidi"))
        else "latin"
    )
    font = _find(theme, f"a:themeElements/a:fontScheme/a:{family}/a:{script}")
    return font.get("typeface") if font is not None else None


def _theme_colour(theme: Any, name: str) -> str | None:
    name = {
        "dark1": "dk1",
        "light1": "lt1",
        "dark2": "dk2",
        "light2": "lt2",
        "hyperlink": "hlink",
        "followedHyperlink": "folHlink",
    }.get(name, name)
    element = _find(theme, f"a:themeElements/a:clrScheme/a:{name}")
    if element is None or not len(element):
        return None
    colour = element[0]
    value = (
        colour.get("val")
        if colour.tag.endswith("}srgbClr")
        else colour.get("lastClr")
        if colour.tag.endswith("}sysClr")
        else None
    )
    return "#" + value.upper() if value else None


def _font_label(
    size: str | float | None,
    name: str | None,
    bold: bool | None,
    italic: bool | None,
    colour: str | None,
) -> str:
    return " ".join(
        [
            f"{float(size):g}pt" if size is not None else "size=inherited",
            json.dumps(name, ensure_ascii=False) if name else "font=inherited",
            "bold" if bold else "not-bold" if bold is False else "bold=inherited",
            "italic"
            if italic
            else "not-italic"
            if italic is False
            else "italic=inherited",
            "colour=" + (colour or "inherited"),
        ]
    )


def _run_ranges(scope: str, styles: Iterable[tuple[int, str]]) -> list[str]:
    ranges: list[tuple[int, int, str]] = []
    for index, style in styles:
        if ranges and ranges[-1][1] == index - 1 and ranges[-1][2] == style:
            first, _, _ = ranges[-1]
            ranges[-1] = (first, index, style)
        else:
            ranges.append((index, index, style))
    return [
        f"{scope}, runs {first}-{last}: {style}"
        if first != last
        else f"{scope}, run {first}: {style}"
        for first, last, style in ranges
    ]


def _cell_ranges(styles: dict[tuple[int, int], str]) -> list[str]:
    runs: list[tuple[int, int, int, str]] = []
    for (row, col), style in sorted(styles.items()):
        if runs and runs[-1][0] == row and runs[-1][2:] == (col - 1, style):
            _, start, _, _ = runs[-1]
            runs[-1] = (row, start, col, style)
        else:
            runs.append((row, col, col, style))
    rectangles: list[tuple[int, int, int, int, str]] = []
    active: dict[tuple[int, int, str], int] = {}
    for row, start, end, style in runs:
        key = (start, end, style)
        index = active.get(key)
        if index is not None and rectangles[index][1] == row - 1:
            first, _, _, _, _ = rectangles[index]
            rectangles[index] = (first, row, start, end, style)
        else:
            active[key] = len(rectangles)
            rectangles.append((row, row, start, end, style))
    lines = []
    for first, last, start, end, style in sorted(rectangles):
        cell = f"{get_column_letter(start)}{first}"
        if first != last or start != end:
            cell += f":{get_column_letter(end)}{last}"
        lines.append(f"{cell}: {style}")
    return lines


def _drawing_font(properties: list[Any], theme: Any, colour_map: Any) -> str:
    values: dict[str, Any] = {}
    for prop in properties:
        if prop is None:
            continue
        if prop.tag == "unresolved":
            break
        for key in ("sz", "b", "i"):
            if key not in values and prop.get(key) is not None:
                values[key] = prop.get(key)
        latin = _find(prop, "a:latin")
        if "name" not in values and latin is not None:
            name = latin.get("typeface")
            values["name"] = (
                _theme_font(theme, name) if name and name.startswith("+") else name
            )
        if prop.tag.endswith("}fontRef") and "name" not in values:
            values["name"] = _theme_font(theme, prop.get("idx", ""))
        fill = prop if prop.tag.endswith("}fontRef") else _find(prop, "a:solidFill")
        if "colour" not in values and fill is not None:
            rgb, scheme = _find(fill, "a:srgbClr"), _find(fill, "a:schemeClr")
            if rgb is not None:
                values["colour"] = (
                    "#" + rgb.get("val").upper()
                    if not len(rgb)
                    else "inherited (colour transforms)"
                )
            elif scheme is not None:
                name = scheme.get("val")
                mapped = colour_map.get(name, name) if colour_map is not None else name
                values["colour"] = (
                    _theme_colour(theme, mapped)
                    if not len(scheme)
                    else f"theme({name}; colour transforms)"
                )
            else:
                values["colour"] = "inherited"
    return _font_label(
        float(values["sz"]) / 100 if "sz" in values else None,
        values.get("name"),
        values["b"] in ("1", "true") if "b" in values else None,
        values["i"] in ("1", "true") if "i" in values else None,
        values.get("colour"),
    )


def _pptx_styles(data: bytes, parts: set[int | str] | None) -> list[str]:
    presentation = Presentation(io.BytesIO(data))
    lines: list[str] = []
    for index, slide in enumerate(presentation.slides):
        if parts is not None and index not in parts:
            continue
        master = slide.slide_layout.slide_master
        theme = _theme(master.part)
        colour_map = _find(master._element, "p:clrMap")
        for source in (slide.slide_layout, slide):
            override = _find(source._element, "p:clrMapOvr/a:overrideClrMapping")
            if override is not None:
                colour_map = override

        def shapes(
            items: Any,
            prefix: str,
            slide: Any = slide,
            master: Any = master,
            theme: Any = theme,
            colour_map: Any = colour_map,
        ) -> None:
            for shape in items:
                scope = prefix + ", shape " + json.dumps(shape.name, ensure_ascii=False)
                if hasattr(shape, "shapes"):
                    shapes(shape.shapes, scope)
                frames = [(scope, shape.text_frame)] if shape.has_text_frame else []
                if shape.has_table:
                    frames += [
                        (f"{scope}, row {r}, column {c}", cell.text_frame)
                        for r, row in enumerate(shape.table.rows, 1)
                        for c, cell in enumerate(row.cells, 1)
                    ]
                inherited: list[Any] = []
                style_kind = "otherStyle"
                if shape.is_placeholder:
                    style_kind = ""
                    base = shape
                    while base is not None:
                        kind = base.placeholder_format.type
                        if not style_kind:
                            style_kind = (
                                "titleStyle"
                                if kind
                                in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE)
                                else "bodyStyle"
                                if kind
                                in (
                                    PP_PLACEHOLDER.BODY,
                                    PP_PLACEHOLDER.OBJECT,
                                    PP_PLACEHOLDER.SUBTITLE,
                                )
                                else ""
                            )
                        if base is not shape and base.has_text_frame:
                            inherited.append(base.text_frame)
                        base = getattr(base, "_base_placeholder", None)
                for frame_scope, frame in frames:
                    for p, paragraph in enumerate(frame.paragraphs, 1):
                        level = paragraph.level + 1
                        defaults = [paragraph.font._rPr]
                        for source in [frame, *inherited]:
                            if source is not frame:
                                props = [
                                    p.font._rPr
                                    for p in source.paragraphs
                                    if p.level == paragraph.level
                                ]
                                if len({str(prop.xml) for prop in props}) > 1:
                                    defaults.append(ET.Element("unresolved"))
                                elif props:
                                    defaults.append(props[0])
                            defaults.append(
                                _find(
                                    source._txBody,
                                    f"a:lstStyle/a:lvl{level}pPr/a:defRPr",
                                )
                            )
                        if not shape.has_table:
                            defaults.append(_find(shape._element, "p:style/a:fontRef"))
                            if style_kind:
                                defaults.append(
                                    _find(
                                        master._element,
                                        f"p:txStyles/p:{style_kind}/a:lvl{level}pPr/a:defRPr",
                                    )
                                )
                                defaults.append(
                                    _find(
                                        presentation._element,
                                        f"p:defaultTextStyle/a:lvl{level}pPr/a:defRPr",
                                    )
                                )
                        lines.extend(
                            _run_ranges(
                                f"{frame_scope}, paragraph {p}",
                                (
                                    (
                                        r,
                                        _drawing_font(
                                            [run.font._rPr, *defaults],
                                            theme,
                                            colour_map,
                                        ),
                                    )
                                    for r, run in enumerate(paragraph.runs, 1)
                                    if run.text.strip()
                                ),
                            )
                        )

        shapes(slide.shapes, f"Slide {index + 1}")
    return lines


def _docx_font(
    run: Any,
    paragraph: Any,
    document: Any,
    theme: Any,
    table_style: Any = None,
    in_table: bool = False,
) -> str:
    properties = [run._r.rPr]
    style_chains = []
    ambiguous_table_style = False
    for is_table_style, style in (
        (False, run.style),
        (False, paragraph.style),
        (True, table_style),
    ):
        seen = set()
        chain = []
        while style is not None and style.style_id not in seen:
            if is_table_style and style.element.xpath("w:tblStylePr/w:rPr"):
                ambiguous_table_style = True
                chain.clear()
                break
            seen.add(style.style_id)
            chain.append(style.element.rPr)
            style = style.base_style
        properties.extend(chain)
        style_chains.append(chain)
    defaults = (
        None
        if in_table
        else _find(document.styles.element, "w:docDefaults/w:rPrDefault/w:rPr")
    )
    properties.append(defaults)
    values: dict[str, Any] = {}
    for prop in properties:
        for key in ("sz", "b", "i", "rFonts", "color"):
            element = _find(prop, "w:" + key)
            if key not in values and element is not None:
                values[key] = element
    size = values["sz"].get(_W + "val") if "sz" in values else None
    name, colour = None, None
    if "rFonts" in values:
        fonts = values["rFonts"]
        themed = fonts.get(_W + "asciiTheme") or fonts.get(_W + "hAnsiTheme")
        name = (
            _theme_font(theme, themed)
            if themed
            else fonts.get(_W + "ascii") or fonts.get(_W + "hAnsi")
        )
    if "color" in values:
        element = values["color"]
        themed, rgb = element.get(_W + "themeColor"), element.get(_W + "val")
        if themed:
            colour = _theme_colour(theme, themed)
            if element.get(_W + "themeTint") or element.get(_W + "themeShade"):
                colour = f"theme({themed}; colour transforms)"
        elif rgb and rgb != "auto":
            colour = "#" + rgb.upper()

    def emphasis(key: str) -> bool | None:
        direct = _find(run._r.rPr, "w:" + key)
        if direct is not None:
            return direct.get(_W + "val", "true") not in ("0", "false", "off")
        if ambiguous_table_style:
            return None
        value, defined = False, False
        for chain in style_chains:
            for prop in chain:
                toggle = _find(prop, "w:" + key)
                if toggle is not None:
                    value ^= toggle.get(_W + "val", "true") not in ("0", "false", "off")
                    defined = True
                    break
        if defined:
            return value
        default = _find(defaults, "w:" + key)
        return (
            default.get(_W + "val", "true") not in ("0", "false", "off")
            if default is not None
            else None
        )

    return _font_label(
        float(size) / 2 if size is not None else None,
        name,
        emphasis("b"),
        emphasis("i"),
        colour,
    )


def _docx_styles(data: bytes) -> list[str]:
    document = Document(io.BytesIO(data))
    theme = _theme(document.part)
    lines: list[str] = []

    def paragraphs(
        container: Any,
        prefix: str,
        table_style: Any = None,
        in_table: bool = False,
    ) -> None:
        for p, paragraph in enumerate(container.paragraphs, 1):
            runs = (
                Run(
                    element,
                    Paragraph(next(element.iterancestors(_W + "p")), paragraph._parent),
                )
                for element in paragraph._p.xpath(
                    ".//w:r[not(ancestor::w:del or ancestor::w:moveFrom or ancestor::w:rt)]"
                )
            )
            lines.extend(
                _run_ranges(
                    f"{prefix}Paragraph {p}",
                    (
                        (
                            r,
                            _docx_font(
                                run, run._parent, document, theme, table_style, in_table
                            ),
                        )
                        for r, run in enumerate(runs, 1)
                        if run.text.strip()
                    ),
                )
            )
        for t, table in enumerate(container.tables, 1):
            for r, row in enumerate(table.rows, 1):
                for c, cell in enumerate(row.cells, 1):
                    paragraphs(
                        cell,
                        f"{prefix}Table {t}, row {r}, column {c}, ",
                        table.style,
                        True,
                    )

    paragraphs(document, "")
    seen = set()
    for s, section in enumerate(document.sections, 1):
        for name in (
            "header",
            "footer",
            "first_page_header",
            "first_page_footer",
            "even_page_header",
            "even_page_footer",
        ):
            container = getattr(section, name)
            if (
                not container.is_linked_to_previous
                and container.part.partname not in seen
            ):
                seen.add(container.part.partname)
                paragraphs(container, f"Section {s} {name}, ")
    return lines


def _xlsx_styles(data: bytes, parts: set[int | str] | None) -> list[str]:
    workbook = openpyxl.load_workbook(io.BytesIO(data))
    palette: tuple[str, ...] = tuple(getattr(workbook, "_colors", ()))
    try:
        theme = ET.fromstring(workbook.loaded_theme) if workbook.loaded_theme else None
        lines = []
        for sheet in workbook:
            if parts is not None and sheet.title not in parts:
                continue
            styles = {}
            column_fonts: dict[int, Any] = {}
            cells: dict[tuple[int, int], Any] = cast(Any, sheet)._cells
            for cell in cells.values():
                if cell.value is None:
                    continue
                font, colour = cell.font, None
                if cell.style_id == 0:
                    dimension = sheet.row_dimensions.get(cell.row)
                    if dimension is not None and dimension.customFormat:
                        font = dimension.font
                    else:
                        if cell.column not in column_fonts:
                            matches = [
                                d
                                for d in sheet.column_dimensions.values()
                                if d.has_style
                                and d.min is not None
                                and d.max is not None
                                and d.min <= cell.column <= d.max
                            ]
                            column_fonts[cell.column] = (
                                matches[0].font
                                if len(matches) == 1
                                else None
                                if matches
                                else font
                            )
                        font = column_fonts[cell.column]
                if font is None:
                    styles[(cell.row, cell.column)] = _font_label(
                        None, None, None, None, None
                    )
                    continue
                color = font.color
                if color is not None:
                    if color.type == "rgb":
                        colour = "#" + color.rgb[-6:].upper()
                    elif color.type == "indexed" and 0 <= color.indexed < min(
                        64, len(palette)
                    ):
                        entry = cast(object, palette[color.indexed])
                        colour = (
                            "#" + entry[-6:].upper() if isinstance(entry, str) else None
                        )
                    elif color.type == "theme" and 0 <= color.theme < 12:
                        name = (
                            "lt1",
                            "dk1",
                            "lt2",
                            "dk2",
                            "accent1",
                            "accent2",
                            "accent3",
                            "accent4",
                            "accent5",
                            "accent6",
                            "hlink",
                            "folHlink",
                        )[color.theme]
                        colour = _theme_colour(theme, name)
                    if color.tint:
                        colour = f"{color.type}({color.value}; tint={color.tint:g})"
                name = (
                    _theme_font(theme, font.scheme)
                    if font.scheme in ("major", "minor")
                    else font.name
                )
                styles[(cell.row, cell.column)] = _font_label(
                    font.sz, name, font.b, font.i, colour
                )
            lines.extend(
                f"Sheet {json.dumps(sheet.title, ensure_ascii=False)}, {line}"
                for line in _cell_ranges(styles)
            )
        return lines
    finally:
        workbook.close()


def extract_text_styles(
    data: bytes, extension: str, parts: set[int | str] | None = None
) -> list[str]:
    if extension == ".pptx":
        return _pptx_styles(data, parts)
    if extension == ".docx":
        return _docx_styles(data)
    return _xlsx_styles(data, parts)


def render_text_styles(lines: list[str], max_chars: int = 8_000) -> str:
    header, marker = "\n\n=== Text Styles ===\n", "[Text styles truncated]"
    max_chars = min(max_chars, 8_000)
    if not lines or max_chars < len(header) + len(marker):
        return ""
    out: list[str] = []
    size = len(header)
    for i, line in enumerate(lines):
        line = line.replace("\r", "\\r").replace("\n", "\\n")
        more = i < len(lines) - 1
        reserve = len(marker) + 1 if more else 0
        if (len(out) >= 127 and more) or size + len(line) + reserve > max_chars:
            out.append(marker)
            break
        out.append(line)
        size += len(line) + 1
    return header + "\n".join(out)


async def extract_text_style_lines(
    data: bytes, extension: str, parts: set[int | str] | None = None
) -> list[str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        __file__,
        extension,
        json.dumps(sorted(parts) if parts is not None else None),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(data), timeout=60)
    except BaseException as error:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(error, TimeoutError):
            logger.debug(f"Text-style extraction timed out for {extension}")
            return []
        raise
    if process.returncode:
        raise ValueError(stderr.decode(errors="replace")[-1_000:])
    lines = stdout.decode().split("\n")
    return lines[:-1] if lines[-1] == "" else lines


def _main() -> None:
    parts = json.loads(sys.argv[2])
    lines = extract_text_styles(
        sys.stdin.buffer.read(), sys.argv[1], set(parts) if parts is not None else None
    )
    section = render_text_styles(lines)
    sys.stdout.write(section.removeprefix("\n\n=== Text Styles ===\n"))


if __name__ == "__main__":
    _main()
