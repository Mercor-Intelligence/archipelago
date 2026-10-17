import os
from typing import Annotated

from pydantic import Field
from utils.decorators import make_async_background
from utils.models import DeleteDocumentResponse
from utils.path_utils import PathTraversalError, resolve_under_root
from utils.structured_output import structured


@make_async_background
def delete_document(
    file_path: Annotated[
        str,
        Field(
            description="Absolute path to the .docx file to delete (e.g., '/documents/old_report.docx'); operation is irreversible"
        ),
    ],
) -> str:
    """Delete a .docx document from the filesystem."""

    if not isinstance(file_path, str) or not file_path:
        return "File path is required"
    if not file_path.startswith("/"):
        return "File path must start with /"
    if not file_path.lower().endswith(".docx"):
        return "File path must end with .docx"

    try:
        target_path = resolve_under_root(file_path)
    except PathTraversalError:
        return f"Invalid path: {file_path}"

    try:
        if not os.path.exists(target_path):
            return f"File not found: {file_path}"
        if not os.path.isfile(target_path):
            return f"Not a file: {file_path}"

        os.remove(target_path)
        return structured(
            DeleteDocumentResponse(filepath=file_path, status="success"),
            f"Document {file_path} deleted successfully",
        )
    except PermissionError:
        return f"Permission denied: {file_path}"
    except Exception as exc:
        return f"Failed to delete document: {repr(exc)}"
