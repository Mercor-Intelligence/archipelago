from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BaseSlideOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UpdateSlideTitleOperation(BaseSlideOperation):
    type: Literal["update_slide_title"]
    index: int
    title: str

    @field_validator("index")
    @classmethod
    def _validate_index(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Slide index must be non-negative")
        return value

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        if not value:
            raise ValueError("Title must not be empty")
        return value


class UpdateSlideSubtitleOperation(BaseSlideOperation):
    type: Literal["update_slide_subtitle"]
    index: int
    subtitle: str

    @field_validator("index")
    @classmethod
    def _validate_index(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Slide index must be non-negative")
        return value

    @field_validator("subtitle")
    @classmethod
    def _validate_subtitle(cls, value: str) -> str:
        if not value:
            raise ValueError("Subtitle must not be empty")
        return value


class SetBulletsOperation(BaseSlideOperation):
    type: Literal["set_bullets"]
    index: int
    placeholder: Literal["body", "left", "right"] = "body"
    items: list[str]

    @field_validator("index")
    @classmethod
    def _validate_index(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Slide index must be non-negative")
        return value

    @field_validator("items")
    @classmethod
    def _validate_items(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("Bullet list must contain at least one item")
        return value


class AppendBulletsOperation(BaseSlideOperation):
    type: Literal["append_bullets"]
    index: int
    placeholder: Literal["body", "left", "right"] = "body"
    items: list[str]

    @field_validator("index")
    @classmethod
    def _validate_index(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Slide index must be non-negative")
        return value

    @field_validator("items")
    @classmethod
    def _validate_items(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("Bullet list must contain at least one item")
        return value


class ClearPlaceholderOperation(BaseSlideOperation):
    type: Literal["clear_placeholder"]
    index: int
    placeholder: Literal["body", "left", "right"] = "body"

    @field_validator("index")
    @classmethod
    def _validate_index(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Slide index must be non-negative")
        return value


class ReplaceTextOperation(BaseSlideOperation):
    type: Literal["replace_text"]
    search: str
    replace: str
    match_case: bool = False

    @field_validator("search")
    @classmethod
    def _validate_search(cls, value: str) -> str:
        if not value:
            raise ValueError("Search text must not be empty")
        return value


class AppendTableOperation(BaseSlideOperation):
    type: Literal["append_table"]
    index: int
    placeholder: Literal["body", "left", "right"] = "body"
    rows: list[list[str]]
    header: bool = True

    @field_validator("index")
    @classmethod
    def _validate_index(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Slide index must be non-negative")
        return value

    @field_validator("rows")
    @classmethod
    def _validate_rows(cls, value: list[list[str]]) -> list[list[str]]:
        if not value:
            raise ValueError("Table must contain at least one row")
        column_count: int | None = None
        for row_index, row in enumerate(value):
            if not row:
                raise ValueError(
                    f"Table row {row_index} must contain at least one cell"
                )
            if column_count is None:
                column_count = len(row)
            elif len(row) != column_count:
                raise ValueError("All table rows must have the same number of cells")
        return value


class UpdateTableCellOperation(BaseSlideOperation):
    type: Literal["update_table_cell"]
    index: int
    table_idx: int
    row: int
    column: int
    text: str

    @field_validator("index", "table_idx", "row", "column")
    @classmethod
    def _validate_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Indices must be non-negative")
        return value


class DeleteSlideOperation(BaseSlideOperation):
    type: Literal["delete_slide"]
    index: int

    @field_validator("index")
    @classmethod
    def _validate_index(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Slide index must be non-negative")
        return value


class DuplicateSlideOperation(BaseSlideOperation):
    type: Literal["duplicate_slide"]
    index: int
    position: Literal["after", "end"] = "after"

    @field_validator("index")
    @classmethod
    def _validate_index(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Slide index must be non-negative")
        return value


class SetNotesOperation(BaseSlideOperation):
    type: Literal["set_notes"]
    index: int
    notes: str

    @field_validator("index")
    @classmethod
    def _validate_index(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Slide index must be non-negative")
        return value


class ApplyTextFormattingOperation(BaseSlideOperation):
    """Operation to apply text formatting to a placeholder or specific paragraph/run."""

    type: Literal["apply_text_formatting"]
    index: int
    placeholder: Literal["title", "body", "left", "right"] = "body"
    paragraph_index: int | None = None
    run_index: int | None = None
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    font_size: float | int | None = None
    font_color: str | None = None
    font_name: str | None = None
    alignment: Literal["left", "center", "right", "justify"] | None = None

    @field_validator("index")
    @classmethod
    def _validate_index(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Slide index must be non-negative")
        return value

    @field_validator("paragraph_index", "run_index")
    @classmethod
    def _validate_optional_index(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("Paragraph/run index must be non-negative")
        return value

    @field_validator("font_size")
    @classmethod
    def _validate_font_size(cls, value: float | int | None) -> float | int | None:
        if value is not None and value <= 0:
            raise ValueError("Font size must be positive")
        return value

    @field_validator("font_color")
    @classmethod
    def _validate_font_color(cls, value: str | None) -> str | None:
        if value is not None:
            color = value.strip().lstrip("#").upper()
            if len(color) != 6:
                raise ValueError(
                    "Font color must be a 6-hex RGB string like 'FF0000' or '#FF0000'"
                )
            try:
                int(color, 16)
            except ValueError:
                raise ValueError(
                    "Font color must be a valid hex string like 'FF0000' or '#FF0000'"
                ) from None
            return color
        return value


class AddHyperlinkOperation(BaseSlideOperation):
    """Operation to add a hyperlink to text in a placeholder."""

    type: Literal["add_hyperlink"]
    index: int
    placeholder: Literal["title", "body", "left", "right"] = "body"
    url: str
    paragraph_index: int | None = None
    run_index: int | None = None

    @field_validator("index")
    @classmethod
    def _validate_index(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Slide index must be non-negative")
        return value

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("URL must not be empty")
        return value.strip()

    @field_validator("paragraph_index", "run_index")
    @classmethod
    def _validate_optional_index(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("Paragraph/run index must be non-negative")
        return value


class FormatTableCellOperation(BaseSlideOperation):
    """Operation to format a table cell (background color, font styling)."""

    type: Literal["format_table_cell"]
    index: int
    table_idx: int
    row: int
    column: int
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    font_size: float | int | None = None
    font_color: str | None = None
    bg_color: str | None = None

    @field_validator("index", "table_idx", "row", "column")
    @classmethod
    def _validate_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Indices must be non-negative")
        return value

    @field_validator("font_size")
    @classmethod
    def _validate_font_size(cls, value: float | int | None) -> float | int | None:
        if value is not None and value <= 0:
            raise ValueError("Font size must be positive")
        return value

    @field_validator("font_color", "bg_color")
    @classmethod
    def _validate_color(cls, value: str | None) -> str | None:
        if value is not None:
            color = value.strip().lstrip("#").upper()
            if len(color) != 6:
                raise ValueError(
                    "Color must be a 6-hex RGB string like 'FF0000' or '#FF0000'"
                )
            try:
                int(color, 16)
            except ValueError:
                raise ValueError(
                    "Color must be a valid hex string like 'FF0000' or '#FF0000'"
                ) from None
            return color
        return value


SlideEditOperation = Annotated[
    UpdateSlideTitleOperation
    | UpdateSlideSubtitleOperation
    | SetBulletsOperation
    | AppendBulletsOperation
    | ClearPlaceholderOperation
    | ReplaceTextOperation
    | AppendTableOperation
    | UpdateTableCellOperation
    | DeleteSlideOperation
    | DuplicateSlideOperation
    | SetNotesOperation
    | ApplyTextFormattingOperation
    | AddHyperlinkOperation
    | FormatTableCellOperation,
    Field(discriminator="type"),
]
