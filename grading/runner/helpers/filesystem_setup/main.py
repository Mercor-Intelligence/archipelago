"""Filesystem setup helper for code execution verifiers."""

import io
import zipfile
from pathlib import Path
from typing import Any

from loguru import logger

from runner.models import AgentTrajectoryOutput


async def filesystem_setup_helper(
    initial_snapshot_bytes: io.BytesIO,
    final_snapshot_bytes: io.BytesIO,
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

    # Extract initial snapshot (world + task data)
    if initial_snapshot_bytes and initial_snapshot_bytes.getbuffer().nbytes > 0:
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
    if final_snapshot_bytes and final_snapshot_bytes.getbuffer().nbytes > 0:
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

    # Reset BytesIO positions for reuse by other helpers
    initial_snapshot_bytes.seek(0)
    final_snapshot_bytes.seek(0)

    logger.info("[FILESYSTEM] Files extracted successfully, ready for verifiers")

    return {
        "initial_file_count": initial_file_count,
        "final_file_count": final_file_count,
        "extract_base": str(extract_base),
    }
