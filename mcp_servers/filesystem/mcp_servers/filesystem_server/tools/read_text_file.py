import os
from typing import Annotated

from loguru import logger
from pydantic import Field
from tools.eml_reader import EML_MAX_BYTES, oversize_message, render_eml
from utils.decorators import make_async_background
from utils.path_utils import (
    PathTraversalError,
)
from utils.path_utils import (
    resolve_under_root as _resolve_under_root,
)
from utils.path_utils import (
    validate_real_path as _validate_real_path,
)

# File size threshold for warning (3GB) - files above this will log a warning but still be processed
LARGE_FILE_WARNING_BYTES = 3 * 1024 * 1024 * 1024

# Sentinel value for max_size meaning "read the file whatever its size"
NO_SIZE_LIMIT = 0

# Allowed text file extensions
TEXT_EXTENSIONS = frozenset(
    {
        "txt",
        "json",
        "csv",
        "py",
        "md",
        "eml",
        "xml",
        "yaml",
        "yml",
        "js",
        "ts",
        "jsx",
        "tsx",
        "htm",
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
            description="Absolute path to the text file within the sandbox filesystem. REQUIRED. Must start with '/'. Supported extensions: txt, json, csv, py, md, eml, xml, yaml, yml, js, ts, jsx, tsx, htm, html, css, scss, less, java, c, cpp, h, hpp, rs, go, rb, php, sh, bash, zsh, fish, ps1, bat, cmd, sql, graphql, gql, toml, ini, cfg, conf, env, properties, log, gitignore, dockerignore, editorconfig, rst, tex, bib. Also supports extensionless files: Makefile, Dockerfile, Vagrantfile. Example: '/config/settings.json' or '/src/main.py'. Returns the complete text content of the file as a string. Raises FileNotFoundError if file doesn't exist, ValueError for unsupported extensions, encoding errors, or a file larger than max_size, RuntimeError for other read failures. Note: the server applies no general size cap of its own, so very large files (>3GB) will succeed but may be slow and memory-intensive; pass max_size to cap the read by byte count. Note: '.eml' (MIME email) files are decoded rather than returned verbatim - you get a '## Headers' section, one '## Body' section per plain-text or HTML body part (HTML as raw source, never rendered), and a '## Attachments' manifest listing each attachment's name, type, and size; attachment payloads are not returned. Emails over 25 MB are refused."
        ),
    ],
    encoding: Annotated[
        str,
        Field(
            description="Character encoding for reading the file. Default: 'utf-8'. Common values: 'utf-8', 'latin-1', 'ascii', 'utf-16', 'cp1252'. Raises ValueError if the file cannot be decoded with the specified encoding. For '.eml' files each body part is decoded with the charset it declares and this value is only the fallback for parts that declare none."
        ),
    ] = "utf-8",
    max_size: Annotated[
        int,
        Field(
            description="Maximum size in bytes the file is allowed to have, checked against the file on disk before any content is read. Default: 0, meaning no limit — the server imposes no size cap of its own, so a read fails on size only when you pass a positive max_size. Pass a positive byte count to cap the read: a file larger than max_size raises ValueError('File too large: <actual> bytes exceeds max_size of <max_size> bytes') and returns no content, never a truncated read. A file exactly max_size bytes is read. Negative values raise ValueError. Files above 3GB are still read when max_size allows it, but log a slow / high-memory warning."
        ),
    ] = NO_SIZE_LIMIT,
) -> str:
    """Read the contents of a text file. Only files with supported extensions (e.g. .txt, .json, .csv, .py, .md, .xml, .yaml, .htm, .html, .sh) are readable. Use to read configs, logs, or source. Reads the whole file by default; pass max_size to cap the read at a byte count and fail on oversized files instead of loading them. MIME email (.eml) files are supported and come back decoded: headers, plain-text and HTML body parts, and an attachment manifest."""
    if not isinstance(file_path, str) or not file_path:
        raise ValueError("File path is required and must be a string")

    if not file_path.startswith("/"):
        raise ValueError("File path must start with /")

    if isinstance(max_size, bool) or not isinstance(max_size, int):
        raise ValueError("max_size must be an integer number of bytes")

    if max_size < NO_SIZE_LIMIT:
        raise ValueError(f"max_size must be >= 0 (0 means no limit), got {max_size}")

    # Validate file extension
    file_ext = _get_extension(file_path)
    if file_ext not in TEXT_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type: '{file_ext}'. "
            f"Supported extensions: {', '.join(sorted(TEXT_EXTENSIONS))}"
        )

    try:
        target_path = _resolve_under_root(file_path)
    except PathTraversalError as exc:
        raise ValueError(f"Access denied: {file_path}") from exc

    # SECURITY: Use lstat to check existence without following symlinks
    if not os.path.lexists(target_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    # SECURITY: Validate real path is within sandbox before any file operations
    real_path = _validate_real_path(target_path)

    if not os.path.isfile(real_path):
        raise ValueError(f"Not a file: {file_path}")

    file_size = os.path.getsize(real_path)

    # Enforce the caller's cap before reading, so an oversized file costs no
    # memory and the caller never receives a silently truncated result.
    if max_size > NO_SIZE_LIMIT and file_size > max_size:
        raise ValueError(
            f"File too large: {file_size} bytes exceeds max_size of {max_size} bytes"
        )

    # Log warning for very large files but still process them
    if file_size > LARGE_FILE_WARNING_BYTES:
        size_gb = file_size / (1024 * 1024 * 1024)
        logger.warning(
            f"Processing large file: {file_path} ({size_gb:.2f}GB). "
            "This may take longer and use significant memory."
        )

    # MIME email is text, but its content is encoded: headers are RFC 2047,
    # bodies are quoted-printable or base64, and attachments are base64 blobs.
    # Hand it to the email parser instead of returning the raw source.
    if file_ext == "eml":
        if file_size > EML_MAX_BYTES:
            logger.warning(f"Refusing oversized email: {file_path} ({file_size} bytes)")
            raise ValueError(oversize_message(file_path, file_size))
        try:
            with open(real_path, "rb") as f:
                raw_email = f.read()
        except Exception as exc:
            raise RuntimeError(f"Failed to read text file: {repr(exc)}") from exc
        return render_eml(raw_email, fallback_encoding=encoding)

    try:
        with open(real_path, encoding=encoding) as f:
            return f.read()
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Failed to decode file with encoding '{encoding}': {exc}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to read text file: {repr(exc)}") from exc
