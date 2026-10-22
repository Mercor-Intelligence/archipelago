"""Streaming upload handler for direct file population.

This module handles extracting tar.gz archives uploaded directly via HTTP
into subsystem directories. Memory-safe implementation that streams to disk
and extracts incrementally.
"""

import tarfile
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from loguru import logger

from runner.utils.settings import get_settings

from .models import PopulateStreamResult

settings = get_settings()


def get_subsystem_paths() -> dict[str, Path]:
    """Get mapping of subsystem names to their root paths.

    Returns:
        Dictionary mapping subsystem names to Path objects
    """
    return {
        settings.FILESYSTEM_SUBSYSTEM_NAME: Path(
            f"/{settings.FILESYSTEM_SUBSYSTEM_NAME}"
        ),
        settings.APPS_DATA_SUBSYSTEM_NAME: Path(
            f"/{settings.APPS_DATA_SUBSYSTEM_NAME}"
        ),
    }


def validate_subsystem(subsystem: str) -> None:
    """Validate subsystem parameter to prevent path traversal attacks.

    Args:
        subsystem: The subsystem path to validate

    Raises:
        ValueError: If the subsystem contains path traversal sequences
    """
    if not subsystem or not subsystem.strip():
        raise ValueError("Subsystem name cannot be empty")

    if subsystem.startswith("/"):
        raise ValueError("Subsystem name cannot start with '/'")

    if ".." in subsystem:
        raise ValueError(
            "Subsystem name cannot contain '..' (path traversal not allowed)"
        )

    if "\\" in subsystem:
        raise ValueError(
            "Subsystem name cannot contain '\\' (use '/' for nested paths)"
        )


async def handle_populate_stream(
    file_stream: AsyncIterator[bytes],
    subsystem: str,
) -> PopulateStreamResult:
    """
    Extract a tar.gz stream directly into a subsystem.

    Memory-safe implementation:
    - Streams upload to temporary file (no memory buffering)
    - Extracts incrementally using tar.next() (constant memory)

    Args:
        file_stream: Async iterator of bytes from uploaded file
        subsystem: Target subsystem name ("filesystem" or ".apps_data")

    Returns:
        PopulateStreamResult with extraction statistics

    Raises:
        ValueError: Unknown subsystem name or path traversal attempt
    """
    validate_subsystem(subsystem)

    subsystem_paths = get_subsystem_paths()

    target_path: Path | None = None
    for root_name, root_path in subsystem_paths.items():
        if subsystem == root_name:
            target_path = root_path
            break
        elif subsystem.startswith(f"{root_name}/"):
            target_path = root_path / subsystem[len(root_name) + 1 :]
            break

    if target_path is None:
        valid = ", ".join(f"'{k}'" for k in subsystem_paths.keys())
        raise ValueError(f"Subsystem must start with {valid}. Got: '{subsystem}'")

    target_path.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=True) as tmp:
        async for chunk in file_stream:
            _ = tmp.write(chunk)

        tmp.flush()
        _ = tmp.seek(0)

        objects_added = 0
        extracted_bytes = 0

        with tarfile.open(fileobj=tmp, mode="r:gz") as tar:
            while True:
                member = tar.next()
                if member is None:
                    break

                objects_added += 1

                if member.isfile():
                    extracted_bytes += member.size

                tar.extract(member, path=target_path, filter="data")

    logger.info(
        f"Extracted {objects_added} objects ({extracted_bytes / 1e6:.1f} MB) to {target_path}"
    )

    return PopulateStreamResult(
        objects_added=objects_added,
        subsystem=subsystem,
        extracted_bytes=extracted_bytes,
    )
