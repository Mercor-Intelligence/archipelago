import os

from utils.decorators import make_async_background

DOCS_ROOT = os.getenv("APP_DOCS_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the docs root."""
    path = path.lstrip("/")
    full_path = os.path.join(DOCS_ROOT, path)
    return os.path.normpath(full_path)


@make_async_background
def delete_document(file_path: str) -> str:
    """Delete a .docx document from the filesystem."""

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

        os.remove(target_path)
        return f"Document {file_path} deleted successfully"
    except PermissionError:
        return f"Permission denied: {file_path}"
    except Exception as exc:
        return f"Failed to delete document: {repr(exc)}"
