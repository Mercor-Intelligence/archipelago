import os

from docx import Document
from helpers.identifier import parse_identifier
from helpers.mutate import resolve_target, set_text
from utils.decorators import make_async_background
from utils.models import EditContentTextResponse, EditTargetInfo

DOCS_ROOT = os.getenv("APP_DOCS_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the docs root."""
    path = path.lstrip("/")
    full_path = os.path.join(DOCS_ROOT, path)
    return os.path.normpath(full_path)


@make_async_background
def edit_content_text(file_path: str, identifier: str, new_text: str) -> str:
    """Replace text content at a specific identifier in a .docx document."""

    if not isinstance(file_path, str) or not file_path:
        return "File path is required"
    if not file_path.startswith("/"):
        return "File path must start with /"
    if not file_path.lower().endswith(".docx"):
        return "File path must end with .docx"
    if not identifier or not identifier.strip():
        return "Identifier is required"

    target_path = _resolve_under_root(file_path)

    try:
        if not os.path.exists(target_path):
            return f"File not found: {file_path}"
        if not os.path.isfile(target_path):
            return f"Not a file: {file_path}"

        doc = Document(target_path)
    except Exception as exc:
        return f"Failed to read document: {repr(exc)}"

    # Resolve identifier and target object
    try:
        parsed = parse_identifier(identifier)
        target_kind, target_obj, target_type = resolve_target(doc, parsed)
    except Exception as exc:
        return f"Failed to parse identifier '{identifier}'. Please ensure the identifier is valid from read_document_content tool. Error: {repr(exc)}"

    # Apply text change
    try:
        old_text, _ = set_text(target_obj, target_type, new_text)
    except Exception as exc:
        return f"Failed to set text: {repr(exc)}"

    # Save document
    try:
        doc.save(target_path)
    except Exception as exc:
        return f"Failed to save document: {repr(exc)}"

    result = EditContentTextResponse(
        filepath=file_path,
        status="success",
        target=EditTargetInfo(
            kind=target_kind,
            identifier=identifier,
            old_text=old_text,
            new_text=new_text,
        ),
    )

    return str(result)
