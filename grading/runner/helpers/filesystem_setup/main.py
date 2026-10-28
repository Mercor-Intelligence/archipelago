"""Filesystem setup helper for code execution verifiers."""

import zipfile
from pathlib import Path
from typing import IO, Any

from loguru import logger

from runner.models import AgentTrajectoryOutput
from runner.utils.metrics import distribution

# Emitted per layer so `studio.grading.helper_seconds{helper:filesystem_setup}`
# can be normalized. Time alone cannot separate "one huge file" from "200k tiny
# ones", and the extract loop does per-member work (a `resolve()` and a
# `zf.extract()` each), so the file count is the divisor that matters.
_FILES_METRIC = "studio.grading.filesystem_setup_files"
_BYTES_METRIC = "studio.grading.filesystem_setup_bytes"


def _snapshot_size(f: IO[bytes]) -> int:
    """Return the size of a seekable file-like object without consuming it."""
    pos = f.tell()
    f.seek(0, 2)
    size = f.tell()
    f.seek(pos)
    return size


async def filesystem_setup_helper(
    initial_snapshot_bytes: IO[bytes],
    final_snapshot_bytes: IO[bytes],
    trajectory: AgentTrajectoryOutput,
) -> dict[str, Any]:
    """
    Extract snapshot files to filesystem for code execution verifiers.

    This helper runs once before all verifiers, extracting files from snapshots
    to the root directory. Files are layered: initial snapshot (world + task)
    first, then final snapshot (trajectory output) which may overwrite.

    Args:
        initial_snapshot_bytes: Initial snapshot (world + task data)
        final_snapshot_bytes: Final snapshot (trajectory output)
        trajectory: Agent trajectory (unused but required by interface)

    Returns:
        Dictionary with extraction statistics
    """
    extract_base = Path("/")
    logger.info(f"[FILESYSTEM] Extracting files to root directory: {extract_base}")

    initial_file_count = 0
    final_file_count = 0

    initial_size = (
        _snapshot_size(initial_snapshot_bytes) if initial_snapshot_bytes else 0
    )
    final_size = _snapshot_size(final_snapshot_bytes) if final_snapshot_bytes else 0

    # Extract initial snapshot (world + task data)
    if initial_size > 0:
        initial_snapshot_bytes.seek(0)
        with zipfile.ZipFile(initial_snapshot_bytes, "r") as zf:
            for member in zf.namelist():
                member_path = (extract_base / member).resolve()
                try:
                    member_path.relative_to(extract_base.resolve())
                except ValueError:
                    logger.warning(f"[FILESYSTEM] Skipping path traversal: {member}")
                    continue
                zf.extract(member, extract_base)
            initial_file_count = len(zf.namelist())
            logger.info(
                f"[FILESYSTEM] Extracted {initial_file_count} files from initial snapshot"
            )

    # Extract final snapshot (trajectory output, may overwrite)
    if final_size > 0:
        final_snapshot_bytes.seek(0)
        with zipfile.ZipFile(final_snapshot_bytes, "r") as zf:
            for member in zf.namelist():
                member_path = (extract_base / member).resolve()
                try:
                    member_path.relative_to(extract_base.resolve())
                except ValueError:
                    logger.warning(f"[FILESYSTEM] Skipping path traversal: {member}")
                    continue
                zf.extract(member, extract_base)
            final_file_count = len(zf.namelist())
            logger.info(
                f"[FILESYSTEM] Extracted {final_file_count} files from final snapshot"
            )

    # Reset positions for reuse by other helpers
    initial_snapshot_bytes.seek(0)
    final_snapshot_bytes.seek(0)

    # Emitted for both layers unconditionally, including the zero case: a run
    # that extracted nothing still has to show up, or the per-file average is
    # computed over a denominator that silently drops the empty runs.
    for layer, files, size in (
        ("initial", initial_file_count, initial_size),
        ("final", final_file_count, final_size),
    ):
        distribution(_FILES_METRIC, float(files), tags=[f"layer:{layer}"])
        distribution(_BYTES_METRIC, float(size), tags=[f"layer:{layer}"])

    logger.info("[FILESYSTEM] Files extracted successfully, ready for verifiers")

    return {
        "initial_file_count": initial_file_count,
        "final_file_count": final_file_count,
        "extract_base": str(extract_base),
    }
