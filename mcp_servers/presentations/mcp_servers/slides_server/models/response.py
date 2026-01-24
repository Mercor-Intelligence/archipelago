"""Pydantic response models for slides tools."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ============ Write Operation Responses ============


class CreateDeckResponse(BaseModel):
    """Response for create_deck operation."""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="Whether operation succeeded")
    file_name: str | None = Field(None, description="Created file name")
    file_path: str | None = Field(None, description="Full path to created file")
    error: str | None = Field(None, description="Error message if failed")


class DeleteDeckResponse(BaseModel):
    """Response for delete_deck operation."""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="Whether operation succeeded")
    file_path: str | None = Field(None, description="Path of deleted file")
    error: str | None = Field(None, description="Error message if failed")


class AddSlideResponse(BaseModel):
    """Response for add_slide operation."""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="Whether operation succeeded")
    index: int | None = Field(None, description="Index where slide was added")
    file_path: str | None = Field(None, description="Path to presentation")
    error: str | None = Field(None, description="Error message if failed")


class EditSlidesResponse(BaseModel):
    """Response for edit_slides operation."""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="Whether operation succeeded")
    file_path: str | None = Field(None, description="Path to edited presentation")
    operations_applied: int | None = Field(
        None, description="Number of operations applied"
    )
    error: str | None = Field(None, description="Error message if failed")


class AddImageResponse(BaseModel):
    """Response for add_image operation."""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="Whether operation succeeded")
    slide_index: int | None = Field(None, description="Slide where image was added")
    position: tuple[float, float] | None = Field(
        None, description="Position (x, y) in inches"
    )
    error: str | None = Field(None, description="Error message if failed")


class ModifyImageResponse(BaseModel):
    """Response for modify_image operation."""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="Whether operation succeeded")
    image_index: int | None = Field(None, description="Index of modified image")
    slide_index: int | None = Field(None, description="Slide containing the image")
    operation: str | None = Field(None, description="Operation performed")
    error: str | None = Field(None, description="Error message if failed")


class InsertChartResponse(BaseModel):
    """Response for insert_chart operation."""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="Whether operation succeeded")
    slide_index: int | None = Field(None, description="Slide where chart was inserted")
    chart_type: str | None = Field(None, description="Type of chart created")
    title: str | None = Field(None, description="Chart title")
    error: str | None = Field(None, description="Error message if failed")


class InsertTableResponse(BaseModel):
    """Response for insert_table operation."""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="Whether operation succeeded")
    slide_index: int | None = Field(None, description="Slide where table was inserted")
    rows: int | None = Field(None, description="Number of rows")
    cols: int | None = Field(None, description="Number of columns")
    error: str | None = Field(None, description="Error message if failed")


class AddShapeResponse(BaseModel):
    """Response for add_shape operation."""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="Whether operation succeeded")
    slide_index: int | None = Field(None, description="Slide where shape was added")
    shape_type: str | None = Field(None, description="Type of shape added")
    position: tuple[float, float] | None = Field(
        None, description="Position (x, y) in inches"
    )
    error: str | None = Field(None, description="Error message if failed")


# ============ Read Operation Responses ============


class ReadRangeResponse(BaseModel):
    """Response for read_slides (read_range) operation."""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="Whether operation succeeded")
    content: str | None = Field(None, description="Text content from the range")
    start: int | None = Field(None, description="Start character position")
    end: int | None = Field(None, description="End character position")
    total_length: int | None = Field(
        None, description="Total file length in characters"
    )
    error: str | None = Field(None, description="Error message if failed")


class SlideOverviewData(BaseModel):
    """Data for a single slide in deck overview."""

    model_config = ConfigDict(extra="forbid")

    slide_index: int = Field(..., description="0-based slide index")
    title: str = Field(..., description="Slide title")
    content: str = Field(..., description="Slide text content")


class ReadDeckResponse(BaseModel):
    """Response for read_completedeck operation."""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="Whether operation succeeded")
    total_slides: int | None = Field(None, description="Total number of slides")
    slides: list[SlideOverviewData] | None = Field(
        None, description="Overview of each slide"
    )
    error: str | None = Field(None, description="Error message if failed")


class ImageInfoData(BaseModel):
    """Data for an image in a slide."""

    model_config = ConfigDict(extra="forbid")

    annotation: str = Field(..., description="Cache key for retrieving image")
    slide_index: int = Field(..., description="Slide containing the image")
    image_index: int = Field(..., description="Image index on slide")
    width: float | None = Field(None, description="Width in inches")
    height: float | None = Field(None, description="Height in inches")


class ReadSlideResponse(BaseModel):
    """Response for read_individualslide operation."""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="Whether operation succeeded")
    slide_index: int | None = Field(None, description="0-based slide index")
    total_slides: int | None = Field(None, description="Total slides in presentation")
    layout: str | None = Field(None, description="Slide layout name")
    components: list[dict[str, Any]] | None = Field(
        None, description="Shapes and components on slide"
    )
    images: list[ImageInfoData] | None = Field(None, description="Images on slide")
    notes: str | None = Field(None, description="Speaker notes")
    error: str | None = Field(None, description="Error message if failed")
