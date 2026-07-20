"""Materialize the minimal Harbor task package for a Studio trajectory."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel

from modal_models import AgentConfigResponse

_IDENTITY_RE = re.compile(r"^(?:traj|task|world)_[A-Za-z0-9_]+$")


def _validated_identity(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise ValueError(f"invalid Harbor {field} identity")
    return value


def _message_text(message: object) -> list[str]:
    if isinstance(message, BaseModel):
        message = message.model_dump(mode="json")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []

    parts: list[str] = []
    for block in content:
        if isinstance(block, BaseModel):
            block = block.model_dump(mode="json")
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if block.get("type") == "text" and isinstance(text, str):
            parts.append(text)
    return parts


def _instruction(initial_messages: object) -> str:
    if not isinstance(initial_messages, list):
        return ""
    return "\n\n".join(
        part for message in initial_messages for part in _message_text(message) if part
    )


def _metadata(config: AgentConfigResponse) -> dict[str, str | int]:
    trajectory_id = _validated_identity(
        getattr(config, "trajectory_id", None), field="trajectory"
    )
    if trajectory_id is None:
        raise ValueError("missing Harbor trajectory identity")
    metadata: dict[str, str | int] = {
        "trajectory_id": trajectory_id,
    }
    return metadata


def _task_toml(metadata: dict[str, str | int]) -> str:
    lines = ['version = "1.0"', "", "[metadata]"]
    for key, value in metadata.items():
        if isinstance(value, int):
            lines.append(f"{key} = {value}")
        else:
            lines.append(f'{key} = "{value}"')
    lines.extend(
        [
            "",
            "[verifier]",
            "timeout_sec = 900.0",
            "",
            "[agent]",
            "timeout_sec = 86400.0",
            "",
            "[environment]",
            "build_timeout_sec = 600.0",
            "",
        ]
    )
    return "\n".join(lines)


def materialize_task_package(root: Path, config: AgentConfigResponse) -> Path:
    """Write a server-independent Harbor task directory from agent config."""

    metadata = _metadata(config)
    trajectory_id = metadata["trajectory_id"]
    task_dir = root / f"studio-{trajectory_id}"
    task_dir.mkdir(parents=True, exist_ok=False)
    # Harbor requires this directory to classify --path as a task even when an
    # explicit custom environment provider owns runtime setup.
    (task_dir / "environment").mkdir()
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    test_script = tests_dir / "test.sh"
    test_script.write_text(
        "#!/bin/sh\n"
        'echo "Studio real-time Harbor verification is disabled; use Studio grading." >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    test_script.chmod(0o755)
    (task_dir / "instruction.md").write_text(
        _instruction(getattr(config, "initial_messages", None)), encoding="utf-8"
    )
    (task_dir / "task.toml").write_text(_task_toml(metadata), encoding="utf-8")
    (task_dir / "manifest.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8"
    )
    return task_dir
