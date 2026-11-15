import os

from models.response import DeleteDeckResponse
from models.tool_inputs import DeleteDeckInput
from utils.decorators import make_async_background
from utils.path_utils import resolve_under_root

SLIDES_ROOT = os.getenv("APP_SLIDES_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the slides root, refusing anything that escapes it.

    Delegates to the shared resolver so containment is enforced in ONE place:
    the previous per-module copy normpath()'d after joining and never checked
    the result, so ``/../app/tools/...`` resolved outside the root. Kept as a
    module-level name so call sites (and tests) are unchanged.

    Raises PathTraversalError when the path escapes the sandbox.
    """
    return resolve_under_root(path, root=SLIDES_ROOT)


@make_async_background
def delete_deck(request: DeleteDeckInput) -> DeleteDeckResponse:
    """Delete a PowerPoint presentation file from the filesystem.

    Permanently removes a .pptx file from the specified path. This operation cannot be undone.

    Notes:
        - Permanent, cannot be undone
        - Idempotent: success even if file doesn't exist
        - Fails only if file locked, insufficient permissions, or read-only disk
    """

    def error(msg: str) -> DeleteDeckResponse:
        return DeleteDeckResponse(success=False, error=msg)

    target_path = _resolve_under_root(request.file_path)

    try:
        if os.path.exists(target_path):
            os.remove(target_path)
    except Exception as exc:
        return error(f"Failed to delete slides: {repr(exc)}")

    return DeleteDeckResponse(success=True, file_path=request.file_path)
