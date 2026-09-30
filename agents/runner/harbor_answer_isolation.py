"""Verify reserved Harbor answer directories before model execution."""

from collections.abc import Awaitable, Callable
from typing import Any

import modal
from loguru import logger
from pydantic import BaseModel, ConfigDict

from runner.agents.models import AgentStatus, AgentTrajectoryOutput

_PROVENANCE_KEY = "harbor_answer_isolation"
# Mirrors harbor_world_source: the same reserved paths (is_reserved_answer_path), parent
# directories and sorted order, so a world digest and a sandbox digest agree byte for
# byte. Root-only trees hash exactly as they did before steps/ was covered.
_CHECK_SCRIPT = r"""
import hashlib
import json
import os
import stat
import sys
root = sys.argv[1]
harbor = all(os.path.lexists(os.path.join(root, name)) for name in ("task.toml", "instruction.md"))
entries = {}

def plain_dir(path):
    mode = os.lstat(path).st_mode
    if stat.S_ISLNK(mode):
        raise SystemExit("Harbor isolation cannot fingerprint symlinks or special files")
    return stat.S_ISDIR(mode)

def visit(path, parts):
    mode = os.lstat(path).st_mode
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise SystemExit("Harbor isolation cannot fingerprint symlinks or special files")
    if stat.S_ISDIR(mode):
        entries[parts] = None
        for name in sorted(os.listdir(path)):
            visit(os.path.join(path, name), parts + (name,))
    else:
        content = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                content.update(chunk)
        entries[parts] = content.digest()

if harbor:
    for name in ("golden.patch", "golden_patch", "solution", "tests"):
        path = os.path.join(root, name)
        if os.path.lexists(path):
            visit(path, (name,))
    steps = os.path.join(root, "steps")
    if os.path.lexists(steps) and plain_dir(steps):
        for step in sorted(os.listdir(steps)):
            step_path = os.path.join(steps, step)
            if not plain_dir(step_path):
                continue
            for name in ("solution", "tests"):
                path = os.path.join(step_path, name)
                if os.path.lexists(path):
                    entries[("steps",)] = None
                    entries[("steps", step)] = None
                    visit(path, ("steps", step, name))
digest = hashlib.sha256()
for parts, content_hash in sorted(entries.items()):
    digest.update(json.dumps(["/".join(parts), "dir" if content_hash is None else "file"]).encode() + b"\0")
    if content_hash is not None:
        digest.update(content_hash)
print(json.dumps({"harbor": harbor, "answer_digest": digest.hexdigest() if entries else None}))
"""


class HarborFilesystemState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    harbor: bool
    answer_digest: str | None


async def _read_state(sandbox: modal.Sandbox) -> HarborFilesystemState:
    process = await sandbox.exec.aio(
        "python3", "-c", _CHECK_SCRIPT, "/filesystem", timeout=30
    )
    stdout = await process.stdout.read.aio()
    stderr = await process.stderr.read.aio()
    code = await process.wait.aio()
    if code != 0:
        raise RuntimeError(
            f"Harbor answer isolation could not be verified (exit={code}): {stderr[:2000]}"
        )
    try:
        return HarborFilesystemState.model_validate_json(stdout)
    except ValueError as exc:
        raise RuntimeError("Harbor answer isolation returned invalid evidence") from exc


async def assert_harbor_answers_isolated(
    sandbox: modal.Sandbox,
    *,
    parent_output: dict[str, Any] | None = None,
    snapshot_id: str | None = None,
    existing_sandbox_id: str | None = None,
    world_digest_loader: Callable[[], Awaitable[str | None]] | None = None,
) -> None:
    """Allow reserved files only when they match trusted world or bound parent evidence."""
    state = await _read_state(sandbox)
    if not state.harbor or state.answer_digest is None:
        return
    record = (parent_output or {}).get(_PROVENANCE_KEY)
    if isinstance(record, dict) and record.get("version") == 2:
        identity_matches = (
            record.get("sandbox_id") == existing_sandbox_id
            if existing_sandbox_id
            else bool(snapshot_id) and record.get("snapshot_id") == snapshot_id
        )
        if identity_matches and record.get("answer_digest") == state.answer_digest:
            return
    if world_digest_loader is not None:
        world_digest = await world_digest_loader()
        if world_digest is not None and world_digest == state.answer_digest:
            if await _read_state(sandbox) == state:
                return
    raise RuntimeError(
        "Harbor answer material remains without matching world or parent filesystem evidence; "
        "rerun from an isolated task or use the Lighthouse Harbor harness"
    )


def run_sandbox_id(sandbox: modal.Sandbox, existing_sandbox_id: str | None) -> str:
    """The sandbox a run used, owned or borrowed, as a later round attaches to it."""
    return existing_sandbox_id or sandbox.object_id


async def capture_harbor_isolation(
    sandbox: modal.Sandbox,
) -> HarborFilesystemState | None:
    """Capture actor-created trees without failing a completed run on capture errors."""
    try:
        return await _read_state(sandbox)
    except Exception as exc:
        logger.warning(f"Harbor isolation provenance unavailable: {exc}")
        return None


async def confirm_harbor_snapshot(
    sandbox: modal.Sandbox,
    actor_state: HarborFilesystemState | None,
    snapshot_id: str,
) -> tuple[HarborFilesystemState | None, str | None]:
    """Snapshot hooks may change unrelated files, but must not introduce answer files."""
    after_hooks = await capture_harbor_isolation(sandbox)
    if actor_state is not None and actor_state == after_hooks:
        return actor_state, snapshot_id
    return None, None


def record_harbor_isolation(
    output: AgentTrajectoryOutput,
    state: HarborFilesystemState | None,
    *,
    snapshot_id: str | None,
    sandbox_id: str | None,
) -> None:
    """Replace inherited output with evidence captured by this runner after the actor."""
    payload = dict(output.output or {})
    payload.pop(_PROVENANCE_KEY, None)
    if state is not None and state.harbor and output.status == AgentStatus.COMPLETED:
        payload[_PROVENANCE_KEY] = {
            "version": 2,
            "snapshot_id": snapshot_id,
            "sandbox_id": sandbox_id,
            "answer_digest": state.answer_digest,
        }
    output.output = payload or None
