"""Shared constants and utilities for KiCad verifiers."""

import json
import re
import zipfile
from typing import Any

from loguru import logger

from runner.evals.snapshot_utils import find_files_in_snapshot

KICAD_PROJECTS_BASES = [
    ".apps_data/pcb_design/projects",
    ".apps_data/kicad_mcp/projects",
    ".apps_data/kicad",
]


def find_project_files(
    snapshot_zip: zipfile.ZipFile,
    extension: str,
    project_name: str | None = None,
    projects_base_path: str | None = None,
) -> list[str]:
    """Find KiCad project files, optionally filtered by project name."""
    bases = [projects_base_path] if projects_base_path else KICAD_PROJECTS_BASES
    all_files: list[str] = []
    for base in bases:
        all_files = find_files_in_snapshot(snapshot_zip, extension, base)
        if all_files:
            break
    if not project_name:
        return all_files
    project_lower = project_name.lower()
    return [f for f in all_files if project_lower in f.rsplit("/", 1)[-1].lower()]


def find_pcb_file(
    snapshot_zip: zipfile.ZipFile, project_name: str | None = None
) -> str | None:
    """Find a .kicad_pcb file in the snapshot."""
    files = find_project_files(snapshot_zip, ".kicad_pcb", project_name)
    return files[0] if files else None


def find_report(
    snapshot_zip: zipfile.ZipFile,
    suffix: str,
    project_name: str | None = None,
    directory: str = "reports/",
) -> dict[str, Any] | None:
    """Find and parse a JSON file from a snapshot subdirectory.

    Searches for files matching the suffix within the given directory
    (default: exports/reports/). Also usable for simulation/ results.
    """
    report_files = [
        name
        for name in snapshot_zip.namelist()
        if directory in name and name.endswith(suffix)
    ]
    if project_name:
        project_lower = project_name.lower()
        filtered = [
            f for f in report_files if project_lower in f.rsplit("/", 1)[-1].lower()
        ]
        if filtered:
            report_files = filtered

    if not report_files:
        return None

    report_files.sort()
    try:
        content = snapshot_zip.read(report_files[-1]).decode("utf-8")
        return json.loads(content)
    except (KeyError, json.JSONDecodeError, Exception) as e:
        logger.warning(f"Could not parse report {report_files[-1]}: {e}")
        return None


def extract_balanced_block(text: str, start: int) -> str:
    """Extract a balanced parenthesized block starting at position start."""
    depth = 0
    i = start
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return text[start:]


def count_vias(pcb_text: str) -> int:
    """Count vias in a PCB file."""
    return len(re.findall(r"\(via\s+\(at\s+", pcb_text))
