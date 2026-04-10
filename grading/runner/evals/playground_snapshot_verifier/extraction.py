"""File extraction and normalization for playground snapshots.

Handles extracting files from ZIP archives and normalizing content
for comparison (stripping volatile fields like timestamps, IDs).
"""

import io
import zipfile

from loguru import logger

from runner.evals.utils.normalizer import BINARY_EXTENSIONS, normalize_content

from .models import ExtractionResult


def _get_file_extension(filename: str) -> str:
    """Get lowercase file extension including the dot.

    Args:
        filename: File name or path

    Returns:
        Extension with dot (e.g., ".json") or empty string if no extension
    """
    if "." in filename:
        return filename[filename.rfind(".") :].lower()
    return ""


def extract_and_normalize_files(snapshot_bytes: io.BytesIO) -> ExtractionResult:
    """Extract all files from snapshot and normalize them.

    Handles:
    - SQLite databases (.db, .sqlite, .sqlite3) → SQL dump normalization
    - JSON files → Structured field normalization
    - Text files → Regex pattern normalization
    - Binary files → Skipped (logged)

    Args:
        snapshot_bytes: ZIP archive containing snapshot files

    Returns:
        ExtractionResult with normalized files and any errors encountered
    """
    snapshot_bytes.seek(0)
    result = ExtractionResult()

    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            for file_path in zf.namelist():
                # Skip directories
                if file_path.endswith("/"):
                    continue

                ext = _get_file_extension(file_path)

                # Skip binary files
                if ext in BINARY_EXTENSIONS:
                    result.skipped_binary.append(file_path)
                    continue

                try:
                    file_bytes = zf.read(file_path)

                    # normalize_content handles all file types:
                    # - SQLite → SQL dump → column normalization
                    # - JSON → field normalization
                    # - Text → regex pattern normalization
                    normalized = normalize_content(file_bytes, file_path)
                    result.normalized_files[file_path] = normalized.decode(
                        "utf-8", errors="replace"
                    )
                except Exception as e:
                    error_msg = f"Failed to normalize {file_path}: {e}"
                    logger.warning(error_msg)
                    result.errors.append(error_msg)

    except zipfile.BadZipFile as e:
        error_msg = f"Invalid ZIP file: {e}"
        logger.warning(error_msg)
        result.errors.append(error_msg)
        result.had_fatal_error = True

    return result
