import os
from typing import Annotated

from pydantic import Field
from utils.decorators import make_async_background

FS_ROOT = os.getenv("APP_FS_ROOT", "/filesystem")

# Allowed text file extensions
TEXT_EXTENSIONS = frozenset(
    {
        "txt",
        "json",
        "csv",
        "py",
        "md",
        "xml",
        "yaml",
        "yml",
        "js",
        "ts",
        "jsx",
        "tsx",
        "html",
        "css",
        "scss",
        "less",
        "java",
        "c",
        "cpp",
        "h",
        "hpp",
        "rs",
        "go",
        "rb",
        "php",
        "sh",
        "bash",
        "zsh",
        "fish",
        "ps1",
        "bat",
        "cmd",
        "sql",
        "graphql",
        "gql",
        "toml",
        "ini",
        "cfg",
        "conf",
        "env",
        "properties",
        "log",
        "gitignore",
        "dockerignore",
        "editorconfig",
        "makefile",
        "dockerfile",
        "vagrantfile",
        "rst",
        "tex",
        "bib",
    }
)


def _resolve_under_root(path: str) -> str:
    """Map any incoming path to the sandbox root."""
    if not path or path == "/":
        return FS_ROOT
    rel = os.path.normpath(path).lstrip(os.sep)
    return os.path.join(FS_ROOT, rel)


def _validate_real_path(target_path: str) -> str:
    """Resolve symlinks and validate the real path is within the sandbox.

    Returns the resolved real path if valid, raises ValueError if path escapes sandbox.
    """
    # Resolve any symlinks to get the real path
    real_path = os.path.realpath(target_path)
    # Also resolve FS_ROOT in case it's a symlink or relative path
    real_fs_root = os.path.realpath(FS_ROOT)
    # Ensure the real path is within the sandbox
    if not real_path.startswith(real_fs_root + os.sep) and real_path != real_fs_root:
        raise ValueError("Access denied: path resolves outside sandbox")
    return real_path


def _get_extension(file_path: str) -> str:
    """Extract file extension in lowercase, handling edge cases."""
    basename = os.path.basename(file_path)
    # Handle files like "Makefile", "Dockerfile" without extensions
    if basename.lower() in ("makefile", "dockerfile", "vagrantfile"):
        return basename.lower()
    # Handle hidden files like ".gitignore"
    if basename.startswith(".") and "." not in basename[1:]:
        return basename[1:].lower()
    # Normal extension extraction
    if "." in basename:
        return basename.rsplit(".", 1)[-1].lower()
    return ""


@make_async_background
def read_text_file(
    file_path: Annotated[
        str,
        Field(
            description="Path to the text file. REQUIRED. Example: /documents/readme.md"
        ),
    ],
    encoding: Annotated[
        str,
        Field(
            description="Text encoding to use. Default: utf-8. Examples: utf-8, latin-1, ascii"
        ),
    ] = "utf-8",
    max_size: Annotated[
        int,
        Field(
            description="Maximum file size in bytes to read. Default: 1048576 (1MB). Set to 0 for no limit."
        ),
    ] = 1048576,
) -> str:
    """Read a text file from the filesystem and return its contents."""
    if not isinstance(file_path, str) or not file_path:
        raise ValueError("File path is required and must be a string")

    if not file_path.startswith("/"):
        raise ValueError("File path must start with /")

    # Validate file extension
    file_ext = _get_extension(file_path)
    if file_ext not in TEXT_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type: '{file_ext}'. "
            f"Supported extensions: {', '.join(sorted(TEXT_EXTENSIONS))}"
        )

    target_path = _resolve_under_root(file_path)

    # SECURITY: Use lstat to check existence without following symlinks
    if not os.path.lexists(target_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    # SECURITY: Validate real path is within sandbox before any file operations
    real_path = _validate_real_path(target_path)

    if not os.path.isfile(real_path):
        raise ValueError(f"Not a file: {file_path}")

    # Check file size before reading (use real_path to get accurate size)
    file_size = os.path.getsize(real_path)
    if max_size > 0 and file_size > max_size:
        raise ValueError(
            f"File too large: {file_size} bytes exceeds limit of {max_size} bytes"
        )

    try:
        with open(real_path, encoding=encoding) as f:
            return f.read()
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Failed to decode file with encoding '{encoding}': {exc}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to read text file: {repr(exc)}") from exc
