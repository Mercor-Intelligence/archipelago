"""Shared path utilities for secure file path resolution.

This module provides utilities for safely resolving file paths within the
sandboxed root directory, with protection against path traversal attacks.
"""

import os


def get_slides_root() -> str:
    """Return the slides root directory from APP_SLIDES_ROOT or APP_FS_ROOT env vars."""
    return os.environ.get("APP_SLIDES_ROOT") or os.environ.get(
        "APP_FS_ROOT", "/filesystem"
    )


# Legacy constant for backward compatibility - reads at import time
SLIDES_ROOT = get_slides_root()


class PathTraversalError(ValueError):
    """Raised when a path traversal attack is detected."""

    pass


def _dealias_root_prefix(path: str, root: str) -> str:
    """Rewrite a host-absolute path that ALIASES the root into its rooted form.

    Delivered worlds expose the slides root under a second name: ``/app/files``
    is a symlink to ``/filesystem`` (the ``APP_FS_ROOT``), and the task prompt
    names that alias — so an agent following the prompt sends
    ``/app/files/deck.pptx``. Re-rooting that verbatim yields
    ``<root>/app/files/deck.pptx``, which does not exist, so the deck the agent
    was told to open is reported missing (and a SAVE to that path silently
    lands in a directory grading never reads).

    Matching on ``realpath`` rather than on the string catches every alias
    (symlink, ``..`` chain, trailing slash) without enumerating them.

    Containment is unaffected: this only chooses which candidate string is
    handed to the caller, and that candidate is still joined under ``root`` and
    still realpath-verified below. A path that resolves anywhere other than
    inside the root is returned untouched and therefore still rejected —
    notably ``/app/tools/...``, which holds the seeded apps' admin credentials.
    This deliberately does NOT fall back to "use the caller's path as-is when
    the rooted one is missing"; that would make the sandbox a passthrough.
    """
    if not path or not os.path.isabs(path):
        return path

    # Today's interpretation wins whenever it resolves to something real, so a
    # deck genuinely stored under an ``app/files/`` subdirectory keeps working
    # and the common case costs one stat and no realpath.
    naive = os.path.normpath(os.path.join(root, path.lstrip("/")))
    if os.path.exists(naive):
        return path

    # realpath() resolves the existing prefix of a not-yet-created path, so a
    # save target under an aliased directory de-aliases too.
    real = os.path.realpath(path)
    if real == root:
        return "/"
    if real.startswith(root + os.sep):
        return real[len(root) :]
    return path


def resolve_under_root(
    path: str,
    *,
    root: str | None = None,
    check_exists: bool = False,
    must_be_file: bool = False,
    must_be_dir: bool = False,
) -> str:
    """Safely resolve a path under the sandbox root directory.

    This function protects against path traversal attacks by:
    1. Normalizing the path to remove . and .. components
    2. Using realpath to resolve any symlinks
    3. Verifying the final path is still under the root directory

    Args:
        path: The user-provided path (may be relative or absolute within sandbox)
        root: Override the root directory (defaults to SLIDES_ROOT)
        check_exists: If True, verify the path exists
        must_be_file: If True, verify the path is a regular file
        must_be_dir: If True, verify the path is a directory

    Returns:
        The fully resolved absolute path within the sandbox

    Raises:
        PathTraversalError: If the resolved path escapes the sandbox
        FileNotFoundError: If check_exists=True and path doesn't exist
        ValueError: If path doesn't meet must_be_file or must_be_dir constraints
    """
    if root is None:
        # Read from environment at call time to support testing
        root = get_slides_root()

    # Normalize the root path
    root = os.path.realpath(root)

    path = _dealias_root_prefix(path, root)

    # Strip leading slashes to make path relative
    path = path.lstrip("/")

    # Combine and normalize
    full_path = os.path.normpath(os.path.join(root, path))

    # Always resolve symlinks to prevent path traversal via symlinks
    # os.path.realpath correctly handles intermediate symlinks even for non-existent final paths
    resolved_path = os.path.realpath(full_path)

    # Verify the resolved path is still under root
    if not resolved_path.startswith(root + os.sep) and resolved_path != root:
        raise PathTraversalError(
            f"Path '{path}' resolves outside the sandbox directory"
        )

    # Optional existence checks
    if check_exists and not os.path.exists(resolved_path):
        raise FileNotFoundError(f"Path does not exist: {path}")

    if must_be_file and not os.path.isfile(resolved_path):
        raise ValueError(f"Path is not a file: {path}")

    if must_be_dir and not os.path.isdir(resolved_path):
        raise ValueError(f"Path is not a directory: {path}")

    return resolved_path


def is_path_within_sandbox(path: str, root: str | None = None) -> bool:
    """Check if a path is within the sandbox without raising exceptions."""
    try:
        resolve_under_root(path, root=root)
        return True
    except (PathTraversalError, ValueError):
        return False
