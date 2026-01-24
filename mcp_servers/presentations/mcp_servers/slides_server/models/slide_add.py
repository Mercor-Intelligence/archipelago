from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AddSlideInput(BaseModel):
    """Input model for adding a slide to a presentation."""

    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(..., description="Path to the .pptx file")
    index: int = Field(..., ge=0, description="Position to insert the slide (0-based)")
    layout: Literal[
        "title",
        "title_and_content",
        "section_header",
        "two_content",
        "title_only",
        "blank",
    ] = Field(default="title_and_content", description="Slide layout type")
    title: str | None = Field(None, description="Optional title text for the slide")
    subtitle: str | None = Field(
        None,
        description="Optional subtitle text; ONLY supported on 'title' and 'section_header' layouts. Ignored for other layouts.",
    )
    bullets: list[str] | None = Field(
        None, min_length=1, description="Optional bullet points for the slide"
    )

    @field_validator("file_path")
    @classmethod
    def _validate_file_path(cls, value: str) -> str:
        if not value:
            raise ValueError("File path is required")
        if not value.startswith("/"):
            raise ValueError("File path must start with /")
        if not value.lower().endswith(".pptx"):
            raise ValueError("File path must end with .pptx")
        return value

    @field_validator("bullets")
    @classmethod
    def _validate_bullets(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(value) == 0:
            raise ValueError("Bullets must contain at least one item when provided")
        return value
