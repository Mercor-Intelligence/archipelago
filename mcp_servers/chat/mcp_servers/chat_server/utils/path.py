import os

from utils.config import CHAT_DATA_ROOT

# =============================================================================
# NOTE: TEMPORARY MEASURE - Google Chat Subdirectory Fallback
# =============================================================================
# Some Google Takeout exports incorrectly include a "Google Chat" subdirectory
# in the data structure. This is NOT the expected format - data should be
# placed directly in CHAT_DATA_ROOT (e.g., CHAT_DATA_ROOT/Groups/).
#
# This fallback is a TEMPORARY workaround to handle malformed data uploads.
# Once the upstream data ingestion is fixed to strip the "Google Chat"
# subdirectory, this fallback should be REMOVED.
#
# To remove: Set _ENABLE_GOOGLE_CHAT_SUBDIR_FALLBACK to False or delete
# the _try_google_chat_subdir_fallback function and its usage.
# =============================================================================
_ENABLE_GOOGLE_CHAT_SUBDIR_FALLBACK = True
_GOOGLE_CHAT_SUBDIR = "Google Chat"


def _real(path: str) -> str:
    """Resolve *path* to an absolute path with every symlink resolved."""
    return os.path.normpath(os.path.realpath(path))


def _check_containment(resolved: str) -> None:
    """Raise ValueError if *resolved* falls outside the chat data root.

    Both sides are fully resolved, so a data root that is itself a symlink
    still works while a `..` segment or a symlink pointing out is rejected.
    """
    root = _real(CHAT_DATA_ROOT)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ValueError(
            "Path traversal detected: resolved path escapes the chat data root"
        )


def _try_google_chat_subdir_fallback(path: str) -> str | None:
    """Try to resolve path with 'Google Chat' subdirectory fallback.

    NOTE: This is a TEMPORARY measure. The "Google Chat" subdirectory is
    normally not supposed to be there - data should be directly in CHAT_DATA_ROOT.

    Args:
        path: The relative path (already stripped of leading slash).

    Returns:
        The resolved path if it exists under Google Chat subdirectory, else None.

    Raises:
        ValueError: If the resolved path escapes the chat data root.
    """
    if not _ENABLE_GOOGLE_CHAT_SUBDIR_FALLBACK:
        return None

    google_chat_path = _real(os.path.join(CHAT_DATA_ROOT, _GOOGLE_CHAT_SUBDIR, path))
    _check_containment(google_chat_path)

    if os.path.exists(google_chat_path):
        return google_chat_path

    return None


def resolve_chat_path(path: str) -> str:
    """Map path to the chat data root.

    Args:
        path: The relative path to resolve under the chat data root.

    Returns:
        The resolved absolute path under CHAT_DATA_ROOT.

    Raises:
        ValueError: If the resolved path escapes the chat data root.
    """
    path = path.lstrip("/")

    # Try standard path first (this is the expected/correct structure)
    standard_path = _real(os.path.join(CHAT_DATA_ROOT, path))
    _check_containment(standard_path)

    if os.path.exists(standard_path):
        return standard_path

    # NOTE: TEMPORARY fallback for malformed data with "Google Chat" subdirectory
    fallback_path = _try_google_chat_subdir_fallback(path)
    if fallback_path:
        return fallback_path

    # Neither exists - return standard path (caller will handle missing path)
    return standard_path


def resolve_chat_file(dir_path: str, filename: str) -> str:
    """Join *filename* onto an already-resolved *dir_path* and re-check containment.

    Args:
        dir_path: A directory already resolved by resolve_chat_path.
        filename: The file name to read or write inside that directory.

    Returns:
        The resolved absolute path of the file under CHAT_DATA_ROOT.

    Raises:
        ValueError: If the resolved file escapes the chat data root. A contained
            directory can still hold an entry under this name that is a symlink
            pointing out, so the joined path is checked on its own.
    """
    file_path = _real(os.path.join(dir_path, filename))
    _check_containment(file_path)
    return file_path
