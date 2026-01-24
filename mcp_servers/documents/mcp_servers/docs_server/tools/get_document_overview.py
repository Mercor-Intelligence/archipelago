import os

from docx import Document
from utils.decorators import make_async_background
from utils.models import (
    DocumentOverviewMetadata,
    GetDocumentOverviewResponse,
    HeadingStructure,
)

DOCS_ROOT = os.getenv("APP_DOCS_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the docs root."""
    path = path.lstrip("/")
    full_path = os.path.join(DOCS_ROOT, path)
    return os.path.normpath(full_path)


@make_async_background
def get_document_overview(file_path: str) -> str:
    """Get a structural overview of a .docx document with annotated headings."""

    if not isinstance(file_path, str) or not file_path:
        return "File path is required"
    if not file_path.startswith("/"):
        return "File path must start with /"
    if not file_path.lower().endswith(".docx"):
        return "File path must end with .docx"

    target_path = _resolve_under_root(file_path)

    try:
        if not os.path.exists(target_path):
            return f"File not found: {file_path}"
        if not os.path.isfile(target_path):
            return f"Not a file: {file_path}"

        doc = Document(target_path)
    except Exception as exc:
        return f"Failed to read document: {repr(exc)}"

    structure: list[HeadingStructure] = []
    heading_count = 0

    for p_idx, paragraph in enumerate(doc.paragraphs):
        annotation = f"body.p.{p_idx}"
        style = getattr(paragraph, "style", None)
        style_name = style.name if style else None
        text = paragraph.text

        is_heading = False
        heading_level = 0

        if style_name:
            style_lower = style_name.lower()
            if "heading" in style_lower:
                is_heading = True
                for i in range(1, 10):
                    if f"heading {i}" in style_lower or f"heading{i}" in style_lower:
                        heading_level = i
                        break
                if heading_level == 0:  # Generic heading without level
                    heading_level = 1
            elif "title" in style_lower:
                is_heading = True
                heading_level = 1

        if is_heading:
            heading_count += 1
            structure.append(
                HeadingStructure(
                    type="heading",
                    level=heading_level,
                    text=text,
                    annotation=annotation,
                    style=style_name,
                )
            )

    _, ext = os.path.splitext(file_path)
    extension = ext[1:].lower() if ext.startswith(".") else ext.lower()

    result = GetDocumentOverviewResponse(
        filepath=file_path,
        extension=extension,
        status="success",
        metadata=DocumentOverviewMetadata(
            heading_count=heading_count,
            section_count=len(doc.sections),
        ),
        structure=structure,
    )

    return str(result)
