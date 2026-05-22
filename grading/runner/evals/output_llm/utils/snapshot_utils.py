"""
Utilities for working with snapshot zip files.

This module provides functions for reading files from snapshot zips
and other snapshot-related operations.
"""

import zipfile
from pathlib import Path
from typing import Any

from loguru import logger


def read_file_from_snapshot_zip(
    snapshot_zip: zipfile.ZipFile,
    file_path: str,
    base_dir: str = "filesystem",
) -> bytes | None:
    """
    Read a specific file from a snapshot zip.

    This is a centralized utility for reading files from snapshot zips,
    used across the codebase for artifacts_to_reference, snapshot diffs, etc.

    Snapshot zips typically have a base directory structure (default: "filesystem/")
    that is automatically prepended to the file path.

    Args:
        snapshot_zip: The ZipFile object to read from
        file_path: Path to the file within the zip (relative path, without base_dir)
        base_dir: Base directory in the zip (default: "filesystem")

    Returns:
        File bytes if found, None otherwise

    Example:
        ```python
        with zipfile.ZipFile(snapshot_bytes, "r") as zip_file:
            # Reads from "filesystem/documents/report.pdf"
            file_data = read_file_from_snapshot_zip(zip_file, "documents/report.pdf")
            if file_data:
                # Process file_data
        ```
    """
    # Normalize the path (remove leading slashes if present)
    normalized_path = file_path.lstrip("/")

    # Prepend base directory if provided
    if base_dir:
        full_path = f"{base_dir}/{normalized_path}"
    else:
        full_path = normalized_path

    try:
        file_bytes = snapshot_zip.read(full_path)
        logger.debug(f"Successfully read {len(file_bytes)} bytes from {full_path}")
        return file_bytes
    except KeyError:
        logger.warning(f"File {full_path} not found in snapshot zip")
        logger.debug(f"Available files (first 100): {snapshot_zip.namelist()[:100]}...")
        return None
    except Exception as e:
        logger.error(f"Failed to read {full_path} from snapshot zip: {e}")
        return None


def file_exists_in_snapshot_zip(
    snapshot_zip: zipfile.ZipFile,
    file_path: str,
    base_dir: str = "filesystem",
) -> bool:
    """
    Check if a file exists in a snapshot zip.

    Args:
        snapshot_zip: The ZipFile object to check
        file_path: Path to the file within the zip (relative path, without base_dir)
        base_dir: Base directory in the zip (default: "filesystem")

    Returns:
        True if the file exists, False otherwise
    """
    normalized_path = file_path.lstrip("/")

    if base_dir:
        full_path = f"{base_dir}/{normalized_path}"
    else:
        full_path = normalized_path

    return full_path in snapshot_zip.namelist()


def list_files_in_snapshot_zip(
    snapshot_zip: zipfile.ZipFile,
    prefix: str = "",
    extension: str | None = None,
    base_dir: str = "filesystem",
    strip_base_dir: bool = True,
) -> list[str]:
    """
    List files in a snapshot zip, optionally filtered by prefix and extension.

    Args:
        snapshot_zip: The ZipFile object to list files from
        prefix: Optional prefix to filter by (e.g., "documents/"), relative to base_dir
        extension: Optional extension to filter by (e.g., ".pdf")
        base_dir: Base directory in the zip (default: "filesystem")
        strip_base_dir: If True, removes base_dir from returned paths (default: True)

    Returns:
        List of file paths matching the filters (with base_dir stripped if strip_base_dir=True)
    """
    all_files = snapshot_zip.namelist()

    # Filter by base directory
    if base_dir:
        base_prefix = f"{base_dir}/"
        all_files = [f for f in all_files if f.startswith(base_prefix)]

    # Filter by prefix (within base_dir)
    if prefix:
        full_prefix = f"{base_dir}/{prefix}" if base_dir else prefix
        all_files = [f for f in all_files if f.startswith(full_prefix)]

    # Filter by extension
    if extension:
        if not extension.startswith("."):
            extension = f".{extension}"
        all_files = [
            f for f in all_files if Path(f).suffix.lower() == extension.lower()
        ]

    # Filter out directories (entries ending with /)
    all_files = [f for f in all_files if not f.endswith("/")]

    # Strip base_dir if requested
    if strip_base_dir and base_dir:
        base_prefix = f"{base_dir}/"
        all_files = [
            f[len(base_prefix) :] if f.startswith(base_prefix) else f for f in all_files
        ]

    return all_files


def get_snapshot_zip_info(
    snapshot_zip: zipfile.ZipFile,
    base_dir: str = "filesystem",
) -> dict[str, Any]:
    """
    Get summary information about a snapshot zip.

    Args:
        snapshot_zip: The ZipFile object to analyze
        base_dir: Base directory in the zip to analyze (default: "filesystem")

    Returns:
        Dictionary with summary information:
        - total_files: Number of files in the zip
        - total_size: Total uncompressed size in bytes
        - file_types: Dictionary mapping extensions to counts
        - base_dir: The base directory that was analyzed
    """
    all_files = snapshot_zip.namelist()

    # Filter by base directory if specified
    if base_dir:
        base_prefix = f"{base_dir}/"
        all_files = [f for f in all_files if f.startswith(base_prefix)]

    # Filter out directories
    all_files = [f for f in all_files if not f.endswith("/")]

    total_size = sum(snapshot_zip.getinfo(f).file_size for f in all_files)

    # Count file types
    file_types: dict[str, int] = {}
    for file_path in all_files:
        ext = Path(file_path).suffix.lower() or "no_extension"
        file_types[ext] = file_types.get(ext, 0) + 1

    return {
        "total_files": len(all_files),
        "total_size": total_size,
        "file_types": file_types,
        "base_dir": base_dir,
    }
