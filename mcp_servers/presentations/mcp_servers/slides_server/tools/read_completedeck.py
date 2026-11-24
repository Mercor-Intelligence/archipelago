import json
import os
from io import BytesIO
from typing import Any

from models.response import ReadDeckResponse, SlideOverviewData
from models.tool_inputs import ReadCompleteDeckInput
from pptx import Presentation
from pptx.chart.chart import Chart
from pptx.shapes.autoshape import Shape
from pptx.shapes.graphfrm import GraphicFrame
from utils.decorators import make_async_background
from utils.path_utils import resolve_under_root

SLIDES_ROOT = os.getenv("APP_SLIDES_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the slides root, refusing anything that escapes it.

    Delegates to the shared resolver so containment is enforced in ONE place:
    the previous per-module copy normpath()'d after joining and never checked
    the result, so ``/../app/tools/...`` resolved outside the root. Kept as a
    module-level name so call sites (and tests) are unchanged.

    Raises PathTraversalError when the path escapes the sandbox.
    """
    return resolve_under_root(path, root=SLIDES_ROOT)


def _chart_summary(chart: Chart) -> str:
    """Serialize a chart's title, categories and series values for the overview.

    Each field is extracted independently, as read_individualslide does, so a chart
    whose categories or series cannot be read still contributes what it has instead
    of failing the whole deck read.
    """
    summary: dict[str, Any] = {}

    try:
        # has_title, unlike chart_title, does not insert a <c:title> element.
        if chart.has_title:
            summary["title"] = chart.chart_title.text_frame.text
    except Exception:
        pass

    try:
        categories = [str(category) for category in chart.plots[0].categories]
        if categories:
            summary["categories"] = categories
    except Exception:
        pass

    try:
        series: list[dict[str, Any]] = []
        for plot_series in chart.series:
            entry: dict[str, Any] = {}
            if plot_series.name:
                entry["name"] = plot_series.name
            entry["values"] = list(plot_series.values)
            series.append(entry)
        if series:
            summary["series"] = series
    except Exception:
        pass

    return "Chart: " + json.dumps(summary, ensure_ascii=False)


@make_async_background
def read_completedeck(request: ReadCompleteDeckInput) -> ReadDeckResponse:
    """Read overview information about all slides in a PowerPoint presentation.

    Provides a high-level summary of the entire deck with slide titles and content
    previews, including table rows and chart data as JSON.
    Ideal for understanding deck structure before performing detailed operations.

    Notes:
        - Fast overview without detailed components
        - Use slide_index to target slides with edit_slides/read_individualslide
        - For details: read_individualslide. For markdown: read_slides
        - Content preview: up to 500 chars, truncated without indicator
        - Title defaults to 'Slide {index}' if no text found
        - Speaker notes excluded
    """

    def error(msg: str) -> ReadDeckResponse:
        return ReadDeckResponse(success=False, error=msg)

    target_path = _resolve_under_root(request.file_path)

    try:
        if not os.path.exists(target_path):
            return error(f"File not found: {request.file_path}")
        if not os.path.isfile(target_path):
            return error(f"Not a file: {request.file_path}")

        with open(target_path, "rb") as f:
            file_bytes = f.read()
    except Exception as exc:
        return error(f"Failed to read presentation: {repr(exc)}")

    try:
        presentation = Presentation(BytesIO(file_bytes))

        slides_data = []

        for slide_index, slide in enumerate(presentation.slides):
            # Extract title (usually from the first placeholder)
            title = ""
            content_parts = []

            for shape in slide.shapes:
                # Tables and charts are GraphicFrame, not Shape, so they never
                # reach the text filter below. Serialize them before it.
                if isinstance(shape, GraphicFrame) and shape.has_table:
                    rows = [
                        [cell.text for cell in row.cells] for row in shape.table.rows
                    ]
                    content_parts.append(
                        "Table rows: " + json.dumps(rows, ensure_ascii=False)
                    )
                    continue

                if isinstance(shape, GraphicFrame) and shape.has_chart:
                    content_parts.append(_chart_summary(shape.chart))
                    continue

                # Only process shapes that have text
                if not isinstance(shape, Shape) or not shape.has_text_frame:
                    continue

                if hasattr(shape, "text") and shape.text.strip():
                    text = shape.text.strip()

                    # Try to detect if this is the title
                    if (
                        not title
                        and hasattr(shape, "is_placeholder")
                        and shape.is_placeholder
                    ):
                        try:
                            if shape.placeholder_format.type in (0, 2):
                                title = text
                                continue
                        except (AttributeError, ValueError):
                            pass

                    # Not `not title and not content_parts`: a table or chart
                    # preceding the title textbox now fills content_parts, which
                    # would otherwise leave the slide titleless. The two forms
                    # were equivalent while only this branch appended.
                    if not title:
                        title = text
                    else:
                        content_parts.append(text)

            if not title:
                title = f"Slide {slide_index}"

            content = "\n".join(content_parts) if content_parts else "(No content)"

            slides_data.append(
                SlideOverviewData(
                    slide_index=slide_index,
                    title=title,
                    content=content,
                )
            )

        return ReadDeckResponse(
            success=True,
            total_slides=len(presentation.slides),
            slides=slides_data,
        )

    except Exception as exc:
        return error(f"Failed to parse presentation: {repr(exc)}")
