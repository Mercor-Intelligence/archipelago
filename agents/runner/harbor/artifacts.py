"""Native Harbor artifact collection and redaction."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from runner.harbor.runtime import (
    find_harbor_trajectory,
    find_harbor_trial_result,
    is_harbor_storage_id,
)

_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_MAX_DIRECT_ARTIFACT_BYTES = 256 * 1024 * 1024


def artifact_prefix(storage_id: str) -> str:
    if not is_harbor_storage_id(storage_id):
        raise ValueError("invalid Harbor artifact identity")
    return f"trajectories/{storage_id}/harbor"


def redact_bytes(content: bytes, secret_values: list[str]) -> bytes:
    """Replace known secret values in an artifact payload."""

    for value in secret_values:
        if value:
            content = content.replace(value.encode(), b"[REDACTED]")
    return content


def _read_redacted(path: Path, *, secret_values: list[str]) -> bytes:
    with path.open("rb") as source:
        content = source.read(_MAX_DIRECT_ARTIFACT_BYTES + 1)
    if len(content) > _MAX_DIRECT_ARTIFACT_BYTES:
        raise ValueError(f"Harbor artifact {path.name} exceeds size limit")
    return redact_bytes(content, secret_values)


def build_redacted_native_artifacts(
    jobs_dir: Path, *, secret_values: list[str]
) -> tuple[dict[str, bytes], bool]:
    """Collect first-class Harbor files from a single trial when available."""

    artifacts: dict[str, bytes] = {}
    partial = False
    finders = (
        ("native/trajectory.json", find_harbor_trajectory),
        ("native/result.json", find_harbor_trial_result),
    )
    for key, find_path in finders:
        try:
            path = find_path(jobs_dir)
            if path is None:
                continue
            artifacts[key] = _read_redacted(path, secret_values=secret_values)
        except (OSError, ValueError):
            partial = True
    return artifacts, partial


def build_redacted_jobs_tar(jobs_dir: Path, *, secret_values: list[str]) -> bytes:
    """Create a bounded tarball while replacing known secret byte sequences."""

    files: list[tuple[Path, str]] = []
    total = 0
    for path in sorted(jobs_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        total += path.stat().st_size
        if total > _MAX_ARTIFACT_BYTES:
            raise ValueError("Harbor jobs artifacts exceed size limit")
        relative = path.relative_to(jobs_dir).as_posix()
        files.append((path, f"jobs/{relative}"))

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        total = 0
        for path, arcname in files:
            content = redact_bytes(path.read_bytes(), secret_values)
            total += len(content)
            if total > _MAX_ARTIFACT_BYTES:
                raise ValueError("Harbor jobs artifacts exceed size limit")
            info = tarfile.TarInfo(arcname)
            info.size = len(content)
            info.mtime = 0
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()
