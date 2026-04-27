"""Reproducibility manifest written alongside trajectory.json.

Captures every input that affects determinism for a given run so
trajectories can be replayed and discrepancies isolated to their
source. The manifest is best-effort: LLM-side determinism via
seed is provider-dependent and not guaranteed even at temperature
zero. The value of this artifact is in making the inputs to a
run inspectable and comparable, not in promising bitwise replay.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class RunManifest(BaseModel):
    """Sidecar artifact capturing all run-determining inputs."""

    schema_version: int = 1
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # Code provenance
    git_sha: str | None = None
    git_dirty: bool = False
    python_version: str = Field(default_factory=lambda: sys.version)

    # Run config
    trajectory_id: str
    agent_config_id: str
    agent_config_values: dict[str, Any]
    orchestrator_model: str
    orchestrator_extra_args: dict[str, Any]
    seed: int | None = None
    deterministic: bool = False

    # Inputs / outputs
    mcp_server_configs_hash: str | None = None
    initial_snapshot_sha256: str | None = None
    final_snapshot_sha256: str | None = None

    def write(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2))

    @classmethod
    def from_run_inputs(
        cls,
        trajectory_id: str,
        agent_config_id: str,
        agent_config_values: dict[str, Any],
        orchestrator_model: str,
        orchestrator_extra_args: dict[str, Any],
        seed: int | None = None,
        deterministic: bool = False,
        mcp_server_configs: dict[str, Any] | None = None,
    ) -> RunManifest:
        return cls(
            trajectory_id=trajectory_id,
            agent_config_id=agent_config_id,
            agent_config_values=agent_config_values,
            orchestrator_model=orchestrator_model,
            orchestrator_extra_args=orchestrator_extra_args,
            seed=seed,
            deterministic=deterministic,
            git_sha=_git_sha(),
            git_dirty=_git_dirty(),
            mcp_server_configs_hash=_hash_dict(mcp_server_configs)
            if mcp_server_configs
            else None,
        )

    def attach_snapshots(
        self,
        initial_path: Path | None = None,
        final_path: Path | None = None,
    ) -> None:
        if initial_path and initial_path.exists():
            self.initial_snapshot_sha256 = _sha256_file(initial_path)
        if final_path and final_path.exists():
            self.final_snapshot_sha256 = _sha256_file(final_path)


def _git_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _git_dirty() -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _hash_dict(d: dict[str, Any]) -> str:
    blob = json.dumps(d, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
