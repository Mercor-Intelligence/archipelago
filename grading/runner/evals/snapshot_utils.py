"""Shared snapshot utility functions for file-based verifiers."""

import zipfile

EXPORTS_BASE = "filesystem/exports"


def find_files_in_snapshot(
    snapshot_zip: zipfile.ZipFile, extension: str, base_path: str = ""
) -> list[str]:
    """Find all files with given extension in the snapshot zip.

    base_path should be the full prefix as it appears in the zip
    (e.g. '.apps_data/kicad_mcp/projects' or 'filesystem/exports').
    """
    prefix = f"{base_path}/" if base_path else ""
    return [
        name
        for name in snapshot_zip.namelist()
        if name.startswith(prefix) and name.lower().endswith(extension.lower())
    ]


def export_file_exists(snapshot_zip: zipfile.ZipFile, file_path: str) -> bool:
    """Check if an export file exists in the snapshot."""
    full_path = f"{EXPORTS_BASE}/{file_path.lstrip('/')}"
    return full_path in snapshot_zip.namelist()


def count_export_files(
    snapshot_zip: zipfile.ZipFile, directory: str, extension: str = ""
) -> int:
    """Count export files in a directory."""
    prefix = f"{EXPORTS_BASE}/{directory.strip('/')}/"
    files = [
        name
        for name in snapshot_zip.namelist()
        if name.startswith(prefix) and not name.endswith("/")
    ]
    if extension:
        files = [f for f in files if f.lower().endswith(extension.lower())]
    return len(files)
