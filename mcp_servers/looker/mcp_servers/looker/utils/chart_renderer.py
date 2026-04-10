"""Chart rendering utility for generating PNG visualizations from query data.

This module provides functions to render query results as various chart types
using matplotlib. It supports the common Looker visualization types and returns
base64-encoded PNG data.
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
            logger.debug("matplotlib loaded successfully")
        except ImportError:
            _MATPLOTLIB_AVAILABLE = False
            logger.warning("matplotlib not available, chart rendering will use fallback")

    return _plt if _MATPLOTLIB_AVAILABLE else None


def render_chart(
    data: list[dict[str, Any]],
    fields: list[str],
    chart_type: str = "looker_column",
    width: int = 800,
    height: int = 600,
    title: str | None = None,
) -> str:
    """Render query data as a chart and return base64-encoded PNG.

    Args:
        data: List of row dictionaries from query results
        fields: List of field names in the data
        chart_type: Type of chart to render (looker_column, looker_bar, etc.)
        width: Image width in pixels
        height: Image height in pixels
        title: Optional chart title

    Returns:
        Base64-encoded PNG image data

    Raises:
        ValueError: If data is empty or chart type is not supported
    """
    plt = _get_matplotlib()

    if plt is None:
        # Fallback: return a simple placeholder PNG
        return _generate_placeholder_png(width, height, chart_type)

    if not data:
        return _generate_empty_chart_png(plt, width, height, "No data available")

    # Normalize chart type (handle None/empty values)
    if not chart_type:
        chart_type = "looker_column"
    chart_type_normalized = chart_type.lower().replace("looker_", "")

    # Create figure with specified dimensions (convert pixels to inches at 120 DPI for sharper text)
    dpi = 120
    fig_width = width / dpi
    fig_height = height / dpi
    fig, ax = plt.subplots(figsize=(fig_width, fig_height), dpi=dpi)

    try:
        # Determine x and y fields
        # First field is typically the dimension (x-axis)
        # Subsequent numeric fields are measures (y-axis)
        x_field = fields[0] if fields else None
        y_fields = _identify_numeric_fields(data, fields[1:] if len(fields) > 1 else [])

        if not y_fields:
            # If no numeric fields found, try to use first field as both x and y
            y_fields = _identify_numeric_fields(data, fields)

        if chart_type_normalized in ("column", "bar"):
            _render_bar_chart(
                ax, data, x_field, y_fields, horizontal=(chart_type_normalized == "bar")
            )
        elif chart_type_normalized == "line":
            _render_line_chart(ax, data, x_field, y_fields)
        elif chart_type_normalized == "pie":
            _render_pie_chart(ax, data, x_field, y_fields)
        elif chart_type_normalized == "area":
            _render_area_chart(ax, data, x_field, y_fields)
        elif chart_type_normalized == "scatter":
            _render_scatter_chart(ax, data, x_field, y_fields)
        elif chart_type_normalized == "single_value":
            _render_single_value(ax, data, y_fields or fields)
        elif chart_type_normalized == "table":
            _render_table(ax, data, fields)
        else:
            # Default to column chart for unknown types
            _render_bar_chart(ax, data, x_field, y_fields, horizontal=False)

        # Set title if provided
        if title:
            ax.set_title(title)

        # Adjust layout with tight margins
        fig.subplots_adjust(left=0.1, right=0.95, top=0.92, bottom=0.2)
        plt.tight_layout(pad=0.5)

        # Convert to PNG bytes
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.1)
        buf.seek(0)
        png_data = base64.b64encode(buf.read()).decode("utf-8")

        return png_data

    finally:
        plt.close(fig)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Safely convert a value to float, returning default for non-numeric values.

    Args:
        value: Value to convert (can be int, float, string, None, or other)
        default: Default value to return if conversion fails

    Returns:
        Float value, or default if conversion fails
    """
    if value is None:
        return default
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except (ValueError, TypeError):
            return default
    return default


def _identify_numeric_fields(data: list[dict], fields: list[str]) -> list[str]:
    """Identify which fields contain numeric data.

    Args:
        data: List of data rows
        fields: List of field names to check

    Returns:
        List of field names that contain numeric data
    """
    if not data or not fields:
        return []

    numeric_fields = []
    for field in fields:
        # Check first non-None value
        for row in data:
            value = row.get(field)
            if value is not None:
                if isinstance(value, int | float):
                    numeric_fields.append(field)
                elif isinstance(value, str):
                    # Try to parse string as number
                    try:
                        float(value)
                        numeric_fields.append(field)
                    except (ValueError, TypeError):
                        pass
                break

    return numeric_fields


def _set_x_tick_labels(ax, x_positions, x_values, max_labels: int = 15):
    """Set x-axis tick labels, skipping labels to avoid overlap when there are many."""
    positions = list(x_positions) if not isinstance(x_positions, list) else x_positions
    if len(x_values) > 20:
        step = max(1, len(x_values) // max_labels)
        ax.set_xticks(positions[::step])
        ax.set_xticklabels(x_values[::step], rotation=45, ha="right", fontsize=7)
    else:
        ax.set_xticks(positions)
        ax.set_xticklabels(x_values, rotation=45, ha="right", fontsize=7)


def _render_bar_chart(
    ax, data: list[dict], x_field: str | None, y_fields: list[str], horizontal: bool = False
):
    """Render a bar/column chart."""
    if not x_field or not y_fields:
        ax.text(
            0.5,
            0.5,
            "Insufficient data for chart",
            ha="center",
            va="center",
            transform=ax.transAxes,
        )
        return

    x_values = [str(row.get(x_field, ""))[:15] for row in data]
    x_positions = range(len(x_values))

    # Limit to reasonable number of bars
    max_bars = 50
    if len(x_values) > max_bars:
        x_values = x_values[:max_bars]
        data = data[:max_bars]
        x_positions = range(len(x_values))

    bar_width = 0.8 / len(y_fields) if len(y_fields) > 1 else 0.8

    all_y_values = []
    for i, y_field in enumerate(y_fields):
        y_values = [_safe_float(row.get(y_field)) for row in data]
        all_y_values.extend(y_values)
        offset = (i - len(y_fields) / 2 + 0.5) * bar_width

        if horizontal:
            positions = [p + offset for p in x_positions]
            ax.barh(positions, y_values, height=bar_width, label=y_field.split(".")[-1])
            ax.set_yticks(list(x_positions))
            ax.set_yticklabels(x_values, fontsize=7)
            ax.set_xlabel("Value")
        else:
            positions = [p + offset for p in x_positions]
            ax.bar(positions, y_values, width=bar_width, label=y_field.split(".")[-1])
            ax.set_xticks(list(x_positions))
            ax.set_xticklabels(x_values, rotation=45, ha="right", fontsize=7)
            ax.set_ylabel("Value")

    # Match Recharts Y-axis scaling with proper handling of negative values
    if all_y_values:
        data_min = min(all_y_values)
        data_max = max(all_y_values)
        axis_min, axis_max = _get_nice_axis_range(data_min, data_max)
        if horizontal:
            ax.set_xlim(axis_min, axis_max)
        else:
            ax.set_ylim(axis_min, axis_max)

    if len(y_fields) > 1:
        ax.legend(loc="upper right", fontsize=8)

    ax.set_title(x_field.split(".")[-1] if x_field else "")


def _render_line_chart(ax, data: list[dict], x_field: str | None, y_fields: list[str]):
    """Render a line chart."""
    if not x_field or not y_fields:
        ax.text(
            0.5,
            0.5,
            "Insufficient data for chart",
            ha="center",
            va="center",
            transform=ax.transAxes,
        )
        return

    # Limit to 100 points (consistent with UI)
    max_points = 100
    plot_data = data[:max_points]

    x_values = [str(row.get(x_field, ""))[:15] for row in plot_data]
    x_positions = range(len(x_values))

    all_y_values = []
    for y_field in y_fields:
        y_values = [_safe_float(row.get(y_field)) for row in plot_data]
        all_y_values.extend(y_values)
        ax.plot(list(x_positions), y_values, marker="o", label=y_field.split(".")[-1], markersize=4)

    # Match Recharts Y-axis scaling with proper handling of negative values
    if all_y_values:
        data_min = min(all_y_values)
        data_max = max(all_y_values)
        y_min, y_max = _get_nice_axis_range(data_min, data_max)
        ax.set_ylim(y_min, y_max)

    _set_x_tick_labels(ax, list(x_positions), x_values)
    ax.set_ylabel("Value")

    if len(y_fields) > 1:
        ax.legend(loc="upper right", fontsize=8)


def _render_pie_chart(ax, data: list[dict], x_field: str | None, y_fields: list[str]):
    """Render a pie chart."""
    if not x_field or not y_fields:
        ax.text(
            0.5,
            0.5,
            "Insufficient data for chart",
            ha="center",
            va="center",
            transform=ax.transAxes,
        )
        return

    y_field = y_fields[0]  # Pie chart uses only first y field

    # Build (label, value) pairs and filter out zero/negative values
    all_data = [(str(row.get(x_field, ""))[:20], _safe_float(row.get(y_field))) for row in data]
    valid_data = [(label, val) for label, val in all_data if val > 0]
    if not valid_data:
        ax.text(
            0.5,
            0.5,
            "No positive values for pie chart",
            ha="center",
            va="center",
            transform=ax.transAxes,
        )
        return

    # Take the first 15 rows as they appear in the data
    max_slices = 15
    valid_data = valid_data[:max_slices]

    labels, values = zip(*valid_data)

    ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
    ax.axis("equal")


def _render_area_chart(ax, data: list[dict], x_field: str | None, y_fields: list[str]):
    """Render an area chart."""
    if not x_field or not y_fields:
        ax.text(
            0.5,
            0.5,
            "Insufficient data for chart",
            ha="center",
            va="center",
            transform=ax.transAxes,
        )
        return

    # Limit to 100 points (consistent with UI)
    max_points = 100
    plot_data = data[:max_points]

    x_values = [str(row.get(x_field, ""))[:15] for row in plot_data]
    x_positions = list(range(len(x_values)))

    all_y_values = []
    for y_field in y_fields:
        y_values = [_safe_float(row.get(y_field)) for row in plot_data]
        all_y_values.extend(y_values)
        ax.fill_between(x_positions, y_values, alpha=0.5, label=y_field.split(".")[-1])
        ax.plot(x_positions, y_values, linewidth=1)

    # Match Recharts Y-axis scaling with proper handling of negative values
    if all_y_values:
        data_min = min(all_y_values)
        data_max = max(all_y_values)
        y_min, y_max = _get_nice_axis_range(data_min, data_max)
        ax.set_ylim(y_min, y_max)

    _set_x_tick_labels(ax, x_positions, x_values)
    ax.set_ylabel("Value")

    if len(y_fields) > 1:
        ax.legend(loc="upper right", fontsize=8)


def _render_scatter_chart(ax, data: list[dict], _x_field: str | None, y_fields: list[str]):
    """Render a scatter plot.

    For scatter plots, we use two numeric fields:
    - X-axis: first numeric field from y_fields
    - Y-axis: second numeric field from y_fields (or same as X if only one)

    This matches the UI (Recharts) behavior for consistency.

    Note: _x_field is intentionally unused - scatter plots use y_fields for both axes.
    The parameter is kept for API consistency with other chart rendering functions.
    """
    if not y_fields or len(y_fields) < 1:
        ax.text(
            0.5,
            0.5,
            "Insufficient data for scatter plot",
            ha="center",
            va="center",
            transform=ax.transAxes,
        )
        return

    # Limit to 100 points (consistent with UI)
    max_points = 100
    plot_data = data[:max_points]

    # Use first numeric field as X, second as Y (consistent with UI)
    scatter_x_field = y_fields[0]
    scatter_y_field = y_fields[1] if len(y_fields) > 1 else y_fields[0]

    x_values = [_safe_float(row.get(scatter_x_field)) for row in plot_data]
    y_values = [_safe_float(row.get(scatter_y_field)) for row in plot_data]

    ax.scatter(x_values, y_values, alpha=0.6)
    ax.set_xlabel(scatter_x_field.split(".")[-1])
    ax.set_ylabel(scatter_y_field.split(".")[-1])

    # Set Y-axis to start at 0 when all values are positive
    if y_values:
        y_min = min(y_values)
        y_max = max(y_values)
        axis_y_min, axis_y_max = _get_nice_axis_range(y_min, y_max)
        ax.set_ylim(axis_y_min, axis_y_max)

    # Set X-axis to start at 0 when all values are positive
    if x_values:
        x_min = min(x_values)
        x_max = max(x_values)
        axis_x_min, axis_x_max = _get_nice_axis_range(x_min, x_max)
        ax.set_xlim(axis_x_min, axis_x_max)


def _render_single_value(ax, data: list[dict], fields: list[str]):
    """Render a single value display."""
    ax.axis("off")

    if not data or not fields:
        ax.text(0.5, 0.5, "N/A", ha="center", va="center", fontsize=48, transform=ax.transAxes)
        return

    # Get the first numeric value
    value = None
    label = ""
    for field in fields:
        val = data[0].get(field)
        if val is not None:
            value = val
            label = field.split(".")[-1]
            break

    if value is None:
        ax.text(0.5, 0.5, "N/A", ha="center", va="center", fontsize=48, transform=ax.transAxes)
    else:
        # Format the value
        if isinstance(value, float):
            display_value = f"{value:,.2f}"
        elif isinstance(value, int):
            display_value = f"{value:,}"
        else:
            display_value = str(value)

        ax.text(
            0.5,
            0.6,
            display_value,
            ha="center",
            va="center",
            fontsize=48,
            transform=ax.transAxes,
            fontweight="bold",
        )
        ax.text(
            0.5,
            0.3,
            label,
            ha="center",
            va="center",
            fontsize=16,
            transform=ax.transAxes,
            color="gray",
        )


def _render_table(ax, data: list[dict], fields: list[str]):
    """Render data as a table."""
    ax.axis("off")

    if not data or not fields:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
        return

    # Limit rows and columns for readability
    max_rows = 20
    max_cols = 6
    display_fields = fields[:max_cols]
    display_data = data[:max_rows]

    # Prepare table data
    cell_text = []
    for row in display_data:
        cell_row = []
        for field in display_fields:
            val = row.get(field, "")
            if isinstance(val, float):
                cell_row.append(f"{val:.2f}")
            else:
                cell_row.append(str(val)[:30])  # Truncate long values
        cell_text.append(cell_row)

    # Create headers (use last part of field name)
    headers = [f.split(".")[-1][:15] for f in display_fields]

    # Create table
    table = ax.table(
        cellText=cell_text,
        colLabels=headers,
        loc="center",
        cellLoc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    table.scale(1.2, 1.5)


def _is_numeric_field(data: list[dict], field: str) -> bool:
    """Check if a field contains numeric data."""
    for row in data:
        val = row.get(field)
        if val is not None:
            if isinstance(val, int | float):
                return True
            if isinstance(val, str):
                # Try to parse string as number
                try:
                    float(val)
                    return True
                except (ValueError, TypeError):
                    return False
            return False
    return False


def _get_nice_axis_range(
    data_min: float, data_max: float, num_ticks: int = 5
) -> tuple[float, float]:
    """Calculate nice Y-axis range to match Recharts tick-based scaling.

    Recharts picks a nice tick interval first, then calculates axis range.
    This replicates that behavior for consistency, handling both positive
    and negative data values.

    Args:
        data_min: Minimum value in the data
        data_max: Maximum value in the data
        num_ticks: Number of ticks (default 5, like Recharts)

    Returns:
        Tuple of (axis_min, axis_max) for nice axis range
    """
    import math

    # Handle NaN and Infinity values - return safe defaults
    if math.isnan(data_min) or math.isnan(data_max) or math.isinf(data_min) or math.isinf(data_max):
        return (0, 10)

    # Handle edge cases
    if data_min == data_max:
        if data_max == 0:
            return (0, 10)
        # For single positive value, start at 0 and pad above
        if data_max > 0:
            return (0, data_max * 1.2)
        # For single negative value, pad below and go to 0
        return (data_min * 1.2, 0)

    data_range = data_max - data_min

    # Calculate raw interval
    raw_interval = data_range / (num_ticks - 1)

    # Find the order of magnitude of the interval
    magnitude = 10 ** math.floor(math.log10(raw_interval))

    # Normalize interval to 1-10 range
    normalized = raw_interval / magnitude

    # Pick nice interval values (matches Recharts preferences)
    if normalized <= 1:
        nice_interval = 1
    elif normalized <= 2:
        nice_interval = 2
    elif normalized <= 3:
        nice_interval = 3  # Recharts prefers 3 over 2.5
    elif normalized <= 5:
        nice_interval = 5
    elif normalized <= 7:
        nice_interval = 7  # Recharts uses 7 for certain ranges
    else:
        nice_interval = 10

    interval = nice_interval * magnitude

    # Calculate axis min/max that covers the data with nice tick values
    # If all data is non-negative, start at 0
    if data_min >= 0:
        axis_min = 0
        axis_max = interval * (num_ticks - 1)
        while axis_max < data_max:
            axis_max += interval
    else:
        # Handle negative values - find nice bounds on both ends
        axis_min = math.floor(data_min / interval) * interval
        axis_max = math.ceil(data_max / interval) * interval
        # Ensure we have enough range
        if axis_max <= axis_min:
            axis_max = axis_min + interval

    return (axis_min, axis_max)


def _generate_placeholder_png(width: int, height: int, chart_type: str) -> str:
    """Generate a simple placeholder PNG when matplotlib is not available.

    This creates a minimal PNG with text indicating the chart type.

    Args:
        width: Image width in pixels
        height: Image height in pixels
        chart_type: Type of chart that was requested

    Returns:
        Base64-encoded PNG data
    """
    # Try to use PIL if available
    try:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (width, height), color=(240, 240, 240))
        draw = ImageDraw.Draw(img)

        # Draw border
        draw.rectangle([0, 0, width - 1, height - 1], outline=(200, 200, 200))

        # Draw text
        text = f"Chart: {chart_type}"
        text_bbox = draw.textbbox((0, 0), text)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]
        x = (width - text_width) // 2
        y = (height - text_height) // 2
        draw.text((x, y), text, fill=(100, 100, 100))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8")

    except ImportError:
        # Return a minimal 1x1 transparent PNG
        # This is a valid PNG file
        minimal_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
            b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        return base64.b64encode(minimal_png).decode("utf-8")


def _generate_empty_chart_png(plt, width: int, height: int, message: str) -> str:
    """Generate an empty chart with a message.

    Args:
        plt: matplotlib.pyplot module
        width: Image width in pixels
        height: Image height in pixels
        message: Message to display

    Returns:
        Base64-encoded PNG data
    """
    dpi = 100
    fig, ax = plt.subplots(figsize=(width / dpi, height / dpi), dpi=dpi)
    ax.text(0.5, 0.5, message, ha="center", va="center", fontsize=14, transform=ax.transAxes)
    ax.axis("off")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    png_data = base64.b64encode(buf.read()).decode("utf-8")
    plt.close(fig)

    return png_data
