from .docx_to_images.main import docx_to_images
from .docx_to_text.main import docx_to_text
from .models import (
    ArtifactTransformationDefn,
    ArtifactTransformationId,
    InputFileFamily,
    OutputRepresentation,
)
from .native.main import native_extraction
from .pdf_to_images.main import pdf_to_images
from .pdf_to_text.main import pdf_to_text
from .pptx_to_images.main import pptx_to_images
from .pptx_to_text.main import pptx_to_text
from .spreadsheet_to_images.main import spreadsheet_to_images
from .spreadsheet_to_text.main import spreadsheet_to_text

# @apg_transformation_registry:start
TRANSFORMATION_REGISTRY: dict[ArtifactTransformationId, ArtifactTransformationDefn] = {
    ArtifactTransformationId.PDF_TO_TEXT: ArtifactTransformationDefn(
        transformation_id=ArtifactTransformationId.PDF_TO_TEXT,
        input_family=InputFileFamily.PDF,
        output_representation=OutputRepresentation.TO_TEXT,
        transformation_impl=pdf_to_text,
    ),
    ArtifactTransformationId.PDF_TO_IMAGES: ArtifactTransformationDefn(
        transformation_id=ArtifactTransformationId.PDF_TO_IMAGES,
        input_family=InputFileFamily.PDF,
        output_representation=OutputRepresentation.TO_IMAGES,
        transformation_impl=pdf_to_images,
    ),
    ArtifactTransformationId.PDF_NATIVE: ArtifactTransformationDefn(
        transformation_id=ArtifactTransformationId.PDF_NATIVE,
        input_family=InputFileFamily.PDF,
        output_representation=OutputRepresentation.NATIVE,
        transformation_impl=native_extraction,
    ),
    ArtifactTransformationId.DOCX_TO_TEXT: ArtifactTransformationDefn(
        transformation_id=ArtifactTransformationId.DOCX_TO_TEXT,
        input_family=InputFileFamily.DOCX,
        output_representation=OutputRepresentation.TO_TEXT,
        transformation_impl=docx_to_text,
    ),
    ArtifactTransformationId.DOCX_TO_IMAGES: ArtifactTransformationDefn(
        transformation_id=ArtifactTransformationId.DOCX_TO_IMAGES,
        input_family=InputFileFamily.DOCX,
        output_representation=OutputRepresentation.TO_IMAGES,
        transformation_impl=docx_to_images,
    ),
    ArtifactTransformationId.DOCX_NATIVE: ArtifactTransformationDefn(
        transformation_id=ArtifactTransformationId.DOCX_NATIVE,
        input_family=InputFileFamily.DOCX,
        output_representation=OutputRepresentation.NATIVE,
        transformation_impl=native_extraction,
    ),
    ArtifactTransformationId.PPTX_TO_TEXT: ArtifactTransformationDefn(
        transformation_id=ArtifactTransformationId.PPTX_TO_TEXT,
        input_family=InputFileFamily.PPTX,
        output_representation=OutputRepresentation.TO_TEXT,
        transformation_impl=pptx_to_text,
    ),
    ArtifactTransformationId.PPTX_TO_IMAGES: ArtifactTransformationDefn(
        transformation_id=ArtifactTransformationId.PPTX_TO_IMAGES,
        input_family=InputFileFamily.PPTX,
        output_representation=OutputRepresentation.TO_IMAGES,
        transformation_impl=pptx_to_images,
    ),
    ArtifactTransformationId.PPTX_NATIVE: ArtifactTransformationDefn(
        transformation_id=ArtifactTransformationId.PPTX_NATIVE,
        input_family=InputFileFamily.PPTX,
        output_representation=OutputRepresentation.NATIVE,
        transformation_impl=native_extraction,
    ),
    ArtifactTransformationId.SPREADSHEET_TO_TEXT: ArtifactTransformationDefn(
        transformation_id=ArtifactTransformationId.SPREADSHEET_TO_TEXT,
        input_family=InputFileFamily.SPREADSHEET,
        output_representation=OutputRepresentation.TO_TEXT,
        transformation_impl=spreadsheet_to_text,
    ),
    ArtifactTransformationId.SPREADSHEET_TO_IMAGES: ArtifactTransformationDefn(
        transformation_id=ArtifactTransformationId.SPREADSHEET_TO_IMAGES,
        input_family=InputFileFamily.SPREADSHEET,
        output_representation=OutputRepresentation.TO_IMAGES,
        transformation_impl=spreadsheet_to_images,
    ),
    ArtifactTransformationId.SPREADSHEET_NATIVE: ArtifactTransformationDefn(
        transformation_id=ArtifactTransformationId.SPREADSHEET_NATIVE,
        input_family=InputFileFamily.SPREADSHEET,
        output_representation=OutputRepresentation.NATIVE,
        transformation_impl=native_extraction,
    ),
}
# @apg_transformation_registry:end


def get_transformation(
    transformation_id: ArtifactTransformationId,
) -> ArtifactTransformationDefn | None:
    return TRANSFORMATION_REGISTRY.get(transformation_id)


def get_available_transformations(
    file_extension: str,
) -> list[ArtifactTransformationDefn]:
    ext = file_extension.lower()
    return [
        defn
        for defn in TRANSFORMATION_REGISTRY.values()
        if ext in defn.source_extensions
    ]
