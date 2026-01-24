import re
from re import Pattern
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SheetDefinition(BaseModel):
    """Structured definition for a worksheet."""

    model_config = ConfigDict(extra="forbid")

    name: str
    headers: list[Any] | None = None
    rows: list[list[Any]] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Sheet name must not be empty")
        return value

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value: list[Any] | None) -> list[Any] | None:
        if value is None:
            return None
        if not all(
            isinstance(item, (str, int, float, bool, type(None))) for item in value
        ):
            raise ValueError("Headers must contain only simple values")
        return value

    @field_validator("rows")
    @classmethod
    def _validate_rows(cls, value: list[list[Any]]) -> list[list[Any]]:
        for index, row in enumerate(value):
            if not isinstance(row, list):
                raise ValueError(f"Row {index} must be provided as a list")
        return value


class SheetData(BaseModel):
    """Sheet data definition for adding data to a worksheet (without name)."""

    model_config = ConfigDict(extra="forbid")

    headers: list[Any] | None = None
    rows: list[list[Any]] = Field(default_factory=list)

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value: list[Any] | None) -> list[Any] | None:
        if value is None:
            return None
        if not all(
            isinstance(item, (str, int, float, bool, type(None))) for item in value
        ):
            raise ValueError("Headers must contain only simple values")
        return value

    @field_validator("rows")
    @classmethod
    def _validate_rows(cls, value: list[list[Any]]) -> list[list[Any]]:
        for index, row in enumerate(value):
            if not isinstance(row, list):
                raise ValueError(f"Row {index} must be provided as a list")
        return value


class SetCellOperation(BaseModel):
    """Operation to set a specific cell value."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["set_cell"]
    sheet: str
    cell: str
    value: Any

    _CELL_PATTERN: ClassVar[Pattern[str]] = re.compile(r"^[A-Za-z]+[1-9][0-9]*$")

    @field_validator("sheet")
    @classmethod
    def _validate_sheet(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Sheet name must not be empty")
        return value

    @field_validator("cell")
    @classmethod
    def _validate_cell(cls, value: str) -> str:
        if not cls._CELL_PATTERN.match(value):
            raise ValueError("Cell must be an Spreadsheets reference like 'A1'")
        return value.upper()


class AppendRowsOperation(BaseModel):
    """Operation to append one or many rows to a sheet."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["append_rows"]
    sheet: str
    rows: list[list[Any]] = Field(default_factory=list)

    @field_validator("sheet")
    @classmethod
    def _validate_sheet(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Sheet name must not be empty")
        return value

    @field_validator("rows")
    @classmethod
    def _validate_rows(cls, value: list[list[Any]]) -> list[list[Any]]:
        for index, row in enumerate(value):
            if not isinstance(row, list):
                raise ValueError(f"Row {index} must be provided as a list")
        return value


class RenameSheetOperation(BaseModel):
    """Operation to rename a sheet."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["rename_sheet"]
    sheet: str
    new_name: str

    @field_validator("sheet", "new_name")
    @classmethod
    def _validate_names(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Sheet name must not be empty")
        return value


HORIZONTAL_ALIGNMENTS = {
    "left",
    "center",
    "right",
    "justify",
    "general",
    "fill",
    "centerContinuous",
    "distributed",
}
VERTICAL_ALIGNMENTS = {"top", "center", "bottom", "justify", "distributed"}
BORDER_STYLES = {
    "thin",
    "medium",
    "thick",
    "double",
    "dotted",
    "dashed",
    "hair",
    "mediumDashed",
    "dashDot",
    "mediumDashDot",
    "dashDotDot",
    "slantDashDot",
}
FILL_PATTERNS = {
    "solid",
    "lightGray",
    "mediumGray",
    "darkGray",
    "gray125",
    "gray0625",
    "lightDown",
    "lightUp",
    "darkDown",
    "darkUp",
    "darkGrid",
    "darkTrellis",
    "lightGrid",
    "lightTrellis",
    "darkHorizontal",
    "darkVertical",
    "lightHorizontal",
    "lightVertical",
}
BORDER_SIDES = {"left", "right", "top", "bottom"}


class FormatCellsOperation(BaseModel):
    """Operation to format cells (font, colors, alignment, borders)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["format_cells"]
    sheet: str
    range: str  # e.g., "A1", "A1:B5", "A:A" (column), "1:1" (row)

    # Font properties
    font_name: str | None = None
    font_size: int | None = None
    font_bold: bool | None = None
    font_italic: bool | None = None
    font_underline: bool | None = None
    font_color: str | None = None  # Hex color like "FF0000" or "#FF0000"

    fill_color: str | None = None  # Hex color for cell background
    fill_pattern: str | None = None  # "solid", "lightGray", "darkGray", etc.

    horizontal_alignment: str | None = None  # "left", "center", "right", "justify"
    vertical_alignment: str | None = None  # "top", "center", "bottom"
    wrap_text: bool | None = None

    border_style: str | None = None  # "thin", "medium", "thick", etc.
    border_color: str | None = None  # Hex color for borders
    border_sides: list[str] | None = None  # ["left", "right", "top", "bottom"]

    _RANGE_PATTERN: ClassVar[Pattern[str]] = re.compile(
        r"^([A-Za-z]+[1-9][0-9]*(:[A-Za-z]+[1-9][0-9]*)?|[A-Za-z]+:[A-Za-z]+|[1-9][0-9]*:[1-9][0-9]*)$"
    )

    @field_validator("sheet")
    @classmethod
    def _validate_sheet(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Sheet name must not be empty")
        return value

    @field_validator("range")
    @classmethod
    def _validate_range(cls, value: str) -> str:
        if not cls._RANGE_PATTERN.match(value):
            raise ValueError(
                "Range must be a cell reference like 'A1', 'A1:B5', 'A:A', or '1:5'"
            )
        return value.upper()

    @field_validator("font_color", "fill_color", "border_color")
    @classmethod
    def _validate_color(cls, value: str | None) -> str | None:
        if value is None:
            return None
        s = value.strip().lstrip("#").upper()
        if len(s) not in (6, 8):
            raise ValueError(
                f"Color must be a 6 or 8 hex digit string like 'FF0000' or '#FF0000', got: {value}"
            )
        try:
            int(s, 16)
        except ValueError as e:
            raise ValueError(f"Invalid hex color: {value}") from e
        return s

    @field_validator("horizontal_alignment")
    @classmethod
    def _validate_horizontal(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # Case-insensitive lookup preserving original case for openpyxl
        lower_alignments = {a.lower(): a for a in HORIZONTAL_ALIGNMENTS}
        if value.lower() not in lower_alignments:
            raise ValueError(
                f"horizontal_alignment must be one of: {sorted(HORIZONTAL_ALIGNMENTS)}"
            )
        return lower_alignments[value.lower()]

    @field_validator("vertical_alignment")
    @classmethod
    def _validate_vertical(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.lower() not in VERTICAL_ALIGNMENTS:
            raise ValueError(
                f"vertical_alignment must be one of: {sorted(VERTICAL_ALIGNMENTS)}"
            )
        return value.lower()

    @field_validator("border_style")
    @classmethod
    def _validate_border_style(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # Case-insensitive lookup preserving original case for openpyxl
        lower_styles = {s.lower(): s for s in BORDER_STYLES}
        if value.lower() not in lower_styles:
            raise ValueError(f"border_style must be one of: {sorted(BORDER_STYLES)}")
        return lower_styles[value.lower()]

    @field_validator("fill_pattern")
    @classmethod
    def _validate_fill_pattern(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lower_patterns = {p.lower(): p for p in FILL_PATTERNS}
        if value.lower() not in lower_patterns:
            raise ValueError(f"fill_pattern must be one of: {sorted(FILL_PATTERNS)}")
        return lower_patterns[value.lower()]

    @field_validator("border_sides")
    @classmethod
    def _validate_border_sides(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = []
        for side in value:
            if side.lower() not in BORDER_SIDES:
                raise ValueError(
                    f"border_sides must contain only: {sorted(BORDER_SIDES)}"
                )
            normalized.append(side.lower())
        return normalized


class MergeCellsOperation(BaseModel):
    """Operation to merge cells."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["merge_cells"]
    sheet: str
    range: str  # e.g., "A1:D1"

    _RANGE_PATTERN: ClassVar[Pattern[str]] = re.compile(
        r"^[A-Za-z]+[1-9][0-9]*:[A-Za-z]+[1-9][0-9]*$"
    )

    @field_validator("sheet")
    @classmethod
    def _validate_sheet(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Sheet name must not be empty")
        return value

    @field_validator("range")
    @classmethod
    def _validate_range(cls, value: str) -> str:
        if not cls._RANGE_PATTERN.match(value):
            raise ValueError("Range must be like 'A1:D1'")
        return value.upper()


class UnmergeCellsOperation(BaseModel):
    """Operation to unmerge cells."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["unmerge_cells"]
    sheet: str
    range: str  # e.g., "A1:D1"

    _RANGE_PATTERN: ClassVar[Pattern[str]] = re.compile(
        r"^[A-Za-z]+[1-9][0-9]*:[A-Za-z]+[1-9][0-9]*$"
    )

    @field_validator("sheet")
    @classmethod
    def _validate_sheet(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Sheet name must not be empty")
        return value

    @field_validator("range")
    @classmethod
    def _validate_range(cls, value: str) -> str:
        if not cls._RANGE_PATTERN.match(value):
            raise ValueError("Range must be like 'A1:D1'")
        return value.upper()


class SetColumnWidthOperation(BaseModel):
    """Operation to set column width."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["set_column_width"]
    sheet: str
    column: str  # e.g., "A", "B", "AA"
    width: float

    _COLUMN_PATTERN: ClassVar[Pattern[str]] = re.compile(r"^[A-Za-z]+$")

    @field_validator("sheet")
    @classmethod
    def _validate_sheet(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Sheet name must not be empty")
        return value

    @field_validator("column")
    @classmethod
    def _validate_column(cls, value: str) -> str:
        if not cls._COLUMN_PATTERN.match(value):
            raise ValueError("Column must be a letter like 'A' or 'AA'")
        return value.upper()

    @field_validator("width")
    @classmethod
    def _validate_width(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("Width must be positive")
        if value > 255:
            raise ValueError("Width must not exceed 255")
        return value


class SetRowHeightOperation(BaseModel):
    """Operation to set row height."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["set_row_height"]
    sheet: str
    row: int
    height: float

    @field_validator("sheet")
    @classmethod
    def _validate_sheet(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Sheet name must not be empty")
        return value

    @field_validator("row")
    @classmethod
    def _validate_row(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Row must be at least 1")
        return value

    @field_validator("height")
    @classmethod
    def _validate_height(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("Height must be positive")
        if value > 409:
            raise ValueError("Height must not exceed 409")
        return value


class FreezePanesOperation(BaseModel):
    """Operation to freeze panes at a specific cell."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["freeze_panes"]
    sheet: str
    cell: str | None = None  # e.g., "B2" freezes row 1 and column A. None to unfreeze.

    _CELL_PATTERN: ClassVar[Pattern[str]] = re.compile(r"^[A-Za-z]+[1-9][0-9]*$")

    @field_validator("sheet")
    @classmethod
    def _validate_sheet(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Sheet name must not be empty")
        return value

    @field_validator("cell")
    @classmethod
    def _validate_cell(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not cls._CELL_PATTERN.match(value):
            raise ValueError("Cell must be like 'A1' or 'B2'")
        return value.upper()


class AddNamedRangeOperation(BaseModel):
    """Operation to add a named range."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["add_named_range"]
    name: str
    sheet: str
    range: str  # e.g., "A1:B10"

    _RANGE_PATTERN: ClassVar[Pattern[str]] = re.compile(
        r"^[A-Za-z]+[1-9][0-9]*:[A-Za-z]+[1-9][0-9]*$"
    )
    _NAME_PATTERN: ClassVar[Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Name must not be empty")
        if not cls._NAME_PATTERN.match(value):
            raise ValueError(
                "Name must start with a letter or underscore, "
                "followed by letters, digits, underscores, or dots"
            )
        return value

    @field_validator("sheet")
    @classmethod
    def _validate_sheet(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Sheet name must not be empty")
        return value

    @field_validator("range")
    @classmethod
    def _validate_range(cls, value: str) -> str:
        if not cls._RANGE_PATTERN.match(value):
            raise ValueError("Range must be like 'A1:B10'")
        return value.upper()


class DeleteNamedRangeOperation(BaseModel):
    """Operation to delete a named range."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["delete_named_range"]
    name: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Name must not be empty")
        return value


# Canonical forms for validation types and operators (camelCase as expected by openpyxl)
_VALIDATION_TYPES_CANONICAL = [
    "list",
    "whole",
    "decimal",
    "date",
    "time",
    "textLength",
    "custom",
]
_VALIDATION_OPERATORS_CANONICAL = [
    "between",
    "notBetween",
    "equal",
    "notEqual",
    "lessThan",
    "lessThanOrEqual",
    "greaterThan",
    "greaterThanOrEqual",
]
# Lookup maps for case-insensitive validation
VALIDATION_TYPES_MAP = {v.lower(): v for v in _VALIDATION_TYPES_CANONICAL}
VALIDATION_OPERATORS_MAP = {v.lower(): v for v in _VALIDATION_OPERATORS_CANONICAL}


class AddDataValidationOperation(BaseModel):
    """Operation to add data validation to cells."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["add_data_validation"]
    sheet: str
    range: str  # e.g., "A1:A10"
    validation_type: (
        str  # "list", "whole", "decimal", "date", "time", "textLength", "custom"
    )
    operator: str | None = None  # "between", "equal", "lessThan", etc.
    formula1: str | None = None  # For list: comma-separated values or range
    formula2: str | None = None  # For between/notBetween
    allow_blank: bool = True
    show_error_message: bool = True
    error_title: str | None = None
    error_message: str | None = None
    show_input_message: bool = False
    input_title: str | None = None
    input_message: str | None = None

    _RANGE_PATTERN: ClassVar[Pattern[str]] = re.compile(
        r"^([A-Za-z]+[1-9][0-9]*(:[A-Za-z]+[1-9][0-9]*)?|[A-Za-z]+:[A-Za-z]+|[1-9][0-9]*:[1-9][0-9]*)$"
    )

    @field_validator("sheet")
    @classmethod
    def _validate_sheet(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Sheet name must not be empty")
        return value

    @field_validator("range")
    @classmethod
    def _validate_range(cls, value: str) -> str:
        if not cls._RANGE_PATTERN.match(value):
            raise ValueError("Range must be like 'A1', 'A1:B10', 'A:A', or '1:5'")
        return value.upper()

    @field_validator("validation_type")
    @classmethod
    def _validate_validation_type(cls, value: str) -> str:
        lower_value = value.lower()
        if lower_value not in VALIDATION_TYPES_MAP:
            raise ValueError(
                f"validation_type must be one of: {sorted(_VALIDATION_TYPES_CANONICAL)}"
            )
        return VALIDATION_TYPES_MAP[lower_value]

    @field_validator("operator")
    @classmethod
    def _validate_operator(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lower_value = value.lower()
        if lower_value not in VALIDATION_OPERATORS_MAP:
            raise ValueError(
                f"operator must be one of: {sorted(_VALIDATION_OPERATORS_CANONICAL)}"
            )
        return VALIDATION_OPERATORS_MAP[lower_value]


# Canonical forms for conditional format types (camelCase as expected by openpyxl)
_CONDITIONAL_FORMAT_TYPES_CANONICAL = [
    "cellIs",
    "colorScale",
    "dataBar",
    "expression",
    "top10",
    "aboveAverage",
    "duplicateValues",
    "uniqueValues",
    "containsText",
    "notContainsText",
    "beginsWith",
    "endsWith",
    "containsBlanks",
    "notContainsBlanks",
]
# Lookup map for case-insensitive validation
CONDITIONAL_FORMAT_TYPES_MAP = {
    v.lower(): v for v in _CONDITIONAL_FORMAT_TYPES_CANONICAL
}
# Keep the set for backwards compatibility (if used elsewhere)
CONDITIONAL_FORMAT_TYPES = set(_CONDITIONAL_FORMAT_TYPES_CANONICAL)


class AddConditionalFormattingOperation(BaseModel):
    """Operation to add conditional formatting."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["add_conditional_formatting"]
    sheet: str
    range: str  # e.g., "A1:A10"
    rule_type: str  # "cellIs", "colorScale", "dataBar", "expression", etc.
    operator: str | None = None  # "greaterThan", "lessThan", "equal", "between", etc.
    formula: str | None = None  # Formula or value to compare
    formula2: str | None = None  # Second formula for "between"
    # Formatting options
    font_color: str | None = None
    fill_color: str | None = None
    font_bold: bool | None = None
    font_italic: bool | None = None
    # Color scale options (for colorScale rule_type)
    color_scale_colors: list[str] | None = None  # e.g., ["FF0000", "FFFF00", "00FF00"]
    # Data bar options (for dataBar rule_type)
    data_bar_color: str | None = None
    # Top/bottom options
    rank: int | None = None  # For top10 rule
    percent: bool | None = None  # For top10 rule
    # Text options
    text: str | None = None  # For containsText, beginsWith, endsWith

    _RANGE_PATTERN: ClassVar[Pattern[str]] = re.compile(
        r"^([A-Za-z]+[1-9][0-9]*(:[A-Za-z]+[1-9][0-9]*)?|[A-Za-z]+:[A-Za-z]+|[1-9][0-9]*:[1-9][0-9]*)$"
    )

    @field_validator("sheet")
    @classmethod
    def _validate_sheet(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Sheet name must not be empty")
        return value

    @field_validator("range")
    @classmethod
    def _validate_range(cls, value: str) -> str:
        if not cls._RANGE_PATTERN.match(value):
            raise ValueError("Range must be like 'A1', 'A1:B10', 'A:A', or '1:5'")
        return value.upper()

    @field_validator("rule_type")
    @classmethod
    def _validate_rule_type(cls, value: str) -> str:
        lower_value = value.lower()
        if lower_value not in CONDITIONAL_FORMAT_TYPES_MAP:
            raise ValueError(
                f"rule_type must be one of: {sorted(_CONDITIONAL_FORMAT_TYPES_CANONICAL)}"
            )
        return CONDITIONAL_FORMAT_TYPES_MAP[lower_value]

    @field_validator("operator")
    @classmethod
    def _validate_operator(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lower_value = value.lower()
        if lower_value not in VALIDATION_OPERATORS_MAP:
            raise ValueError(
                f"operator must be one of: {sorted(_VALIDATION_OPERATORS_CANONICAL)}"
            )
        return VALIDATION_OPERATORS_MAP[lower_value]

    @field_validator("font_color", "fill_color", "data_bar_color")
    @classmethod
    def _validate_color(cls, value: str | None) -> str | None:
        if value is None:
            return None
        s = value.strip().lstrip("#").upper()
        if len(s) not in (6, 8):
            raise ValueError(f"Color must be 6 or 8 hex digits, got: {value}")
        try:
            int(s, 16)
        except ValueError as e:
            raise ValueError(f"Invalid hex color: {value}") from e
        return s

    @field_validator("color_scale_colors")
    @classmethod
    def _validate_color_scale_colors(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(value) < 2 or len(value) > 3:
            raise ValueError("color_scale_colors must have 2 or 3 colors")
        validated = []
        for color in value:
            s = color.strip().lstrip("#").upper()
            if len(s) not in (6, 8):
                raise ValueError(f"Color must be 6 or 8 hex digits, got: {color}")
            try:
                int(s, 16)
            except ValueError as e:
                raise ValueError(f"Invalid hex color: {color}") from e
            validated.append(s)
        return validated


class SetAutoFilterOperation(BaseModel):
    """Operation to set auto-filter on a range."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["set_auto_filter"]
    sheet: str
    range: str | None = None  # e.g., "A1:D10". None to remove auto-filter.

    _RANGE_PATTERN: ClassVar[Pattern[str]] = re.compile(
        r"^[A-Za-z]+[1-9][0-9]*:[A-Za-z]+[1-9][0-9]*$"
    )

    @field_validator("sheet")
    @classmethod
    def _validate_sheet(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Sheet name must not be empty")
        return value

    @field_validator("range")
    @classmethod
    def _validate_range(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not cls._RANGE_PATTERN.match(value):
            raise ValueError("Range must be like 'A1:D10'")
        return value.upper()


class SetNumberFormatOperation(BaseModel):
    """Operation to set number format on cells."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["set_number_format"]
    sheet: str
    range: str  # e.g., "A1:A10"
    format: str  # e.g., "#,##0.00", "0%", "yyyy-mm-dd"

    _RANGE_PATTERN: ClassVar[Pattern[str]] = re.compile(
        r"^([A-Za-z]+[1-9][0-9]*(:[A-Za-z]+[1-9][0-9]*)?|[A-Za-z]+:[A-Za-z]+|[1-9][0-9]*:[1-9][0-9]*)$"
    )

    @field_validator("sheet")
    @classmethod
    def _validate_sheet(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Sheet name must not be empty")
        return value

    @field_validator("range")
    @classmethod
    def _validate_range(cls, value: str) -> str:
        if not cls._RANGE_PATTERN.match(value):
            raise ValueError("Range must be like 'A1', 'A1:B10', 'A:A', or '1:5'")
        return value.upper()

    @field_validator("format")
    @classmethod
    def _validate_format(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Format must not be empty")
        return value


class AddImageOperation(BaseModel):
    """Operation to add an image to a worksheet."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["add_image"]
    sheet: str
    image_path: str  # Path to the image file
    cell: str  # e.g., "A1" - anchor position
    width: int | None = None  # Width in pixels
    height: int | None = None  # Height in pixels

    _CELL_PATTERN: ClassVar[Pattern[str]] = re.compile(r"^[A-Za-z]+[1-9][0-9]*$")

    @field_validator("sheet")
    @classmethod
    def _validate_sheet(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Sheet name must not be empty")
        return value

    @field_validator("cell")
    @classmethod
    def _validate_cell(cls, value: str) -> str:
        if not cls._CELL_PATTERN.match(value):
            raise ValueError("Cell must be like 'A1'")
        return value.upper()

    @field_validator("image_path")
    @classmethod
    def _validate_image_path(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Image path must not be empty")
        return value

    @field_validator("width", "height")
    @classmethod
    def _validate_dimension(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value <= 0:
            raise ValueError("Dimension must be positive")
        return value


SheetUpdateOperation = Annotated[
    SetCellOperation
    | AppendRowsOperation
    | RenameSheetOperation
    | FormatCellsOperation
    | MergeCellsOperation
    | UnmergeCellsOperation
    | SetColumnWidthOperation
    | SetRowHeightOperation
    | FreezePanesOperation
    | AddNamedRangeOperation
    | DeleteNamedRangeOperation
    | AddDataValidationOperation
    | AddConditionalFormattingOperation
    | SetAutoFilterOperation
    | SetNumberFormatOperation
    | AddImageOperation,
    Field(discriminator="type"),
]
