import os

from models.response import DeleteDeckResponse
from utils.decorators import make_async_background

SLIDES_ROOT = os.getenv("APP_SLIDES_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the slides root."""
    path = path.lstrip("/")
    full_path = os.path.join(SLIDES_ROOT, path)
    return os.path.normpath(full_path)


@make_async_background
def delete_deck(file_path: str) -> DeleteDeckResponse:
    """Delete a Presentations presentation."""

    def error(msg: str) -> DeleteDeckResponse:
        return DeleteDeckResponse(success=False, error=msg)

    if not file_path or not isinstance(file_path, str):
        return error("File path is required")
    if not file_path.startswith("/"):
        return error("File path must start with /")
    if not file_path.lower().endswith(".pptx"):
        return error("File path must end with .pptx")

    target_path = _resolve_under_root(file_path)

    try:
        if os.path.exists(target_path):
            os.remove(target_path)
    except Exception as exc:
        return error(f"Failed to delete slides: {repr(exc)}")

    return DeleteDeckResponse(success=True, file_path=file_path)
