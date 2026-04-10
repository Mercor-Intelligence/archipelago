"""PDF rendering utility for generating PDF documents from chart/query data.

This module provides functions to render query results as PDF documents.
It uses matplotlib to render charts and then converts them to PDF format.
"""

import base64
import io
from typing import Any

from loguru import logger

# Lazy import matplotlib to avoid startup overhead
_plt = None
_MATPLOTLIB_AVAILABLE = None


def _get_matplotlib():
    """Lazily import matplotlib and return the pyplot module.

    Returns:
        matplotlib.pyplot module or None if not available
    """
    global _plt, _MATPLOTLIB_AVAILABLE

    if _MATPLOTLIB_AVAILABLE is None:
        try:
            import matplotlib

            matplotlib.use("Agg")  # Use non-interactive backend
            import matplotlib.pyplot as plt

            _plt = plt
            _MATPLOTLIB_AVAILABLE = True
            logger.debug("matplotlib loaded successfully for PDF rendering")
        except ImportError:
            _MATPLOTLIB_AVAILABLE = False
            logger.warning("matplotlib not available, PDF rendering will use fallback")

    return _plt if _MATPLOTLIB_AVAILABLE else None


def render_look_pdf(
    data: list[dict[str, Any]],
    fields: list[str],
    look_title: str = "Look Report",
    chart_type: str = "looker_column",
    width: int = 800,
    height: int = 600,
) -> str:
    """Render Look data as a PDF and return base64-encoded data.

    Args:
        data: List of row dictionaries from query results
        fields: List of field names in the data
        look_title: Title for the PDF document
        chart_type: Type of chart to render
        width: PDF width in pixels
        height: PDF height in pixels

    Returns:
        Base64-encoded PDF data
    """
    plt = _get_matplotlib()

    if plt is None:
        return _generate_placeholder_pdf(width, height, look_title)

    # Import chart renderer to reuse chart rendering logic
    from utils.chart_renderer import render_chart

    # First render as PNG using existing chart renderer
    png_base64 = render_chart(
        data=data,
        fields=fields,
        chart_type=chart_type,
        width=width,
        height=height,
        title=look_title,
    )

    # Convert PNG to PDF
    return _png_to_pdf(png_base64, width, height, look_title)


def render_dashboard_pdf(
    tiles: list[dict[str, Any]],
    dashboard_title: str = "Dashboard Report",
    width: int = 1200,
    height: int = 800,
) -> str:
    """Render Dashboard with multiple tiles as a PDF.

    Args:
        tiles: List of tile data, each containing:
            - title: Tile title
            - data: Query result data
            - fields: Field names
            - chart_type: Optional visualization type
        dashboard_title: Title for the PDF document
        width: PDF width in pixels
        height: PDF height in pixels

    Returns:
        Base64-encoded PDF data
    """
    plt = _get_matplotlib()

    if plt is None:
        return _generate_placeholder_pdf(width, height, dashboard_title)

    if not tiles:
        return _generate_empty_pdf(plt, width, height, "No tiles in dashboard")

    # Calculate grid layout based on number of tiles
    n_tiles = len(tiles)
    if n_tiles == 1:
        n_cols, n_rows = 1, 1
    elif n_tiles == 2:
        n_cols, n_rows = 2, 1
    elif n_tiles <= 4:
        n_cols, n_rows = 2, 2
    elif n_tiles <= 6:
        n_cols, n_rows = 3, 2
    else:
        n_cols = 3
        n_rows = (n_tiles + 2) // 3

    # Create figure with subplots
    dpi = 100
    fig_width = width / dpi
    fig_height = height / dpi
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(fig_width, fig_height), dpi=dpi)

    try:
        # Flatten axes array for easier iteration
        if n_tiles == 1:
            axes = [axes]
        else:
            axes = axes.flatten() if hasattr(axes, "flatten") else [axes]

        # Import chart rendering helpers
        from utils.chart_renderer import (
            _identify_numeric_fields,
            _render_area_chart,
            _render_bar_chart,
            _render_line_chart,
            _render_pie_chart,
            _render_scatter_chart,
            _render_single_value,
            _render_table,
        )

        # Render each tile
        for i, tile in enumerate(tiles):
            if i >= len(axes):
                break

            ax = axes[i]
            tile_title = tile.get("title", f"Tile {i + 1}")
            tile_data = tile.get("data", [])
            tile_fields = tile.get("fields", [])
            tile_chart_type = tile.get("chart_type") or "looker_column"

            if not tile_data or not tile_fields:
                ax.text(
                    0.5,
                    0.5,
                    f"{tile_title}\n(No data)",
                    ha="center",
                    va="center",
                    transform=ax.transAxes,
                    fontsize=10,
                )
                ax.axis("off")
                continue

            # Determine x and y fields
            x_field = tile_fields[0] if tile_fields else None
            y_fields = _identify_numeric_fields(
                tile_data, tile_fields[1:] if len(tile_fields) > 1 else []
            )

            if not y_fields:
                y_fields = _identify_numeric_fields(tile_data, tile_fields)

            # Normalize chart type
            chart_type_normalized = tile_chart_type.lower().replace("looker_", "")

            try:
                if chart_type_normalized in ("column", "bar"):
                    _render_bar_chart(
                        ax,
                        tile_data,
                        x_field,
                        y_fields,
                        horizontal=(chart_type_normalized == "bar"),
                    )
                elif chart_type_normalized == "line":
                    _render_line_chart(ax, tile_data, x_field, y_fields)
                elif chart_type_normalized == "pie":
                    _render_pie_chart(ax, tile_data, x_field, y_fields)
                elif chart_type_normalized == "area":
                    _render_area_chart(ax, tile_data, x_field, y_fields)
                elif chart_type_normalized == "scatter":
                    _render_scatter_chart(ax, tile_data, x_field, y_fields)
                elif chart_type_normalized == "single_value":
                    _render_single_value(ax, tile_data, tile_fields)
                elif chart_type_normalized == "table":
                    _render_table(ax, tile_data, tile_fields)
                else:
                    # Unknown chart type - default to column chart
                    _render_bar_chart(ax, tile_data, x_field, y_fields, horizontal=False)

                ax.set_title(tile_title, fontsize=10, fontweight="bold")
            except Exception as e:
                logger.warning(f"Error rendering tile {tile_title}: {e}")
                ax.text(
                    0.5,
                    0.5,
                    f"{tile_title}\n(Render error)",
                    ha="center",
                    va="center",
                    transform=ax.transAxes,
                    fontsize=10,
                )
                ax.axis("off")

        # Hide unused axes
        for i in range(len(tiles), len(axes)):
            axes[i].axis("off")

        # Add dashboard title
        fig.suptitle(dashboard_title, fontsize=14, fontweight="bold", y=0.98)

        plt.tight_layout(rect=[0, 0, 1, 0.95])

        # Save to PDF
        buf = io.BytesIO()
        fig.savefig(buf, format="pdf", bbox_inches="tight")
        buf.seek(0)
        pdf_data = base64.b64encode(buf.read()).decode("utf-8")

        return pdf_data
    finally:
        plt.close(fig)


def _png_to_pdf(png_base64: str, width: int, height: int, title: str) -> str:
    """Convert a base64 PNG to a PDF document.

    Args:
        png_base64: Base64-encoded PNG data
        width: Document width
        height: Document height
        title: Document title

    Returns:
        Base64-encoded PDF data
    """
    plt = _get_matplotlib()

    if plt is None:
        return _generate_placeholder_pdf(width, height, title)

    # Decode PNG
    png_bytes = base64.b64decode(png_base64)

    # Load image
    from matplotlib import image as mpimg

    img = mpimg.imread(io.BytesIO(png_bytes), format="png")

    # Create figure and add image
    dpi = 100
    fig_width = width / dpi
    fig_height = height / dpi
    fig, ax = plt.subplots(figsize=(fig_width, fig_height), dpi=dpi)

    ax.imshow(img)
    ax.axis("off")

    # Save as PDF
    buf = io.BytesIO()
    fig.savefig(buf, format="pdf", bbox_inches="tight", pad_inches=0)
    buf.seek(0)
    pdf_data = base64.b64encode(buf.read()).decode("utf-8")
    plt.close(fig)

    return pdf_data


def _generate_placeholder_pdf(width: int, height: int, title: str) -> str:
    """Generate a simple placeholder PDF when matplotlib is not available.

    Args:
        width: Document width
        height: Document height
        title: Document title

    Returns:
        Base64-encoded PDF data
    """
    # Try to use reportlab if available
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas

        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=letter)
        c.drawString(100, 750, title)
        c.drawString(100, 700, f"Dimensions: {width}x{height}")
        c.drawString(100, 650, "(matplotlib not available for chart rendering)")
        c.save()
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8")
    except ImportError:
        pass

    # Generate a minimal valid PDF with proper byte offsets
    # Build PDF objects and compute offsets dynamically
    obj1 = "<< /Type /Catalog /Pages 2 0 R >>"
    obj2 = "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
    obj3 = (
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
        f"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
    )

    # Escape special characters for PDF literal string
    # PDF requires escaping of (, ), and \ characters
    # Also sanitize non-latin-1 characters to avoid UnicodeEncodeError
    safe_title = title.encode("latin-1", errors="replace").decode("latin-1")
    escaped_title = safe_title.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    # Build stream content and compute its length
    stream_content = f"BT\n/F1 24 Tf\n50 {height - 50} Td\n({escaped_title}) Tj\nET"
    stream_length = len(stream_content)

    obj5 = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    # Build PDF body with proper structure
    lines = ["%PDF-1.4"]
    offsets = [0]  # Object 0 is special (free)

    # Object 1
    offsets.append(len("\n".join(lines)) + 1)
    lines.extend(["1 0 obj", obj1, "endobj"])

    # Object 2
    offsets.append(len("\n".join(lines)) + 1)
    lines.extend(["2 0 obj", obj2, "endobj"])

    # Object 3
    offsets.append(len("\n".join(lines)) + 1)
    lines.extend(["3 0 obj", obj3, "endobj"])

    # Object 4 (stream)
    offsets.append(len("\n".join(lines)) + 1)
    lines.extend(
        [
            "4 0 obj",
            f"<< /Length {stream_length} >>",
            "stream",
            stream_content,
            "endstream",
            "endobj",
        ]
    )

    # Object 5
    offsets.append(len("\n".join(lines)) + 1)
    lines.extend(["5 0 obj", obj5, "endobj"])

    # xref table
    xref_offset = len("\n".join(lines)) + 1
    lines.append("xref")
    lines.append(f"0 {len(offsets)}")
    lines.append("0000000000 65535 f ")
    for offset in offsets[1:]:
        lines.append(f"{offset:010d} 00000 n ")

    # trailer
    lines.append("trailer")
    lines.append(f"<< /Size {len(offsets)} /Root 1 0 R >>")
    lines.append("startxref")
    lines.append(str(xref_offset))
    lines.append("%%EOF")

    pdf_content = "\n".join(lines)
    return base64.b64encode(pdf_content.encode("latin-1")).decode("utf-8")


def _generate_empty_pdf(plt, width: int, height: int, message: str) -> str:
    """Generate an empty PDF with a message.

    Args:
        plt: matplotlib.pyplot module
        width: Document width
        height: Document height
        message: Message to display

    Returns:
        Base64-encoded PDF data
    """
    dpi = 100
    fig, ax = plt.subplots(figsize=(width / dpi, height / dpi), dpi=dpi)
    ax.text(0.5, 0.5, message, ha="center", va="center", fontsize=14, transform=ax.transAxes)
    ax.axis("off")

    buf = io.BytesIO()
    fig.savefig(buf, format="pdf", bbox_inches="tight")
    buf.seek(0)
    pdf_data = base64.b64encode(buf.read()).decode("utf-8")
    plt.close(fig)

    return pdf_data
