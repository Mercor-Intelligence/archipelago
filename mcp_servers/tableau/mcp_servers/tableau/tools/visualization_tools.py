"""Visualization tools for Tableau-style drag-and-drop query generation.

Implements 5 tools:
- tableau_upload_csv: Upload CSV data into the in-memory database
- tableau_get_sheets: Get sheets with shelf configurations
- tableau_list_fields: List fields from a datasource table
- tableau_configure_shelf: Configure shelf layout for a view
- tableau_create_visualization: Generate SQL, execute query, render chart

These tools work for both the GUI (REST bridge) and AI agent (MCP protocol).
"""

import base64
import gc
import io
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
from db.models import Datasource, View, Workbook, WorkbookDatasource
from db.session import get_engine, get_session
from models import (
    ShelfConfig,
    TableauConfigureShelfInput,
    TableauConfigureShelfOutput,
    TableauCreateSheetInput,
    TableauCreateSheetOutput,
    TableauCreateVisualizationInput,
    TableauCreateVisualizationOutput,
    TableauFieldInfo,
    TableauGetSheetsInput,
    TableauGetSheetsOutput,
    TableauListFieldsInput,
    TableauListFieldsOutput,
    TableauSheetInfo,
    TableauUploadCsvFieldInfo,
    TableauUploadCsvInput,
    TableauUploadCsvOutput,
    TableauVisualizationData,
)
from sqlalchemy import inspect, select, text

logger = logging.getLogger(__name__)

# Default IDs from session.py
DEFAULT_SITE_ID = "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"
DEFAULT_USER_ID = "b1c2d3e4-f5a6-4b5c-8d9e-0f1a2b3c4d5e"
DEFAULT_PROJECT_ID = "c2d3e4f5-a6b7-4c5d-9e0f-1a2b3c4d5e6f"

# Valid aggregation functions
VALID_AGGREGATIONS = {"SUM", "AVG", "COUNT", "MIN", "MAX", "COUNT_DISTINCT"}

# Valid filter operators
VALID_OPERATORS = {"=", "!=", ">", "<", ">=", "<=", "IN", "NOT IN", "LIKE"}

# CSV file storage directory — cleared on each session start
CSV_STORAGE_DIR = Path(os.environ.get("MCP_CSV_DIR") or "/tmp/mcp-tableau-csvs")


def clear_csv_storage() -> None:
    """Wipe and recreate the CSV storage directory. Called on session start."""
    if CSV_STORAGE_DIR.exists():
        shutil.rmtree(CSV_STORAGE_DIR)
    CSV_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"CSV storage directory reset: {CSV_STORAGE_DIR}")


# ============================================================================
# Helpers
# ============================================================================


def _validate_identifier(name: str) -> str:
    """Validate that a SQL identifier contains only safe characters.

    Raises ValueError if the name contains characters outside [a-zA-Z0-9_].
    """
    if not re.match(r"^[a-zA-Z0-9_]+$", name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def _sanitize_table_name(name: str) -> str:
    """Create a safe SQLite table name from a datasource name."""
    # Remove file extension
    name = re.sub(r"\.\w+$", "", name)
    # Replace non-alphanumeric with underscore
    name = re.sub(r"[^a-zA-Z0-9]", "_", name)
    # Collapse multiple underscores
    name = re.sub(r"_+", "_", name).strip("_").lower()
    # Prefix with csv_ to avoid collisions with ORM tables
    return f"csv_{name}" if name else "csv_data"


def _infer_field_type(series: pd.Series) -> str:
    """Infer field data type from a pandas Series."""
    # Drop nulls for analysis
    non_null = series.dropna()
    if len(non_null) == 0:
        return "STRING"

    # Check pandas dtype first
    dtype = series.dtype
    if pd.api.types.is_integer_dtype(dtype):
        return "INTEGER"
    if pd.api.types.is_float_dtype(dtype):
        return "REAL"
    if pd.api.types.is_bool_dtype(dtype):
        return "BOOLEAN"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "DATETIME"

    # For object dtype, try to infer from values
    sample = non_null.head(100)
    # Try numeric
    try:
        numeric = pd.to_numeric(sample, errors="raise")
        if all(numeric == numeric.astype(int)):
            return "INTEGER"
        return "REAL"
    except (ValueError, TypeError):
        pass

    # Try datetime
    try:
        pd.to_datetime(sample, errors="raise", format="mixed")
        return "DATE"
    except (ValueError, TypeError):
        pass

    return "STRING"


def _infer_field_role(data_type: str) -> str:
    """Infer field role (DIMENSION vs MEASURE) from data type."""
    if data_type in ("INTEGER", "REAL"):
        return "MEASURE"
    return "DIMENSION"


def _decode_csv_content(csv_content: str | None, file_content_base64: str | None) -> str:
    """Decode CSV content from either plain text or base64.

    ``file_content_base64`` is the dedicated path for base64-encoded payloads
    (used by the UI file-upload flow).  ``csv_content`` is treated as plain
    text — no auto-detection is attempted because many valid CSV strings are
    also valid base64, which would silently corrupt the data.
    """
    if file_content_base64:
        try:
            return base64.b64decode(file_content_base64, validate=True).decode("utf-8")
        except Exception as exc:
            raise ValueError(f"Invalid base64 or non-UTF-8 file content: {exc}") from exc

    if csv_content:
        return csv_content

    raise ValueError("Either csv_content or file_content_base64 must be provided")


# ============================================================================
# Query Builder
# ============================================================================


def build_query(shelf_config: ShelfConfig, table_name: str) -> tuple[str, dict[str, Any]]:
    """Build a parameterized SQL query from shelf configuration.

    Args:
        shelf_config: The shelf configuration with rows, columns, measures, filters
        table_name: The SQLite table name to query

    Returns:
        Tuple of (SQL string with :param placeholders, parameter dict)
    """
    # Validate table name to prevent SQL injection
    if not re.match(r"^[a-zA-Z0-9_]+$", table_name):
        raise ValueError(f"Invalid table name: {table_name!r}")

    rows = shelf_config.rows
    columns = shelf_config.columns
    measures = shelf_config.measures
    filters = shelf_config.filters

    # Validate all field names to prevent SQL injection via identifier interpolation
    _all_field_names = (
        rows
        + columns
        + [m.field for m in measures]
        + [f.field for f in filters]
        + ([shelf_config.color] if shelf_config.color else [])
        + ([shelf_config.size] if shelf_config.size else [])
        + ([shelf_config.label] if shelf_config.label else [])
    )
    for fname in _all_field_names:
        if not re.match(r"^[a-z0-9_]+$", fname):
            raise ValueError(f"Invalid field name: {fname!r}")

    # All dimension fields form the GROUP BY
    group_by_fields = rows + columns
    for encoding_field in (shelf_config.color, shelf_config.size, shelf_config.label):
        if encoding_field and encoding_field not in group_by_fields:
            group_by_fields.append(encoding_field)

    # Build SELECT clause
    select_parts: list[str] = []
    for f in group_by_fields:
        select_parts.append(f'"{f}"')

    for m in measures:
        agg = m.aggregation.upper()
        if agg not in VALID_AGGREGATIONS:
            raise ValueError(f"Invalid aggregation '{agg}'. Use: {', '.join(VALID_AGGREGATIONS)}")
        field = m.field
        if agg == "COUNT_DISTINCT":
            select_parts.append(f'COUNT(DISTINCT "{field}") AS "{field}_COUNT_DISTINCT"')
        else:
            select_parts.append(f'{agg}("{field}") AS "{field}_{agg}"')

    # Auto-add COUNT(*) when dimensions exist but no explicit measures
    auto_count = False
    if group_by_fields and not measures:
        select_parts.append('COUNT(*) AS "count"')
        auto_count = True

    # If nothing selected, select all columns
    if not select_parts:
        sql = f'SELECT * FROM "{table_name}"'
    else:
        sql = f'SELECT {", ".join(select_parts)} FROM "{table_name}"'

    # Build WHERE clause
    params: dict[str, Any] = {}
    if filters:
        where_parts = []
        for i, f in enumerate(filters):
            op = f.op.upper()
            if op not in VALID_OPERATORS:
                raise ValueError(f"Invalid operator '{op}'. Use: {', '.join(VALID_OPERATORS)}")
            param_name = f"filter_{i}"
            field = f'"{f.field}"'

            if op in ("IN", "NOT IN"):
                # IN operator: value should be a list
                values = f.value if isinstance(f.value, list) else [f.value]
                if not values:
                    # Empty list: IN () is always false, NOT IN () is always true
                    where_parts.append("0 = 1" if op == "IN" else "1 = 1")
                else:
                    placeholders = ", ".join(f":{param_name}_{j}" for j in range(len(values)))
                    where_parts.append(f"{field} {op} ({placeholders})")
                    for j, v in enumerate(values):
                        params[f"{param_name}_{j}"] = v
            else:
                where_parts.append(f"{field} {op} :{param_name}")
                params[param_name] = f.value

        sql += f" WHERE {' AND '.join(where_parts)}"

    # GROUP BY (when there are aggregated measures or auto-count)
    if group_by_fields and (measures or auto_count):
        group_clause = ", ".join(f'"{f}"' for f in group_by_fields)
        sql += f" GROUP BY {group_clause}"

    # ORDER BY — validate sort_field against known result columns to prevent injection
    if shelf_config.sort_field:
        # Build set of valid column names that will appear in the result set
        valid_sort_targets: set[str] = set(group_by_fields)
        for m in measures:
            agg = m.aggregation.upper()
            if agg == "COUNT_DISTINCT":
                valid_sort_targets.add(f"{m.field}_COUNT_DISTINCT")
            else:
                valid_sort_targets.add(f"{m.field}_{agg}")
        if auto_count:
            valid_sort_targets.add("count")

        if shelf_config.sort_field not in valid_sort_targets:
            raise ValueError(
                f"Invalid sort_field '{shelf_config.sort_field}'. "
                f"Must be one of: {sorted(valid_sort_targets)}"
            )
        sort_dir = "DESC" if shelf_config.sort_order.upper() == "DESC" else "ASC"
        sql += f' ORDER BY "{shelf_config.sort_field}" {sort_dir}'
    elif group_by_fields:
        order_clause = ", ".join(f'"{f}"' for f in group_by_fields)
        sql += f" ORDER BY {order_clause}"

    # LIMIT
    if shelf_config.limit:
        sql += " LIMIT :_limit"
        params["_limit"] = int(shelf_config.limit)

    return sql, params


# ============================================================================
# Chart Renderer
# ============================================================================


def _should_facet(rows: list[str], columns: list[str], measures: list) -> bool:
    """Facet when there are dimensions on BOTH shelves and at least one measure."""
    return bool(rows) and bool(columns) and bool(measures)


def _render_faceted(
    df: pd.DataFrame,
    shelf_config: ShelfConfig,
    width: int,
    height: int,
    fmt: str,
) -> str:
    """Render a faceted (trellis) chart: one subplot row per unique value of rows[0].

    The columns dimension(s) form the x-axis within each subplot. A gray
    annotation strip on the right labels each facet row, similar to Tableau.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = shelf_config.rows
    columns = shelf_config.columns
    measures = shelf_config.measures
    mark_type = shelf_config.mark_type.lower()

    facet_dim = rows[0]
    facet_values = df[facet_dim].unique()

    # Cap facets to keep rendered PNG under stdio pipe buffer (~64KB)
    max_facets = 10
    total_facet_count = len(facet_values)
    if total_facet_count > max_facets:
        facet_values = facet_values[:max_facets]

    # Filter out facet values with no data
    facet_values = [v for v in facet_values if not df[df[facet_dim] == v].empty]
    n_facets = len(facet_values)
    truncated = total_facet_count > n_facets

    # Single facet value — fall back to normal (non-faceted) rendering
    if n_facets <= 1:
        return None  # signal caller to fall through

    dpi = 100
    facet_height = max(2.5, (height / dpi) / n_facets)
    fig_h = facet_height * n_facets + 1.0  # extra room for suptitle
    fig_w = width / dpi

    fig, axes = plt.subplots(
        nrows=n_facets,
        ncols=1,
        figsize=(fig_w, fig_h),
        dpi=dpi,
        sharex=True,
        sharey=True,
        squeeze=False,
    )

    # Build measure column name
    measure_col = f"{measures[0].field}_{measures[0].aggregation.upper()}"
    if measure_col not in df.columns:
        measure_col = measures[0].field

    # If multiple columns dims, we'll concatenate them for x-axis labels
    multi_col = len(columns) > 1

    for i, val in enumerate(facet_values):
        ax = axes[i, 0]
        facet_df = df[df[facet_dim] == val].copy().head(50)

        # Build composite x-labels when multiple columns dimensions
        if multi_col:
            facet_df["__x_label__"] = facet_df[columns].astype(str).agg(" - ".join, axis=1)
            x_col_name = "__x_label__"
        else:
            x_col_name = columns[0]

        # Build a mini shelf_config: columns dim becomes the "rows" for the
        # sub-renderers (they use rows[0] as x-axis labels).
        if mark_type in ("line", "area"):
            _render_line(
                ax,
                facet_df,
                [x_col_name],
                [],
                measures,
                shelf_config,
                fill=(mark_type == "area"),
            )
        else:
            # Default to bar
            _render_bar(ax, facet_df, [x_col_name], [], measures, shelf_config)

        # Clear the sub-chart's own title — we use the suptitle instead
        ax.set_title("")
        ax.set_xlabel("")

        # Gray header strip annotation on the right side
        ax.annotate(
            str(val),
            xy=(1.02, 0.5),
            xycoords="axes fraction",
            fontsize=10,
            fontweight="bold",
            va="center",
            ha="left",
            bbox=dict(boxstyle="round,pad=0.3", fc="#e0e0e0", ec="#cccccc"),
        )

    # Only show x-axis label on the bottom subplot
    axes[-1, 0].set_xlabel(
        " - ".join(columns) if multi_col else columns[0],
    )

    # Suptitle
    title = f"{measure_col} by {columns[0]}"
    if truncated:
        title += f"  (showing top {max_facets} of {facet_dim})"
    fig.suptitle(title, fontsize=12, y=1.0)

    plt.tight_layout(rect=[0, 0, 0.95, 0.97])  # leave room for facet labels

    try:
        buf = io.BytesIO()
        fig.savefig(buf, format=fmt, dpi=dpi, bbox_inches="tight")
        buf.seek(0)
        encoded = base64.b64encode(buf.read()).decode("ascii")
        buf.close()
        return encoded
    finally:
        plt.close(fig)
        gc.collect()


def render_chart(
    df: pd.DataFrame,
    shelf_config: ShelfConfig,
    width: int = 800,
    height: int = 500,
    fmt: str = "png",
) -> str:
    """Render a chart from query results using matplotlib.

    Args:
        df: Query result DataFrame
        shelf_config: Shelf configuration (determines chart type)
        width: Image width in pixels
        height: Image height in pixels
        fmt: Output format ('png' or 'svg')

    Returns:
        Base64-encoded image string
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    mark_type = shelf_config.mark_type.lower()
    rows = shelf_config.rows
    columns = shelf_config.columns
    measures = shelf_config.measures

    # Route to faceted rendering when dimensions on both shelves
    logger.debug(
        "render_chart: rows=%s, columns=%s, measures=%s, mark_type=%s, df_shape=%s",
        rows,
        columns,
        [m.field for m in measures],
        mark_type,
        df.shape,
    )
    if (
        not df.empty
        and _should_facet(rows, columns, measures)
        and mark_type not in ("table", "scatter", "pie")
    ):
        logger.info("Faceted rendering triggered: facet_dim=%s, x_dim=%s", rows[0], columns[0])
        result = _render_faceted(df, shelf_config, width, height, fmt)
        if result is not None:  # None means single facet value, fall through
            return result
        logger.info("Single facet value, falling through to normal rendering")

    dpi = 100

    # Multi-measure stacked subplots: one row per measure when 2+ measures
    # on a bar/line/area chart with dimensions on the same shelf.
    dims = rows if rows else (columns if columns else [])
    use_multi_measure = (
        len(measures) >= 2
        and mark_type in ("bar", "line", "area")
        and len(dims) >= 1
        and not df.empty
    )

    if use_multi_measure:
        n_measures = len(measures)
        subplot_h = max(3.0, (height / dpi) / n_measures)
        fig_h = subplot_h * n_measures
        fig, axes = plt.subplots(
            nrows=n_measures,
            ncols=1,
            figsize=(width / dpi, fig_h),
            dpi=dpi,
            sharex=True,
            squeeze=False,
        )
        render_df = _cap_rows(df, shelf_config, max_rows=50)

        for i, m in enumerate(measures):
            sub_ax = axes[i, 0]
            single_measure = [m]
            if mark_type == "line":
                _render_line(sub_ax, render_df, rows, columns, single_measure, shelf_config)
            elif mark_type == "area":
                _render_line(
                    sub_ax, render_df, rows, columns, single_measure, shelf_config, fill=True
                )
            elif rows and not columns:
                _render_horizontal_bar(
                    sub_ax, render_df, rows, columns, single_measure, shelf_config
                )
            else:
                _render_bar(sub_ax, render_df, rows, columns, single_measure, shelf_config)

            # Only show x-tick labels on the bottom subplot
            if i < n_measures - 1:
                sub_ax.set_xticklabels([])
                sub_ax.set_xlabel("")

        # Use the top subplot for nested header metadata
        ax = axes[0, 0]
    else:
        fig, ax = plt.subplots(figsize=(width / dpi, height / dpi), dpi=dpi)

        if df.empty:
            ax.text(
                0.5,
                0.5,
                "No data to visualize",
                ha="center",
                va="center",
                fontsize=14,
                color="gray",
                transform=ax.transAxes,
            )
            ax.set_axis_off()
        elif not measures:
            # Dimensions only (auto-count) — render as text table
            ax.set_axis_off()
            render_df = _cap_rows(df, shelf_config, max_rows=20)
            cell_text = render_df.iloc[:, :6].values.tolist()
            col_labels = list(render_df.columns[:6])
            table = ax.table(
                cellText=cell_text,
                colLabels=col_labels,
                loc="center",
                cellLoc="center",
            )
            table.auto_set_font_size(False)
            table.set_fontsize(9)
            table.scale(1, 1.5)
        elif mark_type == "table":
            # Table visualization - render as a matplotlib table
            ax.set_axis_off()
            render_df = _cap_rows(df, shelf_config, max_rows=20)
            cell_text = render_df.iloc[:, :6].values.tolist()
            col_labels = list(render_df.columns[:6])
            table = ax.table(
                cellText=cell_text,
                colLabels=col_labels,
                loc="center",
                cellLoc="center",
            )
            table.auto_set_font_size(False)
            table.set_fontsize(9)
            table.scale(1, 1.5)
        elif mark_type == "bar":
            render_df = _cap_rows(df, shelf_config, max_rows=50)
            # Horizontal bars when dimensions on Rows, measure on Columns
            if rows and not columns:
                _render_horizontal_bar(ax, render_df, rows, columns, measures, shelf_config)
            else:
                _render_bar(ax, render_df, rows, columns, measures, shelf_config)
        elif mark_type == "line":
            render_df = _cap_rows(df, shelf_config, max_rows=100)
            _render_line(ax, render_df, rows, columns, measures, shelf_config)
        elif mark_type == "scatter":
            render_df = _cap_rows(df, shelf_config, max_rows=100)
            _render_scatter(ax, render_df, measures, shelf_config)
        elif mark_type == "pie":
            render_df = _cap_rows(df, shelf_config, max_rows=15)
            _render_pie(ax, render_df, rows, measures)
        elif mark_type == "area":
            render_df = _cap_rows(df, shelf_config, max_rows=100)
            _render_line(ax, render_df, rows, columns, measures, shelf_config, fill=True)
        else:
            render_df = _cap_rows(df, shelf_config, max_rows=50)
            if rows and not columns:
                _render_horizontal_bar(ax, render_df, rows, columns, measures, shelf_config)
            else:
                _render_bar(ax, render_df, rows, columns, measures, shelf_config)

    # Adjust layout to leave room for nested header levels if present
    n_header_levels = getattr(ax, "__nested_header_levels", 0)
    nested_title = getattr(ax, "__nested_title", None)
    if n_header_levels > 0:
        # Leave room for group headers + suptitle
        top = 1.0 - 0.04 * (n_header_levels + 1)
        plt.tight_layout(rect=[0, 0, 1, top])
        if nested_title:
            fig.suptitle(
                nested_title,
                fontsize=11,
                fontweight="normal",
                color="#333333",
                y=top + 0.04 * n_header_levels + 0.02,
            )
    else:
        plt.tight_layout()

    try:
        buf = io.BytesIO()
        fig.savefig(buf, format=fmt, dpi=dpi, bbox_inches="tight")
        buf.seek(0)
        encoded = base64.b64encode(buf.read()).decode("ascii")
        buf.close()
        return encoded
    finally:
        plt.close(fig)
        gc.collect()


def _auto_xtick_fontsize(ax):
    """Shrink x-tick label font and increase rotation when there are many labels."""
    n = len(ax.get_xticklabels())
    if n > 40:
        ax.tick_params(axis="x", labelsize=6, rotation=90)
    elif n > 25:
        ax.tick_params(axis="x", labelsize=7, rotation=75)
    elif n > 15:
        ax.tick_params(axis="x", labelsize=8, rotation=60)


def _cap_rows(df: pd.DataFrame, shelf_config, max_rows: int = 50) -> pd.DataFrame:
    """Limit DataFrame rows to keep rendered PNGs under the ~64KB stdio pipe buffer."""
    if shelf_config.limit or len(df) <= max_rows:
        return df
    return df.head(max_rows)


def _pretty_name(col: str) -> str:
    """Format a snake_case or raw column name into a human-readable title.

    Examples: 'product_name' -> 'Product Name', 'profit_SUM' -> 'Profit SUM'
    """
    return col.replace("_", " ").strip().title()


def _composite_labels(df: pd.DataFrame, dims: list[str]) -> tuple[pd.Series, str]:
    """Build composite labels from multiple dimension columns.

    Returns (label_series, display_name) where display_name is the
    human-readable axis title (e.g. "Year / Quarter").
    """
    if len(dims) == 1:
        return df[dims[0]].astype(str), _pretty_name(dims[0])
    # Concatenate multiple dimensions
    labels = df[dims].astype(str).agg(" \u2013 ".join, axis=1)  # en-dash
    name = " / ".join(_pretty_name(d) for d in dims)
    return labels, name


def _draw_nested_headers(ax, df, dims, x_positions=None):
    """Draw Tableau-style nested hierarchical column headers.

    The innermost dimension becomes x-tick labels; each outer dimension is
    rendered as a group header strip above the axes with vertical dividers
    between groups.  Styled to match Tableau Desktop: clean text labels,
    solid full-height dividers, and a thin separator line below the header row.

    Args:
        ax: matplotlib Axes
        df: DataFrame (already sorted by outer dims via SQL ORDER BY)
        dims: list of dimension column names, outer-to-inner order
        x_positions: optional array of numeric x positions (for color-encoded
                     grouped bars where positions differ from 0..n-1)

    Returns:
        (inner_labels, inner_dim_name) tuple for use as x-tick labels
    """
    import numpy as np
    from matplotlib.transforms import blended_transform_factory

    if df.empty:
        return df[dims[-1]].astype(str), dims[-1]

    inner_dim = dims[-1]
    outer_dims = dims[:-1]  # may be 1 or more levels
    n_outer = len(outer_dims)

    inner_labels = df[inner_dim].astype(str)

    if x_positions is None:
        x_positions = np.arange(len(df))

    trans = blended_transform_factory(ax.transData, ax.transAxes)

    # For each outer dimension level (outermost drawn highest)
    for level_idx, dim in enumerate(outer_dims):
        values = df[dim].astype(str).values
        # Detect group boundaries
        groups = []  # list of (start_idx, end_idx, label)
        start = 0
        for i in range(1, len(values)):
            if values[i] != values[start]:
                groups.append((start, i - 1, values[start]))
                start = i
        groups.append((start, len(values) - 1, values[start]))

        # y position in axes fraction — stack levels above the plot
        y_frac = 1.01 + level_idx * 0.05

        for g_start, g_end, label in groups:
            # Center x position of the group
            cx = (x_positions[g_start] + x_positions[g_end]) / 2.0
            ax.text(
                cx,
                y_frac,
                label,
                transform=trans,
                ha="center",
                va="bottom",
                fontsize=10,
                fontweight="normal",
                color="#333333",
                clip_on=False,
            )

        # Draw solid vertical divider lines between groups (full height)
        for i in range(1, len(groups)):
            prev_end = groups[i - 1][1]
            curr_start = groups[i][0]
            divider_x = (x_positions[prev_end] + x_positions[curr_start]) / 2.0
            ax.axvline(
                divider_x,
                color="#bbbbbb",
                linewidth=0.8,
                linestyle="-",
            )

    # Thin horizontal separator line at the top of the plot area
    ax.axhline(
        y=ax.get_ylim()[1],
        color="#bbbbbb",
        linewidth=0.6,
    )

    ax.__nested_header_levels = n_outer
    return inner_labels, inner_dim


def _render_bar(ax, df, rows, columns, measures, shelf_config):
    """Render a bar chart."""
    import numpy as np

    if not measures:
        ax.text(
            0.5,
            0.5,
            "No measures configured",
            ha="center",
            va="center",
            fontsize=12,
            color="gray",
            transform=ax.transAxes,
        )
        return

    measure_col = f"{measures[0].field}_{measures[0].aggregation.upper()}"
    if measure_col not in df.columns:
        # Try without aggregation suffix (when no GROUP BY)
        measure_col = measures[0].field
    if measure_col not in df.columns:
        measure_col = df.columns[-1]  # last column as final fallback

    # Determine the dimension list for the primary shelf
    dims = rows if rows else (columns if columns else [])
    # Use nested headers when 2+ dims and no measure-based custom sort
    use_nested = len(dims) >= 2 and not shelf_config.sort_field

    if use_nested:
        # For color-encoded bars, defer _draw_nested_headers until we know
        # the actual center positions; just compute inner labels here.
        inner_dim = dims[-1]
        labels = df[inner_dim].astype(str)
        x_name = _pretty_name(inner_dim)
        x_name_title = " / ".join(_pretty_name(d) for d in dims)
    elif dims:
        labels, x_name = _composite_labels(df, dims)
        x_name_title = x_name
    else:
        labels = pd.Series(range(len(df))).astype(str)
        x_name = "index"
        x_name_title = x_name

    values = pd.to_numeric(df[measure_col], errors="coerce").fillna(0)

    # Color encoding
    if shelf_config.color and shelf_config.color in df.columns:
        color_groups = df[shelf_config.color].unique()

        unique_labels = labels.unique()
        x = np.arange(len(unique_labels))
        bar_width = 0.8 / len(color_groups)
        for i, group in enumerate(color_groups):
            mask = df[shelf_config.color] == group
            group_data = df.loc[mask]
            # Align values to correct label positions (groups may have different subsets)
            label_to_val = {}
            for _, row_data in group_data.iterrows():
                val = pd.to_numeric(row_data[measure_col], errors="coerce")
                if use_nested:
                    # Use inner dim label for matching
                    lbl = str(row_data[dims[-1]])
                elif dims:
                    lbl = (
                        " \u2013 ".join(str(row_data[d]) for d in dims)
                        if len(dims) > 1
                        else str(row_data[dims[0]])
                    )
                else:
                    lbl = str(row_data.name)
                label_to_val[lbl] = 0 if pd.isna(val) else val
            group_vals = [label_to_val.get(label, 0) for label in unique_labels]
            ax.bar(
                x + i * bar_width,
                group_vals,
                bar_width,
                label=str(group),
            )
        center_x = x + bar_width * (len(color_groups) - 1) / 2
        ax.set_xticks(center_x)
        ax.set_xticklabels(unique_labels, rotation=45, ha="right")
        ax.legend(title=shelf_config.color)
        # Draw nested headers with correct center positions for color-encoded bars.
        # Use a deduplicated df (one row per unique label combination) so that
        # the row count matches the length of center_x.
        if use_nested:
            dedup_df = df.drop_duplicates(subset=dims).reset_index(drop=True)
            _draw_nested_headers(ax, dedup_df, dims, x_positions=center_x)
    else:
        x_pos = np.arange(len(values))
        ax.bar(x_pos, values, color="#4e79a7")
        ax.set_xticks(x_pos)
        ax.set_xticklabels(labels, rotation=45, ha="right")
        if use_nested:
            _draw_nested_headers(ax, df, dims, x_positions=x_pos)

    ax.set_ylabel(_pretty_name(measure_col))
    if use_nested:
        ax.set_xlabel("")
        # Store title for fig.suptitle() — placed above nested headers
        ax.__nested_title = x_name_title
        ax.set_title("")  # clear axes-level title
    else:
        ax.set_xlabel(x_name)
        ax.set_title(f"{_pretty_name(measure_col)} by {x_name_title}")

    # Auto-size x-tick labels based on count
    _auto_xtick_fontsize(ax)


def _render_horizontal_bar(ax, df, rows, columns, measures, shelf_config):
    """Render a horizontal bar chart (dimension on Rows, measure on Columns)."""
    import numpy as np

    if not measures:
        ax.text(
            0.5,
            0.5,
            "No measures configured",
            ha="center",
            va="center",
            fontsize=12,
            color="gray",
            transform=ax.transAxes,
        )
        return

    measure_col = f"{measures[0].field}_{measures[0].aggregation.upper()}"
    if measure_col not in df.columns:
        measure_col = measures[0].field

    dims = rows if rows else (columns if columns else [])
    if dims:
        labels, y_name = _composite_labels(df, dims)
    else:
        labels = pd.Series(range(len(df))).astype(str)
        y_name = "index"

    values = pd.to_numeric(df[measure_col], errors="coerce").fillna(0)

    y_pos = np.arange(len(values))
    ax.barh(y_pos, values, color="#4e79a7", height=0.7)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()  # top-to-bottom like Tableau

    ax.set_xlabel(_pretty_name(measure_col))
    ax.set_ylabel(y_name)
    ax.set_title(y_name, fontsize=11, fontweight="normal", color="#333333")


def _render_line(ax, df, rows, columns, measures, shelf_config, fill=False):
    """Render a line chart (or area chart if fill=True)."""
    import numpy as np

    if not measures:
        ax.text(
            0.5,
            0.5,
            "No measures configured",
            ha="center",
            va="center",
            fontsize=12,
            color="gray",
            transform=ax.transAxes,
        )
        return

    measure_col = f"{measures[0].field}_{measures[0].aggregation.upper()}"
    if measure_col not in df.columns:
        measure_col = measures[0].field

    # Determine the dimension list for the primary shelf
    dims = rows if rows else (columns if columns else [])
    use_nested = len(dims) >= 2 and not shelf_config.sort_field

    if use_nested:
        inner_dim = dims[-1]
        x_vals = df[inner_dim].astype(str)
        x_name = _pretty_name(inner_dim)
        x_name_title = " / ".join(_pretty_name(d) for d in dims)
    elif dims:
        x_vals, x_name = _composite_labels(df, dims)
        x_name_title = x_name
    else:
        x_vals = pd.Series(range(len(df)))
        x_name = "index"
        x_name_title = x_name

    values = pd.to_numeric(df[measure_col], errors="coerce").fillna(0)

    if shelf_config.color and shelf_config.color in df.columns:
        # Collect all unique x-labels across groups for consistent axis
        all_x_labels = x_vals.unique() if hasattr(x_vals, "unique") else x_vals
        for group_name, group_df in df.groupby(shelf_config.color):
            if use_nested:
                gx = group_df[dims[-1]].astype(str)
            elif dims:
                gx, _ = _composite_labels(group_df, dims)
            else:
                gx = pd.Series(range(len(group_df)))
            gv = pd.to_numeric(group_df[measure_col], errors="coerce").fillna(0)
            ax.plot(range(len(gx)), gv, label=str(group_name), marker="o")
            if fill:
                ax.fill_between(range(len(gx)), gv, alpha=0.3)
        # Set x-tick labels once after the loop using all unique labels
        ax.set_xticks(range(len(all_x_labels)))
        ax.set_xticklabels(all_x_labels, rotation=45, ha="right")
        ax.legend(title=shelf_config.color)
    else:
        x_pos = np.arange(len(x_vals))
        ax.plot(x_pos, values, marker="o", color="#4e79a7")
        ax.set_xticks(x_pos)
        ax.set_xticklabels(x_vals, rotation=45, ha="right")
        if fill:
            ax.fill_between(x_pos, values, alpha=0.3, color="#4e79a7")

    # Draw nested headers after plotting so we have the correct axis state
    if use_nested:
        _draw_nested_headers(ax, df, dims)

    ax.set_ylabel(_pretty_name(measure_col))
    if use_nested:
        ax.set_xlabel("")
        ax.__nested_title = x_name_title
        ax.set_title("")
    else:
        ax.set_xlabel(x_name)
        ax.set_title(f"{_pretty_name(measure_col)} over {x_name_title}")
    ax.tick_params(axis="x", rotation=45)

    _auto_xtick_fontsize(ax)


def _render_scatter(ax, df, measures, shelf_config):
    """Render a scatter plot (requires 2 measures)."""
    if len(measures) < 2:
        ax.text(
            0.5,
            0.5,
            "Scatter requires 2 measures",
            ha="center",
            va="center",
            fontsize=12,
            color="gray",
            transform=ax.transAxes,
        )
        return

    x_col = f"{measures[0].field}_{measures[0].aggregation.upper()}"
    y_col = f"{measures[1].field}_{measures[1].aggregation.upper()}"
    if x_col not in df.columns:
        x_col = measures[0].field
    if y_col not in df.columns:
        y_col = measures[1].field

    x_vals = pd.to_numeric(df[x_col], errors="coerce").fillna(0)
    y_vals = pd.to_numeric(df[y_col], errors="coerce").fillna(0)

    if shelf_config.color and shelf_config.color in df.columns:
        for group_name, group_df in df.groupby(shelf_config.color):
            gx = pd.to_numeric(group_df[x_col], errors="coerce").fillna(0)
            gy = pd.to_numeric(group_df[y_col], errors="coerce").fillna(0)
            ax.scatter(gx, gy, label=str(group_name), alpha=0.7)
        ax.legend(title=shelf_config.color)
    else:
        ax.scatter(x_vals, y_vals, alpha=0.7, color="#4e79a7")

    ax.set_xlabel(x_col)
    ax.set_ylabel(y_col)
    ax.set_title(f"{y_col} vs {x_col}")


def _render_pie(ax, df, rows, measures):
    """Render a pie chart."""
    if not measures:
        ax.text(
            0.5,
            0.5,
            "No measures configured",
            ha="center",
            va="center",
            fontsize=12,
            color="gray",
            transform=ax.transAxes,
        )
        return

    measure_col = f"{measures[0].field}_{measures[0].aggregation.upper()}"
    if measure_col not in df.columns:
        measure_col = measures[0].field

    if rows:
        labels, _ = _composite_labels(df, rows)
    else:
        labels = pd.Series(range(len(df))).astype(str)

    values = pd.to_numeric(df[measure_col], errors="coerce").fillna(0)

    ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
    ax.set_title(f"{measure_col} distribution")


# ============================================================================
# Tool Implementations
# ============================================================================


async def tableau_upload_csv(
    input_data: TableauUploadCsvInput,
) -> TableauUploadCsvOutput:
    """Upload CSV data and create a queryable datasource.

    Parses the CSV, imports it into the in-memory SQLite database,
    creates a Datasource ORM record, and returns field metadata.
    """
    # Decode CSV content
    csv_text = _decode_csv_content(input_data.csv_content, input_data.file_content_base64)

    # Parse CSV with pandas
    try:
        df = pd.read_csv(io.StringIO(csv_text), skipinitialspace=True, encoding="utf-8")
    except Exception as e:
        raise ValueError(f"Failed to parse CSV: {e}")

    if df.empty:
        raise ValueError("CSV must have at least one data row")

    # Sanitize column names: keep only alphanumeric and underscores to avoid SQL syntax issues
    sanitized = [
        re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", col.lower())).strip("_") or f"col_{i}"
        for i, col in enumerate(df.columns)
    ]
    # Deduplicate: append _N suffixes, incrementing until unique
    seen: set[str] = set()
    deduped: list[str] = []
    for name in sanitized:
        if name not in seen:
            seen.add(name)
            deduped.append(name)
        else:
            counter = 1
            while f"{name}_{counter}" in seen:
                counter += 1
            unique = f"{name}_{counter}"
            seen.add(unique)
            deduped.append(unique)
    df.columns = deduped

    # Generate unique table name (suffix with short UUID to avoid collisions)
    table_name = f"{_sanitize_table_name(input_data.name)}_{uuid4().hex[:8]}"

    # Infer field types and roles
    fields: list[TableauUploadCsvFieldInfo] = []
    for col in df.columns:
        data_type = _infer_field_type(df[col])
        role = _infer_field_role(data_type)
        fields.append(TableauUploadCsvFieldInfo(name=col, data_type=data_type, role=role))

    # Persist CSV file and import into SQLite first, then create the ORM
    # record.  If file/table ops fail we have no orphaned DB row; if the ORM
    # commit fails we clean up the file and table so nothing is orphaned.
    CSV_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = CSV_STORAGE_DIR / f"{table_name}.csv"
    df.to_csv(csv_path, index=False)

    engine = get_engine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: df.to_sql(table_name, sync_conn, if_exists="replace", index=False)
            )
    except Exception:
        csv_path.unlink(missing_ok=True)
        raise

    # Create Datasource ORM record; clean up file/table on failure
    owner_id = input_data.owner_id or DEFAULT_USER_ID
    ds_id = str(uuid4())
    try:
        async with get_session() as session:
            datasource = Datasource(
                id=ds_id,
                site_id=input_data.site_id,
                name=input_data.name,
                project_id=input_data.project_id,
                owner_id=owner_id,
                connection_type="csv",
                description=f"CSV upload: {len(df)} rows, {len(df.columns)} columns",
                table_name=table_name,
            )
            session.add(datasource)
    except Exception:
        csv_path.unlink(missing_ok=True)
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))
        raise

    return TableauUploadCsvOutput(
        datasource_id=ds_id,
        table_name=table_name,
        name=input_data.name,
        fields=fields,
        row_count=len(df),
        message=f"Successfully uploaded {len(df)} rows into '{table_name}'",
    )


async def tableau_get_sheets(
    input_data: TableauGetSheetsInput,
) -> TableauGetSheetsOutput:
    """Get sheets (views) with their shelf configurations."""
    async with get_session() as session:
        stmt = select(View).where(View.site_id == input_data.site_id)
        if input_data.workbook_id:
            stmt = stmt.where(View.workbook_id == input_data.workbook_id)
        stmt = stmt.order_by(View.created_at)

        result = await session.execute(stmt)
        views = list(result.scalars().all())

        sheets: list[TableauSheetInfo] = []
        for v in views:
            shelf_config = None
            if v.shelf_config_json:
                try:
                    shelf_config = ShelfConfig(**json.loads(v.shelf_config_json))
                except Exception:
                    pass

            sheets.append(
                TableauSheetInfo(
                    id=v.id,
                    workbook_id=v.workbook_id,
                    name=v.name,
                    sheet_type=v.sheet_type,
                    datasource_id=v.datasource_id,
                    shelf_config=shelf_config,
                    created_at=v.created_at.isoformat(),
                    updated_at=v.updated_at.isoformat(),
                )
            )

        return TableauGetSheetsOutput(sheets=sheets, total_count=len(sheets))


async def tableau_list_fields(
    input_data: TableauListFieldsInput,
) -> TableauListFieldsOutput:
    """List fields from a datasource's underlying data table.

    Inspects the actual SQLite table to get column info, types,
    and distinct value counts.
    """
    # Look up the datasource
    async with get_session() as session:
        stmt = select(Datasource).where(
            Datasource.id == input_data.datasource_id,
            Datasource.site_id == input_data.site_id,
        )
        result = await session.execute(stmt)
        datasource = result.scalar_one_or_none()

    if not datasource:
        raise ValueError(f"Datasource {input_data.datasource_id} not found")
    if not datasource.table_name:
        raise ValueError(f"Datasource {input_data.datasource_id} has no associated data table")

    table_name = _validate_identifier(datasource.table_name)
    engine = get_engine()

    # Use lightweight SQL queries instead of loading the entire table
    async with engine.connect() as conn:
        # Get column info via PRAGMA
        col_rows = (await conn.execute(text(f'PRAGMA table_info("{table_name}")'))).fetchall()
        if not col_rows:
            raise ValueError(f"Table '{table_name}' not found in database")

        # Get total row count
        row_count = (await conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"'))).scalar() or 0

        fields: list[TableauFieldInfo] = []
        for col_info in col_rows:
            col_name = col_info[1]  # PRAGMA table_info: cid, name, type, notnull, dflt, pk
            _validate_identifier(col_name)
            col_type_raw = (col_info[2] or "").upper()

            # Infer type from a small sample instead of the full column
            sample_result = await conn.execute(
                text(
                    f'SELECT DISTINCT "{col_name}" FROM "{table_name}" WHERE "{col_name}" IS NOT NULL LIMIT 100'
                )
            )
            sample_vals = [r[0] for r in sample_result.fetchall()]
            if sample_vals:
                sample_series = pd.Series(sample_vals)
                data_type = _infer_field_type(sample_series)
            elif col_type_raw in ("INTEGER", "INT", "BIGINT"):
                data_type = "INTEGER"
            elif col_type_raw in ("REAL", "FLOAT", "DOUBLE", "NUMERIC"):
                data_type = "REAL"
            else:
                data_type = "STRING"

            role = _infer_field_role(data_type)

            # Nullable check
            null_count = (
                await conn.execute(
                    text(f'SELECT COUNT(*) FROM "{table_name}" WHERE "{col_name}" IS NULL')
                )
            ).scalar() or 0
            nullable = null_count > 0

            # Distinct count
            distinct_count = (
                await conn.execute(text(f'SELECT COUNT(DISTINCT "{col_name}") FROM "{table_name}"'))
            ).scalar() or 0

            # Sample values (up to 10)
            sample_result = await conn.execute(
                text(
                    f'SELECT DISTINCT "{col_name}" FROM "{table_name}" WHERE "{col_name}" IS NOT NULL LIMIT 10'
                )
            )
            sample_values = [r[0] for r in sample_result.fetchall()]

            fields.append(
                TableauFieldInfo(
                    name=col_name,
                    data_type=data_type,
                    role=role,
                    nullable=nullable,
                    distinct_count=int(distinct_count),
                    sample_values=sample_values,
                )
            )

    return TableauListFieldsOutput(
        datasource_id=input_data.datasource_id,
        table_name=table_name,
        fields=fields,
        row_count=int(row_count),
    )


async def tableau_configure_shelf(
    input_data: TableauConfigureShelfInput,
) -> TableauConfigureShelfOutput:
    """Configure the shelf layout for a view.

    Validates the shelf config against the datasource's fields,
    stores it on the View record, and returns the generated SQL preview.
    """
    shelf_config = input_data.shelf_config

    # Validate datasource exists and belongs to the same site.
    # Extract scalar values before leaving session scope to avoid detached object access.
    async with get_session() as session:
        ds_stmt = select(Datasource).where(
            Datasource.id == shelf_config.datasource_id,
            Datasource.site_id == input_data.site_id,
        )
        ds_result = await session.execute(ds_stmt)
        datasource = ds_result.scalar_one_or_none()
        if not datasource:
            raise ValueError(f"Datasource {shelf_config.datasource_id} not found")
        if not datasource.table_name:
            raise ValueError(f"Datasource {shelf_config.datasource_id} has no data table")
        ds_table_name = datasource.table_name

    # Validate table name before using in queries
    if not re.match(r"^[a-zA-Z0-9_]+$", ds_table_name):
        raise ValueError(f"Invalid table name: {ds_table_name!r}")

    # Validate fields exist in the table
    engine = get_engine()
    async with engine.connect() as conn:
        table_columns = await conn.run_sync(
            lambda sync_conn: [c["name"] for c in inspect(sync_conn).get_columns(ds_table_name)]
        )

    all_fields = (
        shelf_config.rows
        + shelf_config.columns
        + [m.field for m in shelf_config.measures]
        + [f.field for f in shelf_config.filters]
    )
    if shelf_config.color:
        all_fields.append(shelf_config.color)
    if shelf_config.size:
        all_fields.append(shelf_config.size)
    if shelf_config.label:
        all_fields.append(shelf_config.label)
    # Note: sort_field is NOT validated here because it may reference a
    # computed measure alias (e.g. "revenue_SUM") that doesn't exist as a raw
    # table column.  build_query() validates it against the actual result-set
    # column names instead.

    invalid_fields = [f for f in all_fields if f and f not in table_columns]
    if invalid_fields:
        raise ValueError(
            f"Fields not found in table '{ds_table_name}': {invalid_fields}. "
            f"Available: {table_columns}"
        )

    # Generate SQL preview
    generated_sql, _ = build_query(shelf_config, ds_table_name)

    # Store shelf config on the view
    async with get_session() as session:
        view_stmt = select(View).where(
            View.id == input_data.view_id, View.site_id == input_data.site_id
        )
        view_result = await session.execute(view_stmt)
        view = view_result.scalar_one_or_none()

        if not view:
            raise ValueError(f"View {input_data.view_id} not found")

        view.shelf_config_json = json.dumps(shelf_config.model_dump())
        view.datasource_id = shelf_config.datasource_id
        await session.flush()

    return TableauConfigureShelfOutput(
        view_id=input_data.view_id,
        shelf_config=shelf_config,
        generated_sql=generated_sql,
        message="Shelf configuration saved successfully",
    )


async def tableau_create_visualization(
    input_data: TableauCreateVisualizationInput,
) -> TableauCreateVisualizationOutput:
    """Generate a visualization from the view's shelf configuration.

    Reads the shelf config, generates SQL, executes it against the
    in-memory SQLite database, and renders a chart image.
    """
    # Load view + datasource in a single session to avoid extra round-trip
    async with get_session() as session:
        view_stmt = select(View).where(
            View.id == input_data.view_id, View.site_id == input_data.site_id
        )
        view_result = await session.execute(view_stmt)
        view = view_result.scalar_one_or_none()
        if not view:
            raise ValueError(f"View {input_data.view_id} not found")
        if not view.shelf_config_json:
            raise ValueError(
                f"View {input_data.view_id} has no shelf configuration. "
                "Use tableau_configure_shelf first."
            )
        if not view.datasource_id:
            raise ValueError(f"View {input_data.view_id} has no associated datasource")
        view_shelf_config_json = view.shelf_config_json

        ds_stmt = select(Datasource).where(
            Datasource.id == view.datasource_id,
            Datasource.site_id == input_data.site_id,
        )
        ds_result = await session.execute(ds_stmt)
        datasource = ds_result.scalar_one_or_none()
        if not datasource or not datasource.table_name:
            raise ValueError(f"Datasource {view.datasource_id} not found or has no data table")
        ds_table_name = datasource.table_name

    # Parse shelf config
    try:
        shelf_config = ShelfConfig(**json.loads(view_shelf_config_json))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"Malformed shelf configuration: {exc}") from exc

    # Build and execute query
    sql, params = build_query(shelf_config, ds_table_name)

    # Push a SQL LIMIT when the user didn't set one explicitly.
    # Without this, high-cardinality GROUP BYs (e.g. product × category)
    # fetch thousands of rows before _cap_rows truncates in Python,
    # causing timeouts on resource-constrained machines.
    if not shelf_config.limit and not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
        mark = shelf_config.mark_type.lower()
        query_limit = {
            "pie": 15,
            "table": 20,
            "bar": 50,
            "scatter": 100,
            "line": 100,
            "area": 100,
        }.get(mark, 50)
        sql += " LIMIT :_auto_limit"
        params["_auto_limit"] = query_limit

    engine = get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params)
        rows_raw = result.fetchall()
        headers = list(result.keys())

    # Convert to lists for JSON serialization
    rows = [list(r) for r in rows_raw]
    del rows_raw  # free raw result tuples immediately

    # Render chart image
    image_base64 = None
    content_type = "image/png"
    if rows:
        try:
            df = pd.DataFrame(rows, columns=headers)
            image_base64 = render_chart(
                df,
                shelf_config,
                width=input_data.width,
                height=input_data.height,
                fmt=input_data.format,
            )
            del df  # free DataFrame before building response
            if input_data.format == "svg":
                content_type = "image/svg+xml"
        except Exception as e:
            # Chart rendering is best-effort; return data even if chart fails
            logger.warning("Chart rendering failed: %s", e)
            image_base64 = None

    viz_data = TableauVisualizationData(headers=headers, rows=rows, row_count=len(rows))

    return TableauCreateVisualizationOutput(
        view_id=input_data.view_id,
        chart_type=shelf_config.mark_type,
        generated_sql=sql,
        data=viz_data,
        image_base64=image_base64,
        content_type=content_type,
        message=f"Generated {shelf_config.mark_type} chart with {len(rows)} data points",
    )


async def tableau_create_sheet(
    input_data: TableauCreateSheetInput,
) -> TableauCreateSheetOutput:
    """Create a new sheet (Workbook + View) linked to a datasource.

    Creates a Workbook ORM record and a View ORM record with the
    datasource_id set, returning the view_id for use with
    tableau_configure_shelf and tableau_create_visualization.
    """
    # Validate datasource exists and extract scalar values before leaving session scope
    async with get_session() as session:
        ds_stmt = select(Datasource).where(
            Datasource.id == input_data.datasource_id,
            Datasource.site_id == input_data.site_id,
        )
        ds_result = await session.execute(ds_stmt)
        datasource = ds_result.scalar_one_or_none()
        if not datasource:
            raise ValueError(f"Datasource {input_data.datasource_id} not found")
        ds_project_id = datasource.project_id
        ds_owner_id = datasource.owner_id

    # Create Workbook
    workbook_id = str(uuid4())
    view_id = str(uuid4())

    async with get_session() as session:
        workbook = Workbook(
            id=workbook_id,
            site_id=input_data.site_id,
            name=input_data.name,
            project_id=ds_project_id,
            owner_id=ds_owner_id,
            description=f"Auto-created workbook for sheet '{input_data.name}'",
        )
        session.add(workbook)
        await session.flush()

        view = View(
            id=view_id,
            site_id=input_data.site_id,
            workbook_id=workbook_id,
            name=input_data.name,
            sheet_type="worksheet",
            datasource_id=input_data.datasource_id,
        )
        session.add(view)

        wb_ds = WorkbookDatasource(
            id=str(uuid4()),
            workbook_id=workbook_id,
            datasource_id=input_data.datasource_id,
        )
        session.add(wb_ds)
        await session.flush()

    return TableauCreateSheetOutput(
        view_id=view_id,
        workbook_id=workbook_id,
        name=input_data.name,
        datasource_id=input_data.datasource_id,
        message=f"Created sheet '{input_data.name}' with view_id={view_id}",
    )
