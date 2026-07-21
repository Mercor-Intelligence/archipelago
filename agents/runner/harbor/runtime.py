"""Safe Harbor CLI orchestration primitives for Studio batch workers."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import TypeGuard
from urllib.parse import urlsplit

from runner.agents.models import AgentTrajectoryOutput

_LOG_READ_CHUNK_BYTES = 16 * 1024
_MAX_LOG_RECORD_BYTES = 64 * 1024
_MAX_NATIVE_OUTPUT_BYTES = 64 * 1024 * 1024
_MAX_RESULT_BYTES = 16 * 1024 * 1024
_MAX_REFERENCE_BYTES = 4 * 1024
_PROCESS_TERMINATION_GRACE_SECONDS = 60
_STORAGE_ID_RE = re.compile(r"^(?:snap|traj)_[A-Za-z0-9_]+$")


def is_harbor_storage_id(value: object) -> TypeGuard[str]:
    """Return whether a value is a canonical Harbor artifact identity."""

    return isinstance(value, str) and _STORAGE_ID_RE.fullmatch(value) is not None


def build_harbor_command(
    *,
    task_dir: str,
    jobs_dir: str,
    trajectory_id: str,
    orchestrator_model: str,
) -> list[str]:
    """Build a list-form Harbor command; dynamic values are never shell parsed."""

    return [
        "harbor",
        "run",
        "--path",
        task_dir,
        "--jobs-dir",
        jobs_dir,
        "--job-name",
        trajectory_id,
        "--agent-import-path",
        "runner.harbor.agent:StudioArchipelagoAgent",
        "--agent-kwarg",
        f"trajectory_id={trajectory_id}",
        "--model",
        orchestrator_model,
        "--environment-import-path",
        "runner.harbor.studio_environment:StudioModalEnvironment",
        "--environment-kwarg",
        f"trajectory_id={trajectory_id}",
        "--n-concurrent",
        "1",
        "--n-attempts",
        "1",
        # Studio's existing post-save grading pipeline remains authoritative for
        # real-time runs. TEMP_HARBOR's verifier image is delivery-oriented and
        # assumes a compose-built world rather than this populated Studio sandbox.
        "--disable-verification",
        "--yes",
    ]


async def run_harbor_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    on_line: Callable[[str, str], None],
) -> None:
    """Run Harbor without a shell and stream both output channels."""

    if not command:
        raise ValueError("Harbor command cannot be empty")
    child_env = os.environ.copy()
    child_env.setdefault("MODAL_TOKEN_ID", "unused-ambient-identity")
    child_env.setdefault("MODAL_TOKEN_SECRET", "unused-ambient-identity")
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=child_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def pump(source: str, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        pending = bytearray()
        while chunk := await stream.read(_LOG_READ_CHUNK_BYTES):
            pending.extend(chunk)
            while pending:
                newline = pending.find(b"\n")
                if 0 <= newline <= _MAX_LOG_RECORD_BYTES:
                    record = bytes(pending[:newline]).removesuffix(b"\r")
                    del pending[: newline + 1]
                elif len(pending) >= _MAX_LOG_RECORD_BYTES:
                    record = bytes(pending[:_MAX_LOG_RECORD_BYTES])
                    del pending[:_MAX_LOG_RECORD_BYTES]
                else:
                    break
                on_line(source, record.decode("utf-8", errors="replace"))
        if pending:
            on_line(source, bytes(pending).decode("utf-8", errors="replace"))

    try:
        async with asyncio.timeout(timeout_seconds):
            await asyncio.gather(
                pump("stdout", process.stdout),
                pump("stderr", process.stderr),
                process.wait(),
            )
    except BaseException:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=_PROCESS_TERMINATION_GRACE_SECONDS,
                )
            except TimeoutError:
                process.kill()
                await process.wait()
        raise

    if process.returncode != 0:
        raise RuntimeError(f"Harbor exited with status {process.returncode}")


def _matching_trial_files(
    jobs_dir: Path,
    filename: str,
    *,
    subdirectory: str | None = None,
) -> list[Path]:
    """Find host-owned files at a fixed location in Harbor trial directories."""

    if Path(filename).name != filename:
        raise ValueError("Harbor trial filename must be a basename")
    if subdirectory is not None and Path(subdirectory).name != subdirectory:
        raise ValueError("Harbor trial subdirectory must be a basename")
    if jobs_dir.is_symlink() or not jobs_dir.is_dir():
        return []

    resolved_root = jobs_dir.resolve()
    matches: list[Path] = []
    for job_dir in jobs_dir.iterdir():
        if job_dir.is_symlink() or not job_dir.is_dir():
            continue
        for trial_dir in job_dir.iterdir():
            if trial_dir.is_symlink() or not trial_dir.is_dir():
                continue
            parent = trial_dir
            if subdirectory is not None:
                parent = trial_dir / subdirectory
                if parent.is_symlink() or not parent.is_dir():
                    continue
            path = parent / filename
            if path.is_symlink() or not path.is_file():
                continue
            try:
                path.resolve().relative_to(resolved_root)
            except ValueError:
                continue
            matches.append(path)
    return sorted(matches)


def find_harbor_trajectory(jobs_dir: Path) -> Path | None:
    """Find the one Harbor ATIF trajectory emitted by the trial agent."""

    matches = _matching_trial_files(
        jobs_dir,
        "trajectory.json",
        subdirectory="agent",
    )
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(
            "Harbor job must contain at most one agent/trajectory.json; "
            f"found {len(matches)}"
        )
    return matches[0]


def _read_bounded(path: Path, *, max_bytes: int, label: str) -> bytes:
    with path.open("rb") as source:
        content = source.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError(f"Harbor {label} exceeds size limit")
    return content


def _load_result_payload(path: Path) -> dict[str, object]:
    content = _read_bounded(
        path,
        max_bytes=_MAX_RESULT_BYTES,
        label="result.json",
    )
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError("Harbor result.json must contain a JSON object")
    return payload


def find_harbor_trial_result(jobs_dir: Path) -> Path | None:
    """Find the trial result while ignoring Harbor's aggregate job result."""

    candidates = _matching_trial_files(jobs_dir, "result.json")
    matches: list[Path] = []
    for path in candidates:
        payload = _load_result_payload(path)
        if {"task_name", "trial_name"}.issubset(payload):
            matches.append(path)
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(
            "Harbor job must contain at most one trial result.json; "
            f"found {len(matches)}"
        )
    return matches[0]


def load_native_output(jobs_dir: Path) -> AgentTrajectoryOutput:
    """Load the one authoritative Archipelago-native output from a Harbor job."""

    matches = _matching_trial_files(
        jobs_dir,
        "trajectory.native.json",
        subdirectory="agent",
    )
    if len(matches) != 1:
        raise ValueError(
            "Harbor job must contain exactly one trajectory.native.json; "
            f"found {len(matches)}"
        )
    content = _read_bounded(
        matches[0],
        max_bytes=_MAX_NATIVE_OUTPUT_BYTES,
        label="native output",
    )
    return AgentTrajectoryOutput.model_validate_json(content)


def load_snapshot_id(jobs_dir: Path, filename: str) -> str | None:
    matches = _matching_trial_files(
        jobs_dir,
        filename,
        subdirectory="artifacts",
    )
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(
            f"Harbor job must contain at most one {filename}; found {len(matches)}"
        )
    content = _read_bounded(
        matches[0],
        max_bytes=_MAX_REFERENCE_BYTES,
        label="snapshot reference",
    )
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError(f"Harbor snapshot reference in {filename} is invalid")
    snapshot_id = payload.get("snapshot_id")
    if not is_harbor_storage_id(snapshot_id) or not snapshot_id.startswith("snap_"):
        raise ValueError(f"Harbor snapshot reference in {filename} is invalid")
    return snapshot_id


def load_env_image_layer_s3_uri(jobs_dir: Path, trajectory_id: str) -> str | None:
    filename = "studio_env_image_layer.json"
    matches = _matching_trial_files(
        jobs_dir,
        filename,
        subdirectory="artifacts",
    )
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(
            f"Harbor job must contain at most one {filename}; found {len(matches)}"
        )
    content = _read_bounded(
        matches[0],
        max_bytes=_MAX_REFERENCE_BYTES,
        label="env image layer reference",
    )
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError("Harbor env image layer reference is invalid")
    s3_uri = payload.get("env_image_layer_s3_uri")
    if not isinstance(s3_uri, str):
        raise ValueError("Harbor env image layer reference is invalid")
    parsed = urlsplit(s3_uri)
    expected_path = f"/trajectories/{trajectory_id}/rootfs-layer.tar.gz"
    if (
        parsed.scheme != "s3"
        or not parsed.netloc
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Harbor env image layer reference is invalid")
    return s3_uri


def raise_for_harbor_result_error(jobs_dir: Path) -> None:
    """Surface Harbor trial errors that do not necessarily change CLI exit code."""

    result_path = find_harbor_trial_result(jobs_dir)
    if result_path is None:
        return
    payload = _load_result_payload(result_path)
    exception = payload.get("exception_info")
    if exception:
        if isinstance(exception, dict):
            detail = exception.get("exception_message") or exception.get("message")
        else:
            detail = str(exception)
        raise RuntimeError(f"Harbor trial failed: {detail or 'unknown error'}")
