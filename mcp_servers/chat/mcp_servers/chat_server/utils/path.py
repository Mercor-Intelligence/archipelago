import os

from utils.config import CHAT_DATA_ROOT


def resolve_chat_path(path: str) -> str:
    """Map path to the chat data root.

    Args:
        path: The relative path to resolve under the chat data root.

    Returns:
        The normalized absolute path under CHAT_DATA_ROOT.
    """
    path = path.lstrip("/")
    full_path = os.path.join(CHAT_DATA_ROOT, path)
    return os.path.normpath(full_path)
