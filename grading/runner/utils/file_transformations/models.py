from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field

from ..file_extraction.types import ImageMetadata


class TransformationOutput(BaseModel):
    text: str | None = None
    images: list[ImageMetadata] = Field(default_factory=list)


TransformationImpl = Callable[[bytes, str], Awaitable[TransformationOutput]]


# @apg_transformation_shared:start
class OutputRepresentation(StrEnum):
    TO_TEXT = "to_text"
    TO_IMAGES = "to_images"
    NATIVE = "native"


class InputFileFamily(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    SPREADSHEET = "spreadsheet"


class OutputRepresentationDefn(BaseModel):
    representation_id: OutputRepresentation
    label: str
    description: str


OUTPUT_REPRESENTATION_DEFNS: dict[OutputRepresentation, OutputRepresentationDefn] = {
    OutputRepresentation.TO_TEXT: OutputRepresentationDefn(
        representation_id=OutputRepresentation.TO_TEXT,
        label="Extracted Text",
        description="Extracts raw text content using document parsing. Best for criteria evaluating textual content, data values, or written information.",
    ),
    OutputRepresentation.TO_IMAGES: OutputRepresentationDefn(
        representation_id=OutputRepresentation.TO_IMAGES,
        label="Rendered Images",
        description="Renders each page, slide, or sheet as a visual image. Best for criteria evaluating formatting, layout, charts, or visual appearance.",
    ),
    OutputRepresentation.NATIVE: OutputRepresentationDefn(
        representation_id=OutputRepresentation.NATIVE,
        label="Native",
        description="Uses the file's built-in extraction service, combining both extracted text and embedded images into a single representation.",
    ),
}


class InputFileFamilyDefn(BaseModel):
    family_id: InputFileFamily
    extensions: frozenset[str]


INPUT_FILE_FAMILY_DEFNS: dict[InputFileFamily, InputFileFamilyDefn] = {
    InputFileFamily.PDF: InputFileFamilyDefn(
        family_id=InputFileFamily.PDF,
        extensions=frozenset({".pdf"}),
    ),
    InputFileFamily.DOCX: InputFileFamilyDefn(
        family_id=InputFileFamily.DOCX,
        extensions=frozenset({".docx", ".doc"}),
    ),
    InputFileFamily.PPTX: InputFileFamilyDefn(
        family_id=InputFileFamily.PPTX,
        extensions=frozenset({".pptx", ".ppt"}),
    ),
    InputFileFamily.SPREADSHEET: InputFileFamilyDefn(
        family_id=InputFileFamily.SPREADSHEET,
        extensions=frozenset({".xls", ".xlsx", ".xlsm"}),
    ),
}


class ArtifactTransformationId(StrEnum):
    PDF_TO_TEXT = "pdf_to_text"
    PDF_TO_IMAGES = "pdf_to_images"
    PDF_NATIVE = "pdf_native"
    DOCX_TO_TEXT = "docx_to_text"
    DOCX_TO_IMAGES = "docx_to_images"
    DOCX_NATIVE = "docx_native"
    PPTX_TO_TEXT = "pptx_to_text"
    PPTX_TO_IMAGES = "pptx_to_images"
    PPTX_NATIVE = "pptx_native"
    SPREADSHEET_TO_TEXT = "spreadsheet_to_text"
    SPREADSHEET_TO_IMAGES = "spreadsheet_to_images"
    SPREADSHEET_NATIVE = "spreadsheet_native"


class ArtifactTransformationDefn(BaseModel):
    transformation_id: ArtifactTransformationId
    input_family: InputFileFamily
    output_representation: OutputRepresentation
    transformation_impl: TransformationImpl | None = None

    model_config = {"arbitrary_types_allowed": True}

    @computed_field
    @property
    def label(self) -> str:
        return OUTPUT_REPRESENTATION_DEFNS[self.output_representation].label

    @computed_field
    @property
    def description(self) -> str:
        return OUTPUT_REPRESENTATION_DEFNS[self.output_representation].description

    @computed_field
    @property
    def source_extensions(self) -> frozenset[str]:
        return INPUT_FILE_FAMILY_DEFNS[self.input_family].extensions


# @apg_transformation_shared:end
